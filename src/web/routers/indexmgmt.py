# -*- coding: utf-8 -*-
"""指数管理接口：成分股列表、手动加入/移除、权重设置、股票搜索。

成分股增删都落在 index_constituent（is_active 标记），增删后会自动重算
该指数最近一天的估值与评分，保证页面上的数据即时一致。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.infrastructure.config import config
from src.indicators import indicators
from src.notify import pipeline
from src.infrastructure.persistence import storage
from src.web import deps

log = logging.getLogger("web.index")
router = APIRouter(prefix="/api", tags=["index"])


class CodesBody(BaseModel):
    codes: list[str]


class WeightBody(BaseModel):
    pe: float = 0
    pb: float = 0
    ps: float = 0
    pcf: float = 0
    dividend: float = 0


def _refresh_target(conn, code: str, ktype: str, full: bool = False) -> dict | None:
    """成分股/权重变化后重算该目标（尽力而为）。

    组合比较特殊：它的 K 线是成分股**等权合成**出来的，成分股一变，
    K 线以及基于 K 线的全部分位/评分都会过期，所以必须整条重算
    （rebuild_portfolio：组合K线 → 历史评分 → 当日评分）。
    指数不需要：指数的 K 线来自行情接口，只有估值聚合和当日评分要刷。
    """
    try:
        if ktype == "portfolio":
            if full:
                # 组合：先增量补齐成分股数据（K线/分红/元数据），再删旧数据全量重算
                return pipeline.rebuild_portfolio(conn, code)
            indicators.portfolio_score(conn, code)
        elif ktype == "index":
            indicators.rebuild_index_valuation(conn, code, only_missing=True)
            indicators.index_score(conn, code)
        else:
            indicators.stock_score(conn, code)
    except Exception as e:                      # noqa: BLE001
        log.warning("%s 重算失败（数据可能还没抓）：%s", code, e)
    return None


@router.get("/stock/search", summary="按代码/名称搜索股票")
def stock_search(q: str = "", limit: int = 20, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        return {"items": storage.search_stocks(conn, q, limit=min(limit, 100))}
    finally:
        conn.close()


@router.get("/index/{code}/constituents", summary="成分股列表")
def constituents(code: str, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        rows = storage.constituent_rows(conn, code)
        # 附上每只成分股的最新估值与评分（只查这些成分股，避免全表聚合）
        cons = [r["code"] for r in rows]
        scores = storage.latest_score_dates_for(conn, cons)
        latest_k = storage.latest_kline_dates_for(conn, cons)
        for r in rows:
            r["latest_score"] = scores.get(r["code"])
            r["latest_kline"] = latest_k.get(r["code"])
        return {"code": code, "count": len(rows), "items": rows}
    finally:
        conn.close()


@router.post("/index/{code}/constituents", summary="加入成分股")
def constituents_add(code: str, body: CodesBody,
                     _: None = Depends(deps.require_auth)):
    t = config.target(code)
    if not t:
        raise HTTPException(status_code=404, detail=f"目标不存在：{code}")
    conn = deps.conn()
    try:
        added, new_codes = 0, []
        for c in body.codes:
            c = c.strip()
            if c and storage.add_constituent(conn, code, c):
                added += 1
                new_codes.append(c)
        total = len(storage.load_constituents(conn, code))

        # 组合：full=True 会走 pipeline.rebuild_portfolio —— 它会先检查并**只补缺的**
        # 成分股数据（没有 K线的拉 K线，缺元数据的补元数据），再删旧数据全量重算。
        # 这里不要再单独调一次 ensure_constituent_data，否则第二次已经无事可做，
        # 上报的数字会变成全 0（真实工作量在第一次里）。
        rb = _refresh_target(conn, code, t["ktype"], full=bool(added))
        sy = (rb or {}).get("sync") or {}
        failed = sy.get("failed") or []
        fetched = [c for c in new_codes if c not in failed] if new_codes else []
        return {"ok": True, "code": code, "added": added, "total": total,
                "fetched": fetched, "fetch_failed": failed, "rebuild": rb}
    finally:
        conn.close()


@router.post("/index/{code}/constituents/remove", summary="批量移除成分股")
def constituents_remove_batch(code: str, body: CodesBody,
                              _: None = Depends(deps.require_auth)):
    """批量移除（成分股表格里勾选多只后一次移除）。

    只重算一次：逐只移除后统一 rebuild，避免 N 只就触发 N 次全量重算。
    """
    t = config.target(code)
    if not t:
        raise HTTPException(status_code=404, detail=f"目标不存在：{code}")
    conn = deps.conn()
    try:
        n = 0
        for c in body.codes:
            c = c.strip()
            if c and storage.remove_constituent(conn, code, c):
                n += 1
        rb = _refresh_target(conn, code, t["ktype"], full=bool(n))
        return {"ok": True, "code": code, "removed": n, "rebuild": rb}
    finally:
        conn.close()


@router.delete("/index/{code}/constituents/{stock}", summary="移除成分股")
def constituents_remove(code: str, stock: str,
                        _: None = Depends(deps.require_auth)):
    t = config.target(code)
    conn = deps.conn()
    try:
        n = storage.remove_constituent(conn, code, stock)
        rb = _refresh_target(conn, code, t["ktype"], full=bool(n)) if t else None
        return {"ok": True, "code": code, "removed": n, "rebuild": rb}
    finally:
        conn.close()


@router.put("/target-order", summary="调整标的信息顺序")
def target_order(body: CodesBody, _: None = Depends(deps.require_auth)):
    """按提交的 codes 顺序重排标的信息。

    这个顺序同时决定「标的信息」页的列表顺序与**邮件里各标的的先后**，
    所以调完发出去的邮件顺序也跟着变。
    """
    conn = deps.conn()
    try:
        known = {t["code"] for t in config.targets(only_enabled=False)}
        codes = [c.strip() for c in body.codes if c and c.strip() in known]
        if not codes:
            raise HTTPException(status_code=400, detail="没有有效的标的代码")
        # 没提交到的（例如并发新增的）排到后面，保持相对顺序
        rest = [c for c in config.targets(only_enabled=False)
                if c["code"] not in codes]
        n = storage.set_target_order(conn, codes + [c["code"] for c in rest])
        return {"ok": True, "updated": n, "codes": codes}
    finally:
        conn.close()


@router.delete("/target/{code}", summary="移出标的信息")
def target_delete(code: str, _: None = Depends(deps.require_auth)):
    """把标的信息移出（任意 ktype：个股/指数/组合）。

    清理 valuation_target + 成分股 + 评分；组合还额外删掉它自己合成出来的 K 线
    （那是派生数据，留着没有意义；个股/指数的 K 线来自行情，保留）。
    """
    conn = deps.conn()
    try:
        t = config.target(code)
        n = storage.delete_target(conn, code)
        if not n:
            raise HTTPException(status_code=404, detail=f"标的不在标的信息里：{code}")
        if t and t.get("ktype") == "portfolio":
            storage.delete_kline(conn, code)
        return {"ok": True, "code": code, "name": (t or {}).get("name") or code}
    finally:
        conn.close()


@router.put("/index/{code}/weights", summary="设置指数评分权重")
def index_weights(code: str, body: WeightBody, _: None = Depends(deps.require_auth)):
    try:
        weights = deps.normalize_weights(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    conn = deps.conn()
    try:
        n = storage.set_target_weights(conn, code, weights)
        if not n:
            raise HTTPException(status_code=404, detail=f"目标不存在：{code}")
        # 权重变了 → 用已存分位重算该标的历史综合评分（不重读 K 线，秒级）
        try:
            indicators.recompute_scores(conn, code)
        except Exception as e:                      # noqa: BLE001
            log.warning("%s 权重变更后评分重算失败：%s", code, e)
        return {"ok": True, "code": code, "weights": weights}
    finally:
        conn.close()
