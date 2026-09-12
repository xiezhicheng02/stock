# -*- coding: utf-8 -*-
"""标的信息接口：列表、详情、K线、估值序列、评分序列、权重设置。

数据全部来自库（kline / valuation_score / stock_basic / valuation_target），
只读接口直接查库；权重写回走 storage.set_target_weights。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.config import config
from src.indicators import indicators
from src.storage import storage
from src.web import deps

log = logging.getLogger("web.assets")
router = APIRouter(prefix="/api", tags=["assets"])

METRIC_FIELDS = ("pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm", "div_yield")


class WeightsBody(BaseModel):
    pe: float = 0
    pb: float = 0
    ps: float = 0
    pcf: float = 0
    dividend: float = 0


def _targets(conn):
    """估值目标 + 各自的最新数据日期（用于列表页）。"""
    targets = config.targets(only_enabled=False)
    codes = [t["code"] for t in targets]
    latest_scores = storage.latest_score_dates_for(conn, codes)
    latest_k = storage.latest_kline_dates_for(conn, codes)
    out = []
    for t in targets:
        out.append({
            "code": t["code"], "name": t["name"], "ktype": t["ktype"],
            "enabled": t["enabled"], "sort_order": t["sort_order"],
            "weights": t["weights"], "remark": t["remark"],
            "latest_score": latest_scores.get(t["code"]),
            "latest_kline": latest_k.get(t["code"]),
        })
    return out


@router.get("/targets", summary="全部估值目标（指数/个股/组合）")
def targets(_: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        return {"items": _targets(conn)}
    finally:
        conn.close()


@router.get("/asset/{code}", summary="标的详情：基本信息 + 最新估值 + 最新评分 + 建议")
def asset_detail(code: str, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        t = config.target(code)
        basics = storage.load_stock_basic(conn, code=code)
        b = basics[0] if basics else {}
        sc = storage.latest_score(conn, code)
        krows = storage.load_kline(conn, code, fields=("date",) + METRIC_FIELDS
                                   + ("close", "pct_chg"))
        latest_k = krows[-1] if krows else {}
        span = storage.kline_span(conn, code)
        weights = t["weights"] if t else {}
        market = b.get("market") or (code.split(".")[0] if "." in code else None)
        return {
            "code": code,
            "name": t["name"] if t else (b.get("name") or code),
            "ktype": t["ktype"] if t else (b.get("ktype") or "stock"),
            "enabled": bool(t["enabled"]) if t else False,
            "is_target": bool(t),
            "market": market,
            "market_label": {"sh": "上交所", "sz": "深交所"}.get(market, market or ""),
            "industry": b.get("industry"),
            "listed_date": b.get("listed_date"),
            "weights": weights,
            "kline": {"rows": span["rows"], "first_date": span["first"],
                      "latest_date": latest_k.get("date"),
                      "close": latest_k.get("close"), "pct_chg": latest_k.get("pct_chg"),
                      "metrics": {m: latest_k.get(m) for m in METRIC_FIELDS}},
            "score": sc,
            "constituent_count": (len(storage.load_constituents(conn, code))
                                  if t and t["ktype"] in ("index", "portfolio") else None),
        }
    finally:
        conn.close()


@router.get("/asset/{code}/kline", summary="K线序列（蜡烛图 OHLCV）")
def asset_kline(code: str, years: int = 3, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        rows = storage.load_kline(
            conn, code, start=deps.years_ago(years),
            fields=("date", "open", "high", "low", "close", "volume", "amount",
                    "pct_chg", "turn"))
        return {"code": code, "years": years, "items": rows}
    finally:
        conn.close()


@router.get("/asset/{code}/valuation", summary="五指标估值序列")
def asset_valuation(code: str, years: int = 3, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        rows = storage.load_kline(conn, code, start=deps.years_ago(years),
                                  fields=("date",) + METRIC_FIELDS)
        return {"code": code, "years": years, "items": rows}
    finally:
        conn.close()


# 指标 → valuation_score 里的分位列
PCT_COLS = {"pe_ttm": "pct_pe", "pb_mrq": "pct_pb", "ps_ttm": "pct_ps",
            "pcf_ncf_ttm": "pct_pcf", "div_yield": "pct_dividend"}


@router.get("/asset/{code}/percentiles", summary="五指标历史分位序列")
def asset_percentiles(code: str, years: int | None = None,
                      _: None = Depends(deps.require_auth)):
    """各指标相对自身历史的滚动分位序列（供分位走势图）。

    优先读**已落库**的分位（全量重建后每只个股/指数都有），一次索引查询即可；
    库里没有记录时才退回实时计算（首次建库/新标的情况）。
    """
    conn = deps.conn()
    try:
        rows = storage.load_scores(conn, code, fields=("date",) + tuple(PCT_COLS.values()))
        if rows:
            metrics = {m: [{"date": r["date"], "pct": r[col]} for r in rows
                           if r[col] is not None]
                       for m, col in PCT_COLS.items()}
            return {"code": code, "metrics": metrics, "source": "stored"}
        return {"code": code,
                "metrics": indicators.metric_percentile_series(conn, code, years),
                "source": "computed"}
    finally:
        conn.close()


@router.get("/asset/{code}/score", summary="评分历史序列")
def asset_score(code: str, years: int = 10, _: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        rows = storage.load_scores(
            conn, code, start=deps.years_ago(years),
            fields=("date", "score", "score5", "status",
                    "pct_pe", "pct_pb", "pct_ps", "pct_pcf", "pct_dividend"))
        return {"code": code, "years": years, "items": rows}
    finally:
        conn.close()


@router.put("/asset/{code}/weights", summary="设置评分权重比例（目标不存在则新建）")
def asset_set_weights(code: str, body: WeightsBody,
                      _: None = Depends(deps.require_auth)):
    try:
        weights = deps.normalize_weights(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    conn = deps.conn()
    try:
        storage.upsert_target_weights(conn, code, weights)
        # 权重变了 → 用已存分位重算该标的历史综合评分（不重读 K 线，秒级）
        done = 0
        try:
            done = indicators.recompute_scores(conn, code)
            if not done:
                # 库里还没有该标的的分位历史 → 至少算一下最新一天
                t = config.target(code) or {}
                if t.get("ktype") == "index":
                    indicators.index_score(conn, code)
                elif t.get("ktype") == "portfolio":
                    indicators.portfolio_score(conn, code)
                else:
                    indicators.stock_score(conn, code)
                done = 1
        except Exception as e:                      # noqa: BLE001
            log.warning("%s 权重变更后评分重算失败：%s", code, e)
        return {"ok": True, "code": code, "weights": weights, "scored": bool(done)}
    finally:
        conn.close()
