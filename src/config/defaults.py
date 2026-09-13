# -*- coding: utf-8 -*-
"""系统默认配置（**唯一来源**）。

`script/init_db.py` 用它初始化新库；Web 服务启动时用 `config.ensure_settings()`
把这里**新增**的键补进老库（不覆盖已有值），并清掉已废弃的键。
因此以后加配置项只需要改这个文件。
"""

# ---------------------------------------------------------------------
# SMTP 凭据：优先从 src/config/local_config.py 读取（该文件已被 gitignore）
# ---------------------------------------------------------------------
try:
    from src.config import local_config as _lc
except ImportError:
    _lc = None


def _lc_get(attr, default):
    return getattr(_lc, attr, default) if _lc else default


# =====================================================================
# 默认配置清单（key, value, val_type, group, remark）
# 复杂结构用 JSON 存储；这些值此后可在数据库中直接修改
# =====================================================================
DEFAULT_SETTINGS = [
    # ---------- 邮件 / SMTP ----------
    ("SMTP_HOST", _lc_get("SMTP_HOST", "smtp.qq.com"), "str", "smtp", "SMTP 服务器"),
    ("SMTP_PORT", _lc_get("SMTP_PORT", 465), "int", "smtp", "465=SSL / 587=STARTTLS"),
    ("SMTP_USER", _lc_get("SMTP_USER", ""), "str", "smtp", "发件邮箱"),
    ("SMTP_PASS", _lc_get("SMTP_PASS", ""), "str", "smtp", "SMTP 授权码（非登录密码）"),
    ("MAIL_FROM", _lc_get("MAIL_FROM", _lc_get("SMTP_USER", "")), "str", "smtp", "发件人"),
    ("MAIL_TO", _lc_get("MAIL_TO", []), "json", "smtp", "收件人列表"),

    # ---------- 运行控制 ----------
    ("SYNC_BEFORE_SCORE", True, "bool", "runtime", "手动跑批（预览/发信）前是否先增量抓数"),
    ("SYNC_RUN_TIME", "18:00", "str", "runtime", "拉取数据定时任务的触发时间 HH:MM"),
    ("INDICATORS_INTERVAL_MINUTES", 30, "int", "runtime", "计算指标任务间隔分钟（检查缺失并补算）"),
    ("NOTIFY_BUILD_TIME", "03:00", "str", "runtime", "通知任务：生成并暂存邮件正文的时间 HH:MM"),
    ("NOTIFY_SEND_TIME", "08:30", "str", "runtime", "通知任务：发送邮件的时间 HH:MM"),
    ("NOTIFY_RESEND_TIMES", ["11:30", "18:00", "21:00"], "json", "runtime",
     "告警邮件重发的时点列表 HH:MM（与首次发送合计最多 4 次）"),
    ("MAIL_RETENTION_DAYS", 7, "int", "runtime", "暂存邮件正文保留天数（超出自动删除）"),
    ("LAST_ALERT_SIG", "", "str", "runtime", "最近一次告警信状态签名（程序自动写入）"),
    ("LAST_ALERT_DATE", "", "str", "runtime", "最近一次告警信日期（程序自动写入）"),
    ("LAST_RUN_AT", "", "str", "runtime", "最近一次跑批时间（程序自动写入）"),
    ("LAST_MAIL_AT", "", "str", "runtime", "最近一次发信时间（程序自动写入）"),
    ("LAST_MAIL_SUBJECT", "", "str", "runtime", "最近一次邮件标题（程序自动写入）"),

    # ---------- 信号与展示 ----------
    ("SIGNAL_BANDS", [
        [0, 20, "低估", "🟢", "大额定投（2-3倍）"],
        [20, 40, "偏低", "🔵", "正常定投"],
        [40, 70, "正常", "⚪", "小额定投"],
        [70, 85, "偏高", "🟠", "停止定投"],
        [85, 101, "高估", "🔴", "分批卖出（每涨5%分位卖1/3）"],
    ], "json", "signal", "五档信号阈值与动作：(下限,上限,状态,emoji,动作)"),
    ("ACTION_SHORT", {"低估": "大额买入", "偏低": "正常定投", "正常": "小额/持有",
                      "偏高": "停止定投", "高估": "分批卖出"},
     "json", "signal", "状态→标题动作短词"),
    ("ACTION_ICON", {"低估": "💰", "偏低": "💵", "正常": "🤏",
                     "偏高": "⏸️", "高估": "📤"},
     "json", "signal", "状态→动作图标"),
    ("STATUS_STYLE", {"低估": ["#1e8e5a", "#ffffff"], "偏低": ["#2f6fc1", "#ffffff"],
                      "正常": ["#75839a", "#ffffff"], "偏高": ["#a85a00", "#ffffff"],
                      "高估": ["#c94f4f", "#ffffff"], "未知": ["#9aa3b2", "#ffffff"]},
     "json", "signal", "状态→[背景色,文字色]"),
    ("ALERT_STATUSES", ["低估", "高估"], "json", "signal", "进入告警态的状态"),
    ("DIVERGENCE_THRESHOLD", 15, "int", "signal",
     "「短期显著偏离长期」阈值：5年分位与10年分位差超过该值就在邮件里提示"),
    ("ALERT_PREFIX", "【🔔重点提醒】", "str", "signal", "告警时标题前缀"),
    ("ALERT_MARK", "⚡", "str", "signal", "告警指数标题标记"),

    # ---------- 历史窗口 ----------
    ("HISTORY_YEARS_10Y", 10, "int", "chart", "分位计算窗口(年)"),
    ("HISTORY_YEARS_5Y", 5, "int", "chart", "分位计算窗口(年)"),
    ("HISTORY_YEARS_CHART", 5, "int", "chart", "走势图展示窗口(年)：网页 K 线图 + 邮件走势图共用"),

    # ---------- 权重（未在 valuation_target 单独配置时回退到此默认）----------
    ("COMPOSITE_WEIGHTS", {"pe": 0.25, "pb": 0.20, "ps": 0.25,
                           "pcf": 0.15, "dividend": 0.15},
     "json", "weight", "默认五指标权重（合计 1.0）"),

    # ---------- baostock 抓取参数 ----------
    # 注意：复权方式不开放为配置项。指标口径依赖固定复权：日线 close 必须前复权
    # （除权后价格连续，否则分位/涨跌全错），close_raw 必须不复权（用于回补真实价格）。
    # 见 src/fetch_data/data_fetcher.py 顶部的 ADJUST_KLINE / ADJUST_RAW 常量。
    ("BAOSTOCK_START_DATE", "1990-01-01", "str", "baostock",
     "K线/不复权收盘价/交易日历的历史起点（1990-01-01=A股起点，拉取全部历史）"),
    ("DIVIDEND_YEARS_BACK", 3, "int", "baostock", "分红回溯年数（股息率只需近12个月，避免拉全历史）"),
    ("REBUILD_WORKERS", 0, "int", "baostock",
     "全量分位重建的并行进程数（0=按 CPU 自动，树莓派建议 0 或 2~3）"),
    ("BAOSTOCK_RETRY", 3, "int", "baostock", "单次查询失败重试次数"),
    ("BAOSTOCK_TIMEOUT", 60, "int", "baostock", "单次查询超时(秒)"),
    ("FETCH_SLEEP", 0.2, "float", "baostock", "查询间隔(秒)，降低被限流/卡死概率"),
    ("FETCH_BATCH_LOG", 50, "int", "baostock", "每处理多少只标的打印一次进度"),
    ("DIVIDEND_LOOKBACK_DAYS", 365, "int", "baostock", "动态股息率回溯天数(近12个月)"),

    # ---------- Web 服务（FastAPI + uvicorn）----------
    ("WEB_HOST", "0.0.0.0", "str", "web",
     "监听地址：0.0.0.0=局域网可访问 / 127.0.0.1=仅本机（需重启服务生效）"),
    ("WEB_PORT", 8000, "int", "web", "监听端口（需重启服务生效）"),
    ("WEB_LOG_LEVEL", "info", "str", "web",
     "uvicorn 日志级别: critical/error/warning/info/debug（需重启服务生效）"),
    ("WEB_RELOAD", False, "bool", "web",
     "代码热重载（开发用；需安装 watchfiles，不可与调度器共存。需重启服务生效）"),
    ("WEB_PAGE_REFRESH_SEC", 30, "int", "web",
     "网页顶栏状态自动刷新间隔(秒)，0=不自动刷新（刷新页面即生效）"),
    ("WEB_AUTH_TOKEN", "", "str", "web",
     "接口访问令牌（留空=不校验；局域网暴露在 0.0.0.0 时建议设置一个）"),
    ("SETTING_GROUPS", [
        {"title": "📧 邮件配置", "keys": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "MAIL_TO"]},
        {"title": "⏰ 定时任务 · 拉取数据（每天取数）", "keys": ["SYNC_RUN_TIME"]},
        {"title": "🧮 定时任务 · 计算指标（按间隔补算）", "keys": ["INDICATORS_INTERVAL_MINUTES"]},
        {"title": "📨 定时任务 · 通知推送（构建/发送/重发）", "keys": ["NOTIFY_BUILD_TIME", "NOTIFY_SEND_TIME", "NOTIFY_RESEND_TIMES", "MAIL_RETENTION_DAYS"]},
        {"title": "⚙️ 定时任务 · 调度器（启停/时区/容错）", "keys": ["SKIP_NON_TRADING_DAY", "SCHEDULER_ENABLED", "SCHEDULER_TIMEZONE", "SCHEDULER_MISFIRE_GRACE", "SCHEDULER_COALESCE", "SCHEDULER_MAX_INSTANCES", "SCHEDULER_LOCK_FILE"]},
        {"title": "🎯 估值区间与信号", "keys": ["SIGNAL_BANDS", "ALERT_STATUSES", "DIVERGENCE_THRESHOLD", "ALERT_PREFIX", "ALERT_MARK"]},
        {"title": "🛒 推荐操作与配色", "keys": ["ACTION_SHORT", "ACTION_ICON", "STATUS_STYLE"]},
        {"title": "📥 数据源（baostock）", "keys": ["BAOSTOCK_START_DATE", "BAOSTOCK_RETRY", "BAOSTOCK_TIMEOUT", "FETCH_SLEEP", "FETCH_BATCH_LOG", "DIVIDEND_LOOKBACK_DAYS", "DIVIDEND_YEARS_BACK", "REBUILD_WORKERS", "SYNC_BEFORE_SCORE"]},
        {"title": "📈 图表与权重", "keys": ["HISTORY_YEARS_10Y", "HISTORY_YEARS_5Y", "HISTORY_YEARS_CHART", "COMPOSITE_WEIGHTS"]},
        {"title": "⚙️ 系统", "keys": ["WEB_HOST", "WEB_PORT", "WEB_LOG_LEVEL", "WEB_RELOAD", "WEB_PAGE_REFRESH_SEC", "WEB_AUTH_TOKEN", "PREVIEW_DIR"]},
    ], "json", "web", "配置分组及展示顺序（title=节标题, keys=该节配置键，程序自动维护）"),
    ("PREVIEW_DIR", "logs/preview", "str", "web", "邮件预览 HTML 输出目录（web 端生成）"),

    # ---------- 调度器（APScheduler，与 web 同进程）----------
    ("SKIP_NON_TRADING_DAY", True, "bool", "scheduler",
     "所有定时任务只在本交易日执行（按 baostock 同步下来的股市日历判断，含周末调休交易日；关闭则每天都跑）"),
    ("SCHEDULER_ENABLED", True, "bool", "scheduler", "启动 web 时是否同时启动定时任务"),
    ("SCHEDULER_TIMEZONE", "Asia/Shanghai", "str", "scheduler", "调度时区"),
    ("SCHEDULER_MISFIRE_GRACE", 300, "int", "scheduler", "错过触发时间的容忍秒数，超过则放弃本次"),
    ("SCHEDULER_COALESCE", True, "bool", "scheduler", "积压多次触发时是否合并为一次"),
    ("SCHEDULER_MAX_INSTANCES", 1, "int", "scheduler", "同一任务最大并发实例数"),
    ("SCHEDULER_LOCK_FILE", "logs/scheduler.lock", "str", "scheduler",
     "调度器单实例锁文件（防止多进程重复跑任务；换数据库跑第二套实例时改成别的路径。需重启服务生效）"),
    ("SCHEDULER_LAST_RUNS", {}, "json", "scheduler", "各任务最近一次执行结果（程序自动写入）"),
]


