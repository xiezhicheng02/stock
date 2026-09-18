# -*- coding: utf-8 -*-
"""首页接口：上证指数行情 + 系统统计 + 手动触发定时任务。

接口
----
* GET  /api/dashboard        上证指数最新行情+近1年K线、系统统计
* POST /api/tasks/daily      手动触发日频任务（后台线程）
* POST /api/tasks/week       手动触发周频任务
* POST /api/tasks/backfill   手动触发 kline 补缺口任务
"""

import logging
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from src.config import config
from src.storage.storage import Storage
from src.model.Kline import Kline
from src.model.StockBasic import StockBasic

log = logging.getLogger("web.dashboard")
router = APIRouter(prefix="/api", tags=["dashboard"])

MARKET_INDEX = "sh.000001"
MARKET_INDEX_NAME = "上证指数"

#: 任务是否在跑（进程内标志，避免重复触发）
_busy: dict[str, bool] = {}


def _market(st: Storage) -> dict:
    """上证指数最新行情 + 近 1 年日 K（供首页图表）。"""
    start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    rows = st.load(Kline, code=MARKET_INDEX, start=start, end=today)
    if not rows:
        return {"code": MARKET_INDEX, "name": MARKET_INDEX_NAME,
                "latest": None, "kline": [], "rows": 0}
    latest = rows[-1]
    kline = [{
        "date": r.date, "open": r.open, "high": r.high,
        "low": r.low, "close": r.close, "volume": r.volume,
        "pctChg": r.pctChg,
    } for r in rows]
    closes = [r.close for r in rows if r.close is not None]
    trend = {}
    for n in (5, 20, 60):
        if len(closes) > n and closes[-1 - n]:
            trend[f"chg_{n}d"] = round((closes[-1] / closes[-1 - n] - 1) * 100, 2)
        else:
            trend[f"chg_{n}d"] = None
    return {
        "code": MARKET_INDEX, "name": MARKET_INDEX_NAME,
        "latest": {
            "date": latest.date, "close": latest.close,
            "open": latest.open, "high": latest.high, "low": latest.low,
            "preclose": latest.preclose, "volume": latest.volume,
            "amount": latest.amount, "pctChg": latest.pctChg,
        },
        "kline": kline, "rows": len(rows), **trend,
    }


def _stats(st: Storage) -> dict:
    """系统统计：kline 各类型数量、stock_basic 数量、数据日期范围。"""
    conn = st.conn
    by_ktype = dict(conn.execute(
        "SELECT ktype, COUNT(DISTINCT code) FROM kline GROUP BY ktype").fetchall())
    n_basic = conn.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
    n_factor = conn.execute("SELECT COUNT(*) FROM adjust_factor").fetchone()[0]
    lo, hi = conn.execute(
        "SELECT MIN(date), MAX(date) FROM kline").fetchone()
    return {
        "stocks": by_ktype.get("stock", 0),
        "etfs": by_ktype.get("etf", 0),
        "indexes": by_ktype.get("index", 0),
        "kline_total_codes": sum(by_ktype.values()),
        "stock_basics": n_basic,
        "adjust_factors": n_factor,
        "kline_range": {"start": lo, "end": hi},
        "busy": dict(_busy),
    }


@router.get("/dashboard", summary="首页总览")
def dashboard():
    """上证指数行情 + 系统统计。"""
    st = Storage()
    try:
        st.ensure_schema()
        return {"market": _market(st), "stats": _stats(st)}
    finally:
        st.close()


def _run_task(name: str, fn):
    """后台线程跑任务，设 busy 标志。"""
    _busy[name] = True
    try:
        log.info("手动触发 %s", name)
        result = fn()
        log.info("%s 完成: %s", name, result)
    except Exception as e:  # noqa: BLE001
        log.exception("%s 失败: %s", name, e)
    finally:
        _busy[name] = False


def _trigger(name: str, fn) -> dict:
    """后台触发任务，已在跑则 409。"""
    if _busy.get(name):
        raise HTTPException(status_code=409, detail=f"{name} 正在运行中")
    t = threading.Thread(target=_run_task, args=(name, fn), daemon=True)
    t.start()
    return {"ok": True, "task": name, "started": True}


@router.post("/tasks/daily", summary="手动触发日频任务")
def trigger_daily():
    from src.schedule.daily_task import DailyTask
    return _trigger("daily", lambda: DailyTask().run())


@router.post("/tasks/week", summary="手动触发周频任务")
def trigger_week():
    from src.schedule.week_task import WeekTask
    return _trigger("week", lambda: WeekTask().run())


@router.post("/tasks/backfill", summary="手动触发 kline 补缺口")
def trigger_backfill():
    from src.schedule.kline_backfill_task import KlineBackfillTask
    return _trigger("backfill", lambda: KlineBackfillTask().run())
