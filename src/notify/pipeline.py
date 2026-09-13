# -*- coding: utf-8 -*-
"""跑批编排：抓数 → 指数估值聚合 → 评分 → 渲染 →（可选）发信。

这是原来 main.py 主流程的替代品，但不含任何"什么时候跑"的判断
（那部分在 src/web/jobs.py 里，由 APScheduler 触发）。三方共用同一套逻辑：

  * 定时任务   src/web/jobs.py 的 daily_estimate / alert_check
  * Web 接口   src/web/app.py 的 /api/report/*（页面按钮）
  * 命令行     python -m src.notify.pipeline --preview / --send

命令行用法
----------
  python -m src.notify.pipeline --preview            # 只出预览 HTML（默认 preview.html）
  python -m src.notify.pipeline --preview out.html   # 指定输出文件
  python -m src.notify.pipeline --send               # 立刻发一封（忽略交易日/时段判断）
  python -m src.notify.pipeline --send --no-sync     # 不发前先抓数，用库里已有数据
  python -m src.notify.pipeline --codes sh.000300    # 只处理指定标的
"""

import argparse
import logging
import os
import sys
import threading
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from src.config import config            # noqa: E402
from src.fetch_data import data_fetcher  # noqa: E402
from src.indicators import indicators    # noqa: E402
from src.notify import mailer, report    # noqa: E402
from src.storage import storage          # noqa: E402

log = logging.getLogger("pipeline")


# =====================================================================
# 全局跑批互斥
# =====================================================================
# 为什么要进程级锁：baostock 的会话是**进程级全局**的（模块里只有一个 socket），
# 两个并发会话会互相抢响应 → 脏数据写库。而且并发跑批还会重复发信。
# 场景：用户在页面上点"立即发送"，同时 18:05 的主跑也触发。
_RUN_LOCK = threading.Lock()

# 计算指标的轻量锁：计算是离线、幂等的，可以和拉取数据并发（30 分钟一次补缺）。
# 只防止两次计算任务自己叠跑（APScheduler max_instances=1 已兜底，这里再保险一层）。
_COMPUTE_LOCK = threading.Lock()

# 全量分位重建锁：长任务（几百个标的 × 全历史），手动触发，防重入。
_PERCENTILE_LOCK = threading.Lock()


def is_busy() -> bool:
    """当前是否有跑批在进行。"""
    return _RUN_LOCK.locked()


# =====================================================================
# 交易日判断（基于 trade_date 日历表，离线可查）
# =====================================================================
def is_trading_day(conn, date: str | None = None) -> bool | None:
    """是否交易日。日历缺该日期时返回 None（未知）。"""
    return storage.is_trading_day(conn, date or datetime.now().strftime("%Y-%m-%d"))


# =====================================================================
# 单目标拉数 + 计算（"拉取数据"按钮）
# =====================================================================
def sync_target(conn, code: str, ktype: str | None = None,
                full: bool = False, wait: bool = False) -> dict:
    """按目标拉数并实时计算落库：指数 / 组合 / 个股通用。

    流程（与需求一致）：
      指数     检查成分股是否到最新 → 补拉 → 拉指数K线 → 聚合五指标写回 → 评分
      组合     成分股补拉 → 组合K线(OHLCV+五指标)由成分股合成写回 → 评分
      个股     K线 + 分红 + 动态股息率写回 → 评分

    取数阶段先用 baostock 会话，计算阶段复用 indicators。全程在 _RUN_LOCK 内，
    避免与定时任务 / 其它手动触发争用 baostock 的进程级会话。
    """
    t = config.target(code)
    ktype = ktype or (t["ktype"] if t else None) or "stock"
    if ktype not in ("index", "portfolio", "stock"):
        raise ValueError(f"未知标的类型：{ktype}")

    t0 = datetime.now()
    got = _RUN_LOCK.acquire(blocking=wait)
    if not got:
        return {"ok": False, "busy": True,
                "message": "已有同步在进行中，请稍后再试"}
    try:
        conn = _ensure_conn(conn)
        out = {"ok": True, "code": code, "ktype": ktype,
               "started_at": t0.strftime("%Y-%m-%d %H:%M:%S")}

        # ---------- ① 取数（联网；只取数，不算）----------
        with data_fetcher.BaostockSession() as sess:
            # 交易日历缺失时先补（离线判断"最新"的基准）
            try:
                if data_fetcher.ensure_trade_calendar(conn, sess):
                    out["trade_calendar"] = "synced"
            except Exception as e:                          # noqa: BLE001
                log.warning("交易日历同步失败，改用成分股已有数据判断：%s", e)
            out["sync"] = data_fetcher.sync_target_data(conn, sess, code, ktype, full)

        # ---------- ② 计算（纯离线；不联网）----------
        out.update(indicators.compute_target(conn, code, ktype, full=full))

        out["elapsed"] = round((datetime.now() - t0).total_seconds(), 1)
        log.info("目标 %s（%s）同步完成：%s | 耗时 %.1fs",
                 code, ktype, out.get("sync"), out["elapsed"])
        return out
    finally:
        _RUN_LOCK.release()