# 估值目标初始权重（来自 docs/chat.md 的推荐比率）
# (code, name, ktype, enabled, w_pe, w_pb, w_ps, w_pcf, w_dividend, sort, remark)
TARGET_SPECS = [
    ("sh.000300", "沪深300", "index", 1,
     0.30, 0.25, 0.15, 0.10, 0.20, 1, "大盘蓝筹：PE30/PB25/股息20/PS15/PCF10"),
    ("sh.000905", "中证500", "index", 1,
     0.25, 0.20, 0.30, 0.15, 0.10, 2, "中盘成长：PS30/PE25/PB20/PCF15/股息10"),
    ("sh.000688", "科创50", "portfolio", 1,
     0.20, 0.20, 0.40, 0.15, 0.05, 3,
     "科创成长：PS40/PE20/PB20/PCF15/股息5（组合，成分股手工维护）"),
]


def dump_value(value, val_type: str) -> str:
    """按 val_type 把 Python 值序列化成库里存的字符串。"""
    if val_type == "json":
        import json
        return json.dumps(value, ensure_ascii=False)
    if val_type == "bool":
        return "1" if value else "0"
    return str(value)


LOCAL_CONFIG_PATH = "src/config/local_config.py"


def default_of(key, fallback=None):
    """按 key 取 DEFAULT_SETTINGS 里的出厂默认值（找不到时返回 fallback）。"""
    for k, value, *_ in DEFAULT_SETTINGS:
        if k == key:
            return value
    return fallback


