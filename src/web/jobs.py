# -*- coding: utf-8 -*-
"""定时任务定义（任务本体 + 登记表）。

三个核心任务（触发时间可在设置页配置）：
  1. data_sync          拉取数据：每天 SYNC_RUN_TIME（默认 18:00）跑，纯取数落库
  2. compute_indicators 计算指标：每 INDICATORS_INTERVAL_MINUTES（默认 30 分钟）跑，
                         检查缺失的股息率/指数估值/组合K线并补算，再算综合评分（离线）
  3. notify             通知：NOTIFY_BUILD_TIME（03:00）构建并暂存正文
                         → NOTIFY_SEND_TIME（08:30）发送
                         → 若告警，在 NOTIFY_RESEND_TIMES（11:30/18:00/21:00）重发

"跑什么"在 src/notify/pipeline.py 与 src/indicators/indicators.py；
"什么时候跑"由本模块依据 config.schedule() 生成触发器。
通知拆成 3 个独立函数（构建/发送/重发），手动「执行」时直接执行对应动作，不依赖当前时间。
"""

import logging
import threading
from datetime import datetime

from src.config import config
from src.notify import pipeline
from src.storage import storage

log = logging.getLogger("jobs")

# ---------------------------------------------------------------------
# 手动触发的"强制执行"标记
# ---------------------------------------------------------------------
# 定时任务里有几处保护性判断（例如"非交易日不构建邮件"）。这些判断对**定时**
# 执行是对的，但用户手动点「执行」是明确的意图，应该真的跑一遍 —— 否则周末点
# "生成邮件正文"只会得到一句"今天不是交易日，跳过构建"，看起来像功能坏了。
_force_once: set = set()
_force_lock = threading.Lock()


def request_force(job_id: str) -> None:
    """手动触发时调用：让这个任务的下一次执行忽略保护性判断。"""
    # notify_resend_1/2/3 共用 notify_resend 函数，标记归一化
    base = "notify_resend" if job_id.startswith("notify_resend_") else job_id
    with _force_lock:
        _force_once.add(base)


def _take_force(job_id: str) -> bool:
    """取出（并清掉）该任务的一次性强制标记。"""
    with _force_lock:
        if job_id in _force_once:
            _force_once.discard(job_id)
            return True
    return False


def _holiday_skip() -> str | None:
    """非交易日就跳过定时任务（按 baostock 同步下来的股市日历判断）。

    为什么不用"星期范围"（原来那个 RUN_WEEKDAYS）：A 股有**周末调休交易日**，
    单纯按 mon-fri 会漏掉它们；反过来节假日（国庆/春节连休）也不一定是周末。
    交易日历是从 baostock query_trade_dates 同步下来的 `trade_date` 表，
    直接查它最准。

    返回值：该跳过时返回原因文案，该跑返回 None。
    * 日历里没有今天（未知）→ **不拦**，正常跑（宁可多跑一次也别漏）
    * SKIP_NON_TRADING_DAY 关掉 → 每天都跑
    """
    if not config.get_bool("SKIP_NON_TRADING_DAY", True):
        return None
    try:
        conn = _conn()
        try:
            trading = pipeline.is_trading_day(conn)
        finally:
            conn.close()
    except Exception as e:                          # noqa: BLE001
        log.warning("交易日判断失败，按正常执行：%s", e)
        return None
    if trading is False:
        return "今天不是交易日（按股市日历），跳过"
    return None


def _conn():
    """任务运行在调度器线程里，连接必须自建自关（不能跨线程复用）。"""
    return storage.get_conn()


# =====================================================================
# 任务本体
# =====================================================================
def data_sync():
    """拉取数据（纯取数）：每天收盘后跑一次，只拉 baostock 数据落库。

    是否真的跑由**股市日历**决定（见 _holiday_skip）；手动触发会强制跑。
    """
    if not _take_force("data_sync"):
        why = _holiday_skip()
        if why:
            return why
    conn = _conn()
    try:
        r = pipeline.sync_data(conn)
        if r.get("busy"):
            return "已有任务在进行中，跳过"
        if not r["ok"]:
            raise RuntimeError(str(r))
        return f"同步={r.get('sync')}，耗时 {r.get('elapsed', 0)}s"
    finally:
        conn.close()


def compute_indicators():
    """计算指标（离线）：补股息率/指数估值/组合K线，再算综合评分。

    非交易日没有新数据可补，同样按股市日历跳过；手动触发会强制跑。
    """
    if not _take_force("compute_indicators"):
        why = _holiday_skip()
        if why:
            return why
    conn = _conn()
    try:
        r = pipeline.compute_indicators(conn)
        if r.get("busy"):
            return "拉取数据进行中，跳过（下次重试）"
        if not r["ok"]:
            raise RuntimeError(str(r))
        return (f"补股息率 {r.get('dividend_yield_filled', 0)} 行，"
                f"评分 {r.get('scored', 0)} 个标的，耗时 {r.get('elapsed', 0)}s")
    finally:
        conn.close()


