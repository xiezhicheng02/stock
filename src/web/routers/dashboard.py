# -*- coding: utf-8 -*-
"""首页总览接口：上证指数行情 + 系统运行状态（告警/邮件/任务/拉数/统计）。

上证指数（sh.000001）是"大盘参考"，不算估值指标、不参与评分，只展示行情K线。
"""

import logging
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from src.config import config
from src.fetch_data import data_fetcher
from src.notify import pipeline
from src.storage import storage
from src.web import deps
from src.web import scheduler as sched

log = logging.getLogger("web.dashboard")
router = APIRouter(prefix="/api", tags=["dashboard"])

MARKET_INDEX = "sh.000001"          # 上证指数（仅行情展示，不估值）
MARKET_INDEX_NAME = "上证指数"

#: 首页统计的进程内短缓存 {key: (过期时间戳, 值)}。
#: 单进程（调度器与 web 同进程、固定单 worker），所以进程内缓存就够。
_CACHE: dict = {}


def _trend(closes):
    """用收盘价序列算 5/20/60 日涨跌幅（不足则 None）。"""
    out = {}
    for n in (5, 20, 60):
        if len(closes) > n and closes[-1 - n]:
            out[f"chg_{n}d"] = round((closes[-1] / closes[-1 - n] - 1) * 100, 2)
        else:
            out[f"chg_{n}d"] = None
    return out


def _market(conn) -> dict:
    """上证指数最新行情 + 近1年K线 + 5/20/60日涨跌。

    带上 preclose：首页要显示涨跌额、以及今开/最高/最低相对昨收的红绿着色。
    """
    rows = storage.load_kline(conn, MARKET_INDEX, start=deps.years_ago(1),
                              fields=("date", "open", "high", "low", "close",
                                      "preclose", "volume", "amount", "pct_chg"))
    latest = rows[-1] if rows else {}
    closes = [r.get("close") for r in rows if r.get("close") is not None]
    return {"code": MARKET_INDEX, "name": MARKET_INDEX_NAME,
            "latest": latest, "kline": rows, "rows": len(rows),
            "total_rows": storage.count_kline(conn, MARKET_INDEX),
            **_trend(closes)}


def _stats(conn) -> dict:
    """数据统计总览（**带短缓存**）。

    这里三个查询都是"全表统计"：`COUNT(DISTINCT code)` 扫 316 万行、
    两个 `GROUP BY code` 各扫 300 万行左右，本机合计 ~1.9s、树莓派上 10 秒以上。
    而它们的结果只在**拉取/计算任务写库**时才变 —— 首页却每 30 秒自动刷新一次，
    等于拿最贵的查询去喂最高频的刷新。

    所以按 `(data_fingerprint, WEB_STATS_CACHE_SEC)` 做进程内缓存：
      * 指纹变了（取数任务写过库）→ 立即失效，不用等 TTL；
      * TTL 兜住"计算任务写的"变化（评分/分位，指纹反映不出来）。
    设 WEB_STATS_CACHE_SEC=0 可关闭缓存。
    """
    ttl = max(0, config.get_int("WEB_STATS_CACHE_SEC", 60))
    key = ("stats", storage.data_fingerprint(conn), ttl)
    if ttl:
        now = time.time()
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    targets = config.targets(only_enabled=False)
    n_index = sum(1 for t in targets if t["ktype"] == "index")
    n_pf = sum(1 for t in targets if t["ktype"] == "portfolio")
    kd = storage.latest_kline_dates(conn)
    sd = storage.latest_score_dates(conn)
    lo, hi = storage.trade_date_range(conn)
    out = {
        "stocks": storage.count_stocks(conn),
        "indexes": n_index,
        "portfolios": n_pf,
        "kline_codes": len(kd),
        "score_codes": len(sd),
        "trade_calendar": {"start": lo, "end": hi},
        "last_run_at": config.get_str("LAST_RUN_AT", "") or None,
    }
    if ttl:
        _CACHE[key] = (time.time() + ttl, out)
        # 顺手清掉过期项，避免 key 里带指纹导致无限增长
        for k, v in list(_CACHE.items()):
            if v[0] < time.time():
                _CACHE.pop(k, None)
    return out