def _ensure_conn(conn):
    """确保有可用连接（调用方可能传 None）。"""
    return conn if conn is not None else storage.get_conn()


# =====================================================================
# 定时任务一：拉取数据（data_sync，纯取数）
# =====================================================================
def sync_data(conn, wait: bool = False) -> dict:
    """拉取数据定时任务：单纯拉 baostock 数据并落库，不做任何计算。

    交易日历 / 上证指数 / 成分股 / 指数K线 / 个股K线(前复权+不复权收盘价) / 分红。
    动态股息率、指数估值聚合、组合K线合成、评分都由「计算指标」任务离线完成。
    全程在 _RUN_LOCK 内，避免与手动触发/其它任务争用 baostock 会话。
    """
    t0 = datetime.now()
    got = _RUN_LOCK.acquire(blocking=wait)
    if not got:
        return {"ok": False, "busy": True, "message": "已有任务在进行中，请稍后再试"}
    try:
        out = {"ok": True, "started_at": t0.strftime("%Y-%m-%d %H:%M:%S"),
               "sync": "ok"}
        try:
            data_fetcher.sync_all(conn, mode="auto")
        except Exception as e:                      # noqa: BLE001
            log.exception("拉取数据失败：%s", e)
            out["ok"] = False
            out["sync"] = f"failed: {e}"
        out["elapsed"] = round((datetime.now() - t0).total_seconds(), 1)
        log.info("data_sync 完成：同步=%s，耗时 %.1fs", out["sync"], out["elapsed"])
        return out
    finally:
        _RUN_LOCK.release()


# =====================================================================
# 定时任务二：计算指标（compute_indicators，纯离线）
# =====================================================================
def compute_indicators(conn, wait: bool = False, force: bool = False) -> dict:
    """计算指标定时任务：检查所有个股/指数/组合的分位，缺什么补什么（分阶段+并发）。

    纯离线计算（读 kline + dividend），不联网、不占 baostock 会话，
    因此可与拉取数据并发——拉取数据逐只写 K 线的过程中，这里每 30 分钟补一次
    已经落库的数据；被拉取数据重写而清空的股息率，会在下一轮补回来（幂等）。

    **数据没变就早退**：取数侧没写过东西（`sync_state` 指纹没变），而且上一轮
    跑完是干净的（没留下待办），就直接返回，省掉一天里几十次空转。
    `force=True`（手动点「执行」）时不做这个判断 —— 手动触发就是要真跑一次。

    实际阶段划分在 indicators.compute_all 里（个股并发 → 组合K线 → 指数/组合聚合）。
    """
    t0 = datetime.now()
    got = _COMPUTE_LOCK.acquire(blocking=wait)
    if not got:
        return {"ok": False, "busy": True, "message": "上一次计算指标仍在进行，跳过"}
    try:
        out = {"ok": True, "started_at": t0.strftime("%Y-%m-%d %H:%M:%S")}
        if not force:
            fp = storage.data_fingerprint(conn)
            if fp and storage.get_compute_fingerprint(conn) == fp:
                log.info("数据未变化（指纹 %s），跳过本轮重算", fp)
                return {"ok": True, "skipped": True, "fingerprint": fp,
                        "reason": "数据未变化，跳过重算", "elapsed": 0.0}
        try:
            r = indicators.compute_all(conn, save_score=True)
            for k, v in r.items():
                if k not in ("ok", "started_at"):
                    out[k] = v
            # 只有在"确实没留下待办"时才记指纹，否则下一轮必须继续做：
            # 记早了会把没补完的缺口一起跳过。
            if not out.get("lagging") and not storage.dirty_codes(conn):
                storage.set_compute_fingerprint(conn, out.get("fingerprint") or "")
            else:
                log.info("本轮仍留下待办（残缺 %s 只），不记指纹，下一轮继续",
                         out.get("lagging"))
        except Exception as e:                      # noqa: BLE001
            log.exception("计算指标失败：%s", e)
            out["ok"] = False
            out["error"] = str(e)
        out["elapsed"] = round((datetime.now() - t0).total_seconds(), 1)
        log.info("compute_indicators 完成：补股息率 %d 行，补历史分位 %d 只个股"
                 "（%d 行）+ %d 个组合，聚合 %d/%d，补最新分位 %d 个，"
                 "仍残缺 %d 只，耗时 %.1fs",
                 out.get("dividend_yield_filled", 0),
                 out.get("history_stocks", 0), out.get("history_stock_rows", 0),
                 out.get("history_agg", 0),
                 out.get("agg_ok", 0), out.get("agg_total", 0),
                 out.get("percentiles", 0), out.get("lagging", 0),
                 out["elapsed"])
        return out
    finally:
        _COMPUTE_LOCK.release()