def notify_build():
    """03:00：先补算标的信息，再读评分生成并暂存邮件正文。

    手动点「执行」时会带上 force 标记：跳过"非交易日不构建"的保护（周末也能
    生成一份用于预览），并先把标的信息补算到最新，保证邮件内容和库里一致。
    """
    force = _take_force("notify_build")
    conn = _conn()
    try:
        r = pipeline.build_notification(conn, force=force)
        if not r["ok"]:
            return f"跳过构建：{r.get('reason')}"
        extra = ""
        if r.get("computed"):
            c = r["computed"]
            extra = (f"（先补算：股息率 {c.get('dividend_yield_filled', 0)} 行，"
                     f"评分 {c.get('scored', 0)} 个标的，{c.get('elapsed', 0)}s）")
        elif r.get("compute_busy"):
            extra = "（补算正忙，用现有数据生成）"
        return f"构建：{r.get('subject', '')}{extra}"
    finally:
        conn.close()


def notify_send():
    """08:30：发送当天暂存的通知邮件（首次）。

    手动点「执行」时（force）如果今天还没构建正文，会**先构建再发送**——
    按钮的语义就是"现在发一封邮件"，不该因为"还没到 03:00 构建时间"而空转。
    """
    force = _take_force("notify_send")
    if not force:
        why = _holiday_skip()
        if why:
            return why

    conn = _conn()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        built = ""
        if force and not storage.load_pending_mail(conn, today):
            b = pipeline.build_notification(conn, force=True)
            if not b.get("ok"):
                return f"未发送：正文构建失败（{b.get('reason')}）"
            built = "（已先构建正文）"
        r = pipeline.send_notification(conn)
        return (f"发送{built}：{r.get('subject')}" if r["ok"]
                else f"跳过发送：{r.get('reason')}")
    finally:
        conn.close()


def notify_resend():
    """11:30/18:00/21:00：若当天是告警邮件，则重发（合计最多 4 次）。"""
    if not _take_force("notify_resend"):
        why = _holiday_skip()
        if why:
            return why

    conn = _conn()
    try:
        r = pipeline.resend_alert(conn)
        return (f"重发：{r.get('subject')}[第 {r.get('sent_count')} 次]" if r["ok"]
                else f"跳过重发：{r.get('reason')}")
    finally:
        conn.close()


# =====================================================================
# 任务登记表（动态：按配置生成 data_sync + notify 各阶段）
# =====================================================================
def _build_jobs() -> list[dict]:
    sc = config.schedule()

    def crontrigger(hhmm):
        """每天在指定时刻触发；**是否真跑由任务里的交易日守卫判断**
        （用 baostock 股市日历，能正确处理周末调休交易日）。"""
        h, m = config._split_hhmm(hhmm)
        return {"type": "cron", "day_of_week": "*", "hour": h, "minute": m}

    jobs = [
        {"id": "data_sync", "name": "拉取数据", "func": data_sync,
         "trigger": crontrigger(sc["sync_time"]),
         "remark": "拉K线(前复权+不复权收盘价)/成分股/分红，纯取数落库"},
        {"id": "compute_indicators", "name": "计算指标", "func": compute_indicators,
         "trigger": {"type": "interval",
                     "minutes": sc["indicators_interval_minutes"]},
         "remark": "补算股息率/指数估值/组合K线 + 综合评分（离线，检查缺失即补）"},
        {"id": "notify_build", "name": "通知·构建正文", "func": notify_build,
         "trigger": crontrigger(sc["notify_build_time"]),
         "remark": "读评分，生成并暂存邮件正文"},
        {"id": "notify_send", "name": "通知·发送邮件", "func": notify_send,
         "trigger": crontrigger(sc["notify_send_time"]),
         "remark": "发送当天暂存的通知邮件"},
    ]
    for i, t in enumerate(sc["notify_resend_times"] or [], 1):
        jobs.append({"id": f"notify_resend_{i}", "name": "通知·告警重发",
                     "func": notify_resend, "trigger": crontrigger(t),
                     "remark": "告警邮件重发（与首次合计最多4次）"})
    return jobs


def JOBS():
    return _build_jobs()


def registry() -> dict:
    return {j["id"]: j for j in _build_jobs()}


def by_id(job_id: str):
    return registry().get(job_id)