def _alert(conn) -> dict:
    """告警状态：当前处于告警态的标的 + 最近告警时间。

    只遍历**标的信息里的标的**：只有它们才有 `score`（其余 260 万行 score 为空、
    只落分位），遍历全部 860 个代码纯属白跑 —— 还会顺带调 860 次 `config.target()`。
    实测这一步原来要 ~1.3s（本机）。

    告警状态按**当前配置**实时推导（config.signal_of），不读 valuation_score
    里的 status 快照——否则改了 SIGNAL_BANDS / ALERT_STATUSES 要等全量重算才生效。
    """
    alert_targets = []
    for t in config.targets(only_enabled=False):
        sc = storage.latest_score(conn, t["code"])
        if not sc or sc.get("score") is None:
            continue
        sig = config.signal_of(sc["score"])
        if not sig["alert"]:
            continue
        alert_targets.append({"code": t["code"], "name": t["name"],
                              "status": sig["status"], "score": sc["score"],
                              "color": sig["color"],
                              "action": sig["short"] or sig["action"],
                              "date": sc["date"]})
    return {
        "alert_targets": alert_targets,
        "last_alert_at": config.get_str("LAST_ALERT_DATE", "") or None,
        "last_alert_sig": config.get_str("LAST_ALERT_SIG", "") or None,
    }


def _mail(conn) -> dict:
    """邮件：最近一封 + 历史列表（含发送状态与正文快照标记）+ 今天的推送状态。

    pending：今天已构建但**还没发送**的那一封。首页「生成邮件正文」点完就能在
    邮件列表里看到它（标为"待发送"）并预览正文，不用等到 08:30 真的发出去。
    """
    history = storage.load_mail_log(conn, limit=50)
    # "最近发送"以 mail_log 为准（三条发送路径都会写它）；LAST_MAIL_AT 只有
    # 手动跑批会写，定时任务发的信在那里是空的，不能拿来当唯一来源。
    last = history[0] if history else {}
    today = datetime.now().strftime("%Y-%m-%d")
    pending = storage.load_pending_mail(conn, today) or {}
    max_sends = 1 + 3                       # 首次 + 3 次重发（与 pipeline 保持一致）
    sent_count = int(pending.get("sent_count") or 0)
    pending_item = None
    if pending and sent_count == 0:
        key = pending.get("build_date")
        pending_item = {
            "body_key": key,
            "built_at": pending.get("created_at"),
            "subject": pending.get("subject"),
            "summary": pending.get("summary"),
            "receivers": pending.get("receivers"),
            "is_alert": bool(pending.get("is_alert")),
            "has_body": bool(storage.load_mail_body(conn, key)) if key else False,
        }
    # 今天"没构建"时，把构建任务上次的结果也带出去 —— 否则用户点了「生成邮件正文」
    # 只看到邮件列表空着，不知道是被"非交易日跳过"挡掉了还是出错了。
    last_build = (config.get_json("SCHEDULER_LAST_RUNS", {}) or {}).get("notify_build")
    if pending_item:
        last_build = None
    return {
        "last_mail_at": last.get("sent_at")
                        or config.get_str("LAST_MAIL_AT", "") or None,
        "last_mail_subject": last.get("subject")
                             or config.get_str("LAST_MAIL_SUBJECT", "") or None,
        "history": history,
        "pending": pending_item,
        # 今天这一封的推送状态：构建了吗 / 发了几次 / 是不是告警
        "today": {
            "date": today,
            "built": bool(pending),
            "subject": pending.get("subject"),
            "is_alert": bool(pending.get("is_alert")),
            "sent_count": sent_count,
            "max_sends": max_sends,
            "last_send": last.get("sent_at"),
            "last_build": last_build,
        },
    }


@router.get("/dashboard", summary="首页总览")
def dashboard(_: None = Depends(deps.require_auth)):
    conn = deps.conn()
    try:
        return {
            "market": _market(conn),
            "stats": _stats(conn),
            "alert": _alert(conn),
            "mail": _mail(conn),
            "scheduler": sched.status(),
            "jobs": sched.jobs_info(),
            "recent_runs": sched.recent(10),
            "is_trading_today": pipeline.is_trading_day(
                conn, datetime.now().strftime("%Y-%m-%d")),
        }
    finally:
        conn.close()


@router.post("/market/sync", summary="拉取上证指数K线 + 交易日历")
def market_sync(full: bool = False, _: None = Depends(deps.require_auth)):
    """拉取上证指数行情K线（不算估值，只用于首页展示），并顺带同步交易日历。"""
    if pipeline.is_busy():
        raise HTTPException(status_code=409, detail="已有同步在进行中，请稍后再试")
    conn = deps.conn()
    try:
        with data_fetcher.BaostockSession() as sess:
            n = data_fetcher.sync_kline(conn, sess, MARKET_INDEX, "index", full)
            try:
                n_cal = data_fetcher.sync_trade_dates(conn, sess, full=False)
            except Exception as e:              # noqa: BLE001
                log.warning("交易日历同步失败：%s", e)
                n_cal = 0
        return {"ok": True, "code": MARKET_INDEX, "kline": n, "trade_calendar": n_cal}
    except Exception as e:                      # noqa: BLE001
        log.exception("上证指数拉取失败")
        raise HTTPException(status_code=400, detail=f"拉取失败：{deps.safe_msg(e)}")
    finally:
        conn.close()