def _mail_summary(items: list) -> str:
    return "；".join(f"{i['name']} {i['status']} {i['score']:.0f}分" for i in items)


def _log_mail(conn, subject, summary, receivers, kind, ok, body_key=None):
    """写一条历史邮件记录（写失败不影响发信本身，只记警告）。"""
    try:
        storage.add_mail_log(conn, subject, summary, receivers,
                             kind=kind, ok=ok, body_key=body_key)
    except Exception as e:                          # noqa: BLE001
        log.warning("写邮件日志失败：%s", e)


def _send_logged(conn, subject, html, images, receivers, kind, summary, body_key):
    """发送 + 记录结果。成功/失败都落一条 mail_log，首页才能看到发送状态。"""
    try:
        r = mailer.send_mail(subject, html, images, receivers)
    except Exception as e:                          # noqa: BLE001
        _log_mail(conn, subject, summary, receivers, kind, ok=False, body_key=body_key)
        raise
    _log_mail(conn, subject, summary, receivers, kind, ok=True, body_key=body_key)
    return r


# =====================================================================
# 全量分位重建（手动：命令行 / 网页按钮）
# =====================================================================
def rebuild_busy() -> bool:
    """是否有全量分位重建在进行（供接口判断重复触发）。"""
    return _PERCENTILE_LOCK.locked()


def rebuild_percentiles(conn, freq: str = "D", clean: bool = True,
                        wait: bool = False, progress=None, workers=None,
                        dividend_full: bool = True) -> dict:
    """重建所有个股 + 指数的历史分位（10 年 / 5 年双窗口）。

    先多进程并行重算个股（动态股息率全量 + 分位），再聚合指数；综合评分只在
    配置了权重时算。长任务，用于首次建库 / 补历史；日常由计算指标任务增量维护。
    """
    t0 = datetime.now()
    if not _PERCENTILE_LOCK.acquire(blocking=wait):
        return {"ok": False, "busy": True, "message": "已有分位重建在进行中，请稍后"}
    try:
        # 与「计算指标」互斥：两者都会写 valuation_score，不能同时跑
        if not _COMPUTE_LOCK.acquire(blocking=wait):
            return {"ok": False, "busy": True,
                    "message": "计算指标任务进行中，请稍后再重建分位"}
        try:
            r = indicators.rebuild_all_percentiles(
                conn, freq=freq, clean=clean, progress=progress,
                workers=workers, dividend_full=dividend_full)
            r["ok"] = True
            r["elapsed"] = round((datetime.now() - t0).total_seconds(), 1)
            return r
        finally:
            _COMPUTE_LOCK.release()
    finally:
        _PERCENTILE_LOCK.release()