# 配置节定义（展示顺序 + 每节包含的键）。设置页、新建库都以这里为唯一来源。
SETTING_GROUPS = default_of("SETTING_GROUPS", [])


def smtp_source_note() -> str:
    """SMTP 凭据来源说明（供 init_db 打印，避免直接引用私有 _lc）。"""
    if _lc:
        return f"SMTP 凭据来源：{LOCAL_CONFIG_PATH}"
    return (f"SMTP 凭据来源：内置默认（未发现 {LOCAL_CONFIG_PATH}，"
            "邮箱项为空，请手工配置）")


# =====================================================================
# 已废弃的配置键（老库里若存在，服务启动时清掉）
# =====================================================================
# MAIN_RUN_* / ALERT_RUN_*：早期 cron 方案的主跑时段/盘中告警时段，已由
#   APScheduler 的 SYNC_RUN_TIME / NOTIFY_* 取代。
# SCHEDULER_HEARTBEAT_*：从未实现心跳任务，任务列表的 next_run 已能反映调度器活性。
#
# 注：BAOSTOCK_ADJUSTFLAG 曾经也在这张墓碑清单里。它早已从本库清掉、新库也不会再
#   写入，留着只是占用；复权方式固定为代码常量（见 data_fetcher 的 ADJUST_KLINE /
#   ADJUST_RAW），设置页不暴露，因此已删除这条墓碑。
DEPRECATED_SETTINGS = (
    "RUN_WEEKDAYS",          # 由 baostock 股市日历替代（见 SKIP_NON_TRADING_DAY）
    "MAIN_RUN_HOUR",
    "MAIN_RUN_MINUTE",
    "ALERT_RUN_HOURS",
    "ALERT_RUN_MINUTE",
    "SCHEDULER_HEARTBEAT_MINUTES",
    "SCHEDULER_HEARTBEAT_AT",
)
