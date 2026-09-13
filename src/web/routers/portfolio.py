# -*- coding: utf-8 -*-
"""自定义组合接口：组合的增删、成分股手动增删、权重、组合评分。

组合 = valuation_target 里 ktype='portfolio' 的记录；成分股复用 index_constituent
（index_code 用组合的 code）。评分沿用指数算法（成分股分位加权 → 综合评分），
只是成分股清单由用户手动维护。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.config import config
from src.indicators import indicators
from src.notify import pipeline
from src.storage import storage
from src.web import deps

log = logging.getLogger("web.portfolio")
router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class PortfolioBody(BaseModel):
    name: str
    code: str | None = None
    pe: float = 0.25
    pb: float = 0.20
    ps: float = 0.25
    pcf: float = 0.15
    dividend: float = 0.15


class CodesBody(BaseModel):
    codes: list[str]


class WeightBody(BaseModel):
    pe: float = 0
    pb: float = 0
    ps: float = 0
    pcf: float = 0
    dividend: float = 0


def _next_code(conn) -> str:
    """生成下一个组合代码 pf.001 / pf.002 …"""
    rows = conn.execute(
        "SELECT code FROM valuation_target WHERE ktype='portfolio'").fetchall()
    used = {r[0] for r in rows}
    for i in range(1, 10000):
        code = f"pf.{i:03d}"
        if code not in used:
            return code
    raise HTTPException(status_code=500, detail="组合数量已达上限")


def _portfolio_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM valuation_target WHERE ktype='portfolio' "
        "ORDER BY sort_order, code").fetchall()
    scores = storage.latest_score_dates(conn)
    out = []
    for r in rows:
        out.append({
            "code": r["code"], "name": r["name"], "enabled": bool(r["enabled"]),
            "weights": {"pe": r["w_pe"] or 0.0, "pb": r["w_pb"] or 0.0,
                        "ps": r["w_ps"] or 0.0, "pcf": r["w_pcf"] or 0.0,
                        "dividend": r["w_dividend"] or 0.0},
            "sort_order": r["sort_order"], "remark": r["remark"],
            "constituent_count": len(storage.load_constituents(conn, r["code"])),
            "latest_score": scores.get(r["code"]),
        })
    return out


@router.get("", summary="组合列表")
def list_portfolios(_: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        return {"items": _portfolio_rows(conn)}
    finally:
        conn.close()


@router.post("", summary="创建组合")
def create_portfolio(body: PortfolioBody, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        code = (body.code or "").strip() or _next_code(conn)
        if config.target(code):
            raise HTTPException(status_code=409, detail=f"代码已存在：{code}")
        try:
            weights = deps.normalize_weights(body.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        storage.add_target(conn, code, body.name.strip() or code, "portfolio",
                           weights, enabled=True)
        return {"ok": True, "code": code, "name": body.name, "weights": weights}
    finally:
        conn.close()


@router.delete("/{code}", summary="删除组合")
def delete_portfolio(code: str, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        n = storage.delete_target(conn, code)
        if not n:
            raise HTTPException(status_code=404, detail=f"组合不存在：{code}")
        return {"ok": True, "code": code}
    finally:
        conn.close()


@router.put("/{code}/weights", summary="设置组合评分权重")
def portfolio_weights(code: str, body: WeightBody,
                      _: None = Depends(deps.require_auth)):
    try:
        weights = deps.normalize_weights(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    conn = deps.conn()
    try:
        n = storage.set_target_weights(conn, code, weights)
        if not n:
            raise HTTPException(status_code=404, detail=f"组合不存在：{code}")
        indicators.portfolio_score(conn, code)
        return {"ok": True, "code": code, "weights": weights}
    finally:
        conn.close()


@router.get("/{code}/constituents", summary="组合成分股")
def portfolio_constituents(code: str, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        rows = storage.constituent_rows(conn, code)
        return {"code": code, "count": len(rows), "items": rows}
    finally:
        conn.close()


@router.post("/{code}/constituents", summary="组合加入成分股")
def portfolio_add(code: str, body: CodesBody, _: None = Depends(deps.require_auth)):
    """加入成分股后重建组合K线 + 历史评分 + 当日评分。

    成分股变了，组合K线（= 成分股等权组合而成）和基于它的全部评分都必须重算，
    否则走势图还是旧成分股的结果。
    """
    conn = deps.conn()
    try:
        n = 0
        for c in body.codes:
            c = c.strip()
            if c and storage.add_constituent(conn, code, c):
                n += 1
        totals = len(storage.load_constituents(conn, code))
        if not n:
            return {"ok": True, "code": code, "added": 0, "total": totals,
                    "rebuild": None, "msg": "成分股无变化，未重算"}
        rb = indicators.rebuild_portfolio(conn, code)
        return {"ok": True, "code": code, "added": n, "total": totals,
                "rebuild": rb,
                "msg": f"已加入 {n} 只并重算：K线 {rb['kline_rows']} 个交易日、"
                       f"历史评分 {rb['score_rows']} 条"}
    finally:
        conn.close()


@router.delete("/{code}/constituents/{stock}", summary="组合移除成分股")
def portfolio_remove(code: str, stock: str, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        n = storage.remove_constituent(conn, code, stock)
        if not n:
            raise HTTPException(status_code=404,
                                detail=f"{stock} 不在组合 {code} 的成分股里")
        rb = indicators.rebuild_portfolio(conn, code)
        return {"ok": True, "code": code, "removed": n, "rebuild": rb,
                "msg": f"已移除并重算：K线 {rb['kline_rows']} 个交易日、"
                       f"历史评分 {rb['score_rows']} 条"}
    finally:
        conn.close()


@router.post("/{code}/score", summary="立即计算组合评分")
def portfolio_score_now(code: str, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        r = indicators.portfolio_score(conn, code, save=True)
        if not r:
            raise HTTPException(status_code=400,
                                detail="评分失败：成分股无可用的估值数据（先抓数）")
        return r
    finally:
        conn.close()


@router.post("/{code}/rebuild", summary="重算组合：补数据 + 删旧数据 + 全量重算")
def portfolio_rebuild(code: str, _: None = Depends(deps.require_auth)):
    """手动触发组合重算。

    ① 检查成分股的 K线/分红/元数据是否最新，落后就**增量拉取**；
    ② 删除该组合已算好的 K线 + 分位/评分；
    ③ 重新合成组合K线 → 历史分位/评分 → 当日综合评分。
    """
    conn = deps.conn()
    try:
        t = config.target(code)
        if not t or t["ktype"] != "portfolio":
            raise HTTPException(status_code=404, detail=f"组合不存在：{code}")
        rb = pipeline.rebuild_portfolio(conn, code)
        sy = rb.get("sync") or {}
        n_sync = len(sy.get("synced") or [])
        n_meta = int(sy.get("meta") or 0)
        bits = []
        if n_sync:
            bits.append(f"增量补拉 {n_sync} 只 K线")
        if n_meta:
            bits.append(f"补 {n_meta} 只元数据")
        extra = ("，" + "、".join(bits)) if bits else "，成分股数据已是最新"
        if sy.get("failed"):
            extra += f"，{len(sy['failed'])} 只拉取失败"
        return {"ok": True, "code": code, "rebuild": rb,
                "msg": f"重算完成{extra}（已删旧数据 K线 {rb.get('deleted_kline', 0)} 行、"
                       f"评分 {rb.get('deleted_score', 0)} 行）："
                       f"K线 {rb['kline_rows']} 个交易日、"
                       f"历史评分 {rb['score_rows']} 条、综合评分 "
                       f"{(rb.get('score') or 0):.1f}"}
    finally:
        conn.close()


@router.get("/{code}/score", summary="组合评分历史")
def portfolio_score_history(code: str, years: int = 10,
                            _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        rows = storage.load_scores(
            conn, code, start=deps.years_ago(years),
            fields=("date", "score", "score5", "status",
                    "pct_pe", "pct_pb", "pct_ps", "pct_pcf", "pct_dividend"))
        return {"code": code, "years": years, "items": rows}
    finally:
        conn.close()