# =====================================================================
# 定时任务二：通知（构建 → 发送 → 重发）
# =====================================================================
def build_notification(conn, force: bool = False) -> dict:
    """读库中**已算好的**评分，生成并暂存邮件正文（不发送，也不计算）。

    交易日守卫**不在这里**：统一放在 `web/jobs.py`（与其它任务一致），
    这样"哪个任务什么时候跳过"只有一处判断。`force` 仅用于日志/兼容。

    通知任务不做任何计算 —— 股息率/指数估值/组合K线/评分都由「计算指标」任务负责。
    """
    # 先清理过期正文，防止磁盘占满
    days = int(config.schedule()["mail_retention_days"])
    purged = storage.purge_old_pending(conn, days)
    purged += storage.purge_old_mail_body(conn, days)

    # 这里**不做计算**：股息率/指数估值/组合K线/评分全部由「计算指标」任务
    # （每 INDICATORS_INTERVAL_MINUTES 跑一次）负责，通知任务只读库渲染。
    # 职责边界：取数(data_fetcher) / 计算(indicators) / 通知(本模块) 三层各管一段。
    rep = report.build_report(conn)
    if rep.get("empty"):
        return {"ok": False, "reason": "无评分数据，跳过通知构建",
                "purged": purged}
    build_date = datetime.now().strftime("%Y-%m-%d")
    summary = _mail_summary(rep["items"])
    receivers = config.smtp()["receivers"]
    storage.save_pending_mail(conn, build_date, rep["subject"], summary,
                              rep["html"], receivers, rep["has_alert"])
    # 内联图片必须一起存：正文 HTML 里是 cid: 引用，发送/重发时要把图片作为
    # 附件带上，否则收件人看到的 8 张图表全是破图。
    storage.save_pending_images(conn, build_date, rep["images"])
    # 另存一份**自包含**正文（图片内联 base64）：mailer 发的是 cid: 附件版，
    # 浏览器打不开；首页点击邮件时要能原样回看，所以在这里就把它存好。
    storage.save_mail_body(conn, build_date, rep["subject"],
                           report.inline_images(rep["html"], rep["images"]))
    log.info("通知正文已暂存：%s（告警=%s，清理过期 %d 封）",
             rep["subject"], rep["has_alert"], purged)
    return {"ok": True, "subject": rep["subject"], "is_alert": rep["has_alert"],
            "items": len(rep["items"]), "purged": purged}


def send_notification(conn) -> dict:
    """08:30：发送当天暂存的通知邮件（首次发送）。"""
    build_date = datetime.now().strftime("%Y-%m-%d")
    pending = storage.load_pending_mail(conn, build_date)
    if not pending:
        return {"ok": False, "reason": f"{build_date} 没有暂存的邮件（先跑构建）"}
    if pending["sent_count"] > 0:
        return {"ok": False, "reason": "当天已发送过，跳过"}
    to = pending["receivers"].split(", ") if pending["receivers"] else []
    _send_logged(conn, pending["subject"], pending["html"],
                 storage.load_pending_images(conn, build_date), to,
                 kind="daily", summary=pending["summary"], body_key=build_date)
    storage.bump_pending_sent(conn, build_date)
    log.info("通知已发送：%s", pending["subject"])
    return {"ok": True, "subject": pending["subject"],
            "sent_count": 1, "is_alert": bool(pending["is_alert"])}


def resend_alert(conn) -> dict:
    """11:30/18:00/21:00：若当天邮件是告警，则重发（合计最多 4 次）。"""
    build_date = datetime.now().strftime("%Y-%m-%d")
    pending = storage.load_pending_mail(conn, build_date)
    if not pending:
        return {"ok": False, "reason": "当天没有暂存邮件"}
    if not pending["is_alert"]:
        return {"ok": False, "reason": "非告警邮件，无需重发"}
    max_sends = 1 + 3                       # 首次 + 重发 3 次
    if pending["sent_count"] >= max_sends:
        return {"ok": False, "reason": f"已达最大发送次数 {max_sends}，跳过"}
    to = pending["receivers"].split(", ") if pending["receivers"] else []
    _send_logged(conn, pending["subject"], pending["html"],
                 storage.load_pending_images(conn, build_date), to,
                 kind="alert", summary=pending["summary"], body_key=build_date)
    n = storage.bump_pending_sent(conn, build_date)
    log.info("告警重发（第 %d 次）：%s", n, pending["subject"])
    return {"ok": True, "subject": pending["subject"], "sent_count": n}



def ensure_constituent_data(conn, codes, wait: bool = False) -> dict:
    """把给定成分股的「K线 / 分红 / 元数据」补到最新（**只补缺的**）。

    编排（取数与计算分层）：
      ① 取数 —— data_fetcher.sync_constituents_data（联网，只补缺的）
      ② 计算 —— 新拉回来的 K 线补动态股息率（indicators，纯离线）

    全程在 _RUN_LOCK 内，避免与「拉取数据」任务争用 baostock 会话。
    """
    if not codes:
        return {"checked": 0, "synced": [], "meta": 0, "meta_checked": 0,
                "failed": []}
    got = _RUN_LOCK.acquire(blocking=wait)
    if not got:
        return {"busy": True, "checked": len(codes), "synced": [],
                "meta": 0, "meta_checked": 0, "failed": []}
    try:
        info = data_fetcher.sync_constituents_data(conn, codes)
        # 计算：新拉回来的 K 线要补动态股息率（离线）
        if info.get("synced"):
            indicators.fill_dividend_yields(conn, info["synced"])
    finally:
        _RUN_LOCK.release()
    info["busy"] = False
    return info


def rebuild_portfolio(conn, code: str, sync: bool = True,
                      freq: str = "M") -> dict:
    """「重算组合」：**先把成分股数据补齐，再删旧数据全量重算**。

    ① 检查成分股的 K 线/分红/元数据是否最新，落后就增量拉取
    ② 删除该组合已算好的 K 线 + 分位/评分
    ③ 重新合成组合K线 → 重建历史分位/评分 → 当日综合评分

    返回 {sync, kline_rows, score_rows, score, ...}。
    """
    codes = storage.load_constituents(conn, code)
    if not codes:
        return {"kline_rows": 0, "score_rows": 0, "score": None,
                "constituents": 0, "sync": None}
    info = ensure_constituent_data(conn, codes) if sync else None
    rb = indicators.rebuild_portfolio(conn, code, freq=freq)
    rb["sync"] = info
    return rb


# =====================================================================
# 跑批
# =====================================================================
def run(conn, sync: bool | None = None, send: bool = False,
        only_alerts: bool = False, preview: str | None = None,
        codes=None, years=None, rebuild_valuation: bool = True,
        wait: bool = False, kind: str = "manual") -> dict:
    """完整跑批（同一进程内串行：拿不到锁直接返回 busy，避免并发抢 baostock 会话）。

    wait=True 时排队等待（命令行用），False（默认）时立即返回 busy。

    sync              True/False 强制；None 时按配置 SYNC_BEFORE_SCORE 决定
    send              是否发邮件（False = 只算不发，返回报告内容）
    only_alerts       True 时仅当有标的进入告警态才发信（盘中检查用）
    preview           传入路径则额外写一份自包含预览 HTML
    codes             只处理指定标的（默认取全部启用标的）
    rebuild_valuation 是否重建指数估值绝对值（方案A），默认重建
    """
    t0 = datetime.now()
    got = _RUN_LOCK.acquire(blocking=wait)
    if not got:
        log.warning("已有跑批在进行中，本次跳过（避免并发抓数与重复发信）")
        return {"ok": False, "busy": True, "steps": {"lock": "busy"},
                "sent": False, "subject": "",
                "started_at": t0.strftime("%Y-%m-%d %H:%M:%S"),
                "message": "已有跑批在进行中，请稍后再试"}
    try:
        return _run_locked(conn, sync=sync, send=send, only_alerts=only_alerts,
                           preview=preview, codes=codes, years=years,
                           rebuild_valuation=rebuild_valuation, kind=kind, t0=t0)
    finally:
        _RUN_LOCK.release()


def _run_locked(conn, sync, send, only_alerts, preview, codes, years,
                rebuild_valuation, kind, t0) -> dict:
    """真正干活的部分（调用方已持有 _RUN_LOCK）。"""
    out = {"ok": True, "steps": {}, "sent": False, "subject": "",
           "started_at": t0.strftime("%Y-%m-%d %H:%M:%S")}

    # ---------- ① 抓数 ----------
    do_sync = config.runtime()["sync_before_score"] if sync is None else bool(sync)
    if do_sync:
        try:
            data_fetcher.sync_all(conn, mode="auto")
            out["steps"]["sync"] = "ok"
        except Exception as e:                              # noqa: BLE001
            log.exception("增量抓数失败，继续用库内已有数据计算：%s", e)
            out["steps"]["sync"] = f"failed: {e}"
    else:
        out["steps"]["sync"] = "skipped"

    # ---------- ② 计算指标（个股并发补历史 → 组合K线 → 指数/组合聚合）----------
    r = indicators.compute_all(conn, save_score=True,
                               rebuild_valuation=rebuild_valuation)
    out["steps"]["score"] = (
        f"补历史分位 {r.get('history_stocks', 0)} 只个股 + "
        f"{r.get('history_agg', 0)} 个组合，聚合 {r.get('agg_ok', 0)}/"
        f"{r.get('agg_total', 0)}")
    if not r.get("agg_ok"):
        out["ok"] = False
        out["steps"]["score"] = "无可用标的（缺少成分股或K线数据）"
        log.error("没有任何标的完成评分，本次不发信")
        return out

    # ---------- ④ 渲染 ----------
    rep = report.build_report(conn, codes=codes, years=years)
    if rep.get("empty"):
        out["ok"] = False
        out["steps"]["report"] = "无评分数据可渲染"
        return out
    out["subject"] = rep["subject"]
    out["alert_names"] = rep["alert_names"]
    out["items"] = [{"code": i["code"], "name": i["name"], "score": i["score"],
                     "score5": i["score5"], "status": i["status"],
                     "action": i["action"], "alert": i["alert"]}
                    for i in rep["items"]]
    out["steps"]["report"] = f"{len(rep['items'])} 个标的，{len(rep['images'])} 张图"

    # ---------- ⑤ 预览 ----------
    if preview:
        html = report.inline_images(rep["html"], rep["images"])
        os.makedirs(os.path.dirname(os.path.abspath(preview)), exist_ok=True)
        with open(preview, "w", encoding="utf-8") as f:
            f.write(html)
        out["preview"] = os.path.abspath(preview)
        log.info("预览 HTML 已生成：%s", out["preview"])

    # ---------- ⑥ 发信 ----------
    if not send:
        out["steps"]["mail"] = "未发送（send=False）"
        return _finish(out, t0)

    if only_alerts:
        # 告警去重：盘中会检查多次，但日线数据当天只更新一次，
        # 不去重的话同一天会连发好几封内容完全一样的信。
        # 规则：① 当天还没发过告警信 → 发；② 告警标的集合变了 → 发；否则跳过。
        if not rep["has_alert"]:
            out["steps"]["mail"] = "无告警，跳过发送"
            log.info("本次无标的进入告警态，跳过发送")
            return _finish(out, t0)
        sig = ";".join(sorted(f"{i['code']}:{i['status']}"
                              for i in rep["items"] if i["alert"]))
        today = datetime.now().strftime("%Y-%m-%d")
        if (sig == config.get_str("LAST_ALERT_SIG", "")
                and config.get_str("LAST_ALERT_DATE", "") == today):
            out["steps"]["mail"] = "告警状态未变化，跳过发送（当天已发过）"
            log.info("告警状态与上次相同且当天已发过信，跳过发送")
            return _finish(out, t0)
        config.set("LAST_ALERT_SIG", sig, group="runtime",
                   remark="最近一次告警信的状态签名（程序自动写入）")
        config.set("LAST_ALERT_DATE", today, group="runtime",
                   remark="最近一次告警信日期（程序自动写入）")

    # 手动发送是"这一次"的独立快照，用时间戳做 key（同一天可发多次，不覆盖）；
    # 快照要在发送前存好，否则发送失败就没有正文可查了。
    now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = _mail_summary(rep["items"])
    body_key = "manual-" + now_txt
    try:
        storage.save_mail_body(conn, body_key, rep["subject"],
                               report.inline_images(rep["html"], rep["images"]))
    except Exception as e:                          # noqa: BLE001
        log.warning("写邮件正文快照失败：%s", e)
    _send_logged(conn, rep["subject"], rep["html"], rep["images"], None,
                 kind=kind, summary=summary, body_key=body_key)
    out["sent"] = True
    out["steps"]["mail"] = "已发送"
    config.set("LAST_MAIL_AT", now_txt, group="runtime",
               remark="最近一次发信时间（程序自动写入）")
    config.set("LAST_MAIL_SUBJECT", rep["subject"], group="runtime",
               remark="最近一次邮件标题（程序自动写入）")
    return _finish(out, t0)


def _finish(out: dict, t0: datetime) -> dict:
    """统一收尾：记耗时与最近跑批时间。"""
    out["elapsed"] = round((datetime.now() - t0).total_seconds(), 1)
    try:
        config.set("LAST_RUN_AT", out["started_at"], group="runtime",
                   remark="最近一次跑批时间（程序自动写入）")
    except Exception as e:                                  # noqa: BLE001
        log.warning("记录跑批时间失败：%s", e)
    log.info("跑批完成：%s | 耗时 %.1fs | 发送=%s | 标题=%s",
             out["steps"], out["elapsed"], out["sent"], out["subject"])
    return out


# =====================================================================
# 命令行
# =====================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="估值评分跑批（抓数→评分→渲染→发信）")
    ap.add_argument("--preview", nargs="?", const="preview.html", default=None,
                    help="只生成预览 HTML（默认 preview.html），绝不发邮件")
    ap.add_argument("--send", action="store_true",
                    help="立刻发信（忽略交易日/时段判断，用于验证 SMTP）")
    ap.add_argument("--no-sync", action="store_true", help="跳过抓数，只用库里已有数据")
    ap.add_argument("--no-valuation", action="store_true", help="跳过指数估值聚合（方案A）")
    ap.add_argument("--codes", default=None, help="只处理指定标的，逗号分隔")
    ap.add_argument("--years", type=int, default=None, help="图表展示窗口（年）")
    ap.add_argument("--rebuild-percentiles", action="store_true",
                    help="全量重建所有个股/指数的历史分位（日频），不跑评分/发信")
    ap.add_argument("--freq", default="D", choices=["D", "W", "M"],
                    help="--rebuild-percentiles 的采样频率（默认 D 每交易日）")
    ap.add_argument("--no-clean", action="store_true",
                    help="--rebuild-percentiles 时不先清空已有记录（默认清空）")
    ap.add_argument("--workers", type=int, default=None,
                    help="--rebuild-percentiles 的并行进程数（默认按 CPU 自动）")
    ap.add_argument("--no-dividend-full", action="store_true",
                    help="--rebuild-percentiles 时不顺带全量重算动态股息率")
    ap.add_argument("--db", default=None, help="数据库路径，默认 config.DB_PATH")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.use_db(args.db)

    codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
    conn = storage.get_conn()
    try:
        if args.rebuild_percentiles:
            def _p(done, total):
                print(f"  分位重建进度 {done}/{total}", flush=True)
            r = rebuild_percentiles(conn, freq=args.freq,
                                    clean=not args.no_clean, wait=True,
                                    progress=_p, workers=args.workers,
                                    dividend_full=not args.no_dividend_full)
            print(f"完成：{r.get('ok')}/{r.get('codes')} 个标的，{r.get('rows')} 行，"
                  f"{r.get('workers')} 进程，耗时 {r.get('elapsed')}s"
                  f"（频率 {r.get('freq')}）")
            return 0 if r.get("ok") else 1
        r = run(conn, sync=(False if args.no_sync else None), send=args.send,
                preview=args.preview, codes=codes, years=args.years,
                rebuild_valuation=not args.no_valuation)
    finally:
        conn.close()

    if args.preview:
        print(f"预览文件：{r.get('preview')}")
    print(f"标题：{r.get('subject')}")
    print(f"步骤：{r.get('steps')}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
