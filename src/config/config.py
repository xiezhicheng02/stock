# -*- coding: utf-8 -*-
"""配置访问层（纯函数式）：所有配置存于 SQLite。

配置来源
--------
* setting 表           —— 全局配置（SMTP、信号档位、窗口期、baostock 参数等）
* valuation_target 表  —— 需要计算综合估值的指数/个股及其五指标权重

本文件只保留数据库路径（用于定位数据库本身），其余配置一律从库读取。

用法
----
    from src.config import config

    host = config.get("SMTP_HOST")                 # 按 val_type 自动转换类型
    port = config.get_int("SMTP_PORT", 465)
    bands = config.get_json("SIGNAL_BANDS")
    enabled = config.get_bool("SKIP_NON_TRADING_DAY")

    config.set("MAIN_RUN_HOUR", 19)                # 写回数据库
    for t in config.targets():                     # 估值目标（含权重）
        print(t["name"], t["weights"], t["ktype"])
    config.reload()                                # 外部改了库后刷新缓存

初始化数据库：python3 script/init_db.py
"""

import json
import logging
import math
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime

# ---------------------------------------------------------------------
# 数据库位置：唯一保留在代码中的配置项（可用环境变量 STOCK_DB 覆盖）
# ---------------------------------------------------------------------
DB_PATH = os.environ.get("STOCK_DB", "data/stock.db")

log = logging.getLogger("config")

# 进程内缓存：{key: (value_str, val_type)} + 对应的库文件指纹
_cache = None
_cache_stamp = None
_lock = threading.RLock()          # 保护缓存与"读-改-写"


# =====================================================================
# 内部：连接与缓存
# =====================================================================
def _connect(db_path=None):
    path = db_path or DB_PATH
    if not os.path.exists(path):
        raise RuntimeError(
            f"数据库不存在：{path}\n请先初始化：python3 script/init_db.py"
        )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _db_stamp():
    """数据库的"新鲜度指纹"：主库文件与 WAL 文件的 (mtime_ns, size)。

    WAL 模式下提交主要写 -wal 文件，所以两个都要看。
    取不到（文件不存在等）返回 None，此时退化为"不校验"。
    """
    stamp = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(DB_PATH + suffix)
            stamp.append((suffix, st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append((suffix, None, None))
    return tuple(stamp)


def _load(force=False):
    """加载 setting 表到内存缓存（首次访问、reload、或检测到库被外部改过时）。

    为什么要校验新鲜度：这是**进程内缓存**，而配置改动可能来自
      ① 管理员直接在数据库里改（init_db 的注释就是这么建议的）；
      ② 另一个进程（命令行跑批、cron）写 LAST_RUN_AT / SCHEDULER_LAST_RUNS 等运行痕迹。
    不做校验的话，运行中的 web 会一直用旧配置，而设置页（直读库）显示新值 ——
    "页面看到的"和"实际生效的"不一致，改时段/阈值不重启就不生效。
    """
    global _cache, _cache_stamp
    stamp = _db_stamp()
    if _cache is not None and not force and stamp == _cache_stamp:
        return _cache
    with closing(_connect()) as conn:
        try:
            rows = conn.execute("SELECT key, value, val_type FROM setting").fetchall()
        except sqlite3.OperationalError as e:
            raise RuntimeError(
                f"读取 setting 表失败（{e}）：数据库可能未初始化，"
                f"请运行 python3 script/init_db.py"
            ) from e
    _cache = {r["key"]: (r["value"], r["val_type"] or "str") for r in rows}
    _cache_stamp = _db_stamp()          # 读完之后再取，避免读到一半文件又变了
    return _cache


def reload():
    """清空缓存并重新加载（在数据库被外部修改后调用）。"""
    with _lock:
        _load(force=True)


def ensure_settings(delete_deprecated: bool = True) -> dict:
    """启动自愈：把代码里**新增**的默认配置补进库，并清掉已废弃的键（幂等）。

    老库不会自动获得 `init_db.py` 之后新增的配置项——设置页看不到、也改不了
    （例如后来加的 INDICATORS_INTERVAL_MINUTES）。这里只**新增缺失项、不动已有值**。
    返回 {"added": [...], "removed": [...]}。
    """
    from src.config.defaults import (DEFAULT_SETTINGS, DEPRECATED_SETTINGS,
                                     dump_value)
    with closing(_connect()) as conn:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        added = []
        for key, value, val_type, group, remark in DEFAULT_SETTINGS:
            if conn.execute("SELECT 1 FROM setting WHERE key=?",
                            (key,)).fetchone():
                continue
            conn.execute(
                "INSERT OR REPLACE INTO setting"
                "(key,value,val_type,group_name,remark,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (key, dump_value(value, val_type), val_type, group, remark, now))
            added.append(key)
        removed = []
        if delete_deprecated:
            for key in DEPRECATED_SETTINGS:
                cur = conn.execute("DELETE FROM setting WHERE key=?", (key,))
                if cur.rowcount:
                    removed.append(key)
        conn.commit()
    if removed:
        # 这些键可能还挂在 SETTING_GROUPS 的某个节里，顺手摘掉。
        # 注意：本模块定义了 set() 配置写入函数，会遮蔽内置 set()，因此用 frozenset。
        try:
            drop = frozenset(removed)
            groups = get_json("SETTING_GROUPS", []) or []
            for g in groups:
                if isinstance(g, dict) and g.get("keys"):
                    g["keys"] = [k for k in g["keys"] if k not in drop]
            set("SETTING_GROUPS", groups, group="web",
                remark="配置分组及展示顺序（程序自动维护）")
        except Exception as e:                      # noqa: BLE001
            log.warning("清理废弃配置的分组引用失败：%s", e)
    if added or removed:
        reload()
        log.info("配置自愈：新增 %d 项 %s；清理废弃 %d 项 %s",
                 len(added), added or "-", len(removed), removed or "-")
    return {"added": added, "removed": removed}


def _convert(value, val_type, key=None):
    """按 val_type 把字符串转成 Python 值；转换失败记 warning（配置是手工改的，手误很常见）。"""
    if value is None or val_type == "none":
        return None
    if val_type == "json":
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            log.warning("配置 %s 不是合法 JSON：%r（按默认值处理）", key, value)
            return None
    if val_type == "int":
        try:
            return int(float(value))
        except (ValueError, TypeError):
            log.warning("配置 %s 不是整数：%r（按默认值处理）", key, value)
            return None
    if val_type == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            log.warning("配置 %s 不是数字：%r（按默认值处理）", key, value)
            return None
    if val_type == "bool":
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return value


def _dump(value):
    """把 Python 值序列化，并推断 val_type。返回 (字符串值, val_type)。"""
    if value is None:
        return None, "none"                  # 存 NULL，不要存成字符串 "None"
    if isinstance(value, bool):
        return ("1" if value else "0"), "bool"
    if isinstance(value, int):
        return str(value), "int"
    if isinstance(value, float):
        return str(value), "float"
    if isinstance(value, (dict, list, tuple)):
        # 元组（如 SIGNAL_BANDS）序列化为 JSON 数组
        return json.dumps(value, ensure_ascii=False), "json"
    return str(value), "str"


# =====================================================================
# 读接口
# =====================================================================
def get(key, default=None):
    """读取配置项，按 val_type 自动转换类型；不存在时返回 default。"""
    cache = _load()
    if key not in cache:
        return default
    value, val_type = cache[key]
    if val_type == "none":
        return default
    converted = _convert(value, val_type, key)
    return default if converted is None else converted


def get_str(key, default=""):
    """读取字符串配置。"""
    v = get(key, default)
    return default if v is None else str(v)


def get_int(key, default=0):
    """读取整数配置。"""
    v = get(key, None)
    if v is None:
        return default
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def get_float(key, default=0.0):
    """读取浮点配置。"""
    v = get(key, None)
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def get_bool(key, default=False):
    """读取布尔配置（1/true/yes/on 视为真）。"""
    v = get(key, None)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_json(key, default=None):
    """读取 JSON 配置（列表/字典）。"""
    v = get(key, None)
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return default


def exists(key):
    """配置项是否存在。"""
    return key in _load()


def all_settings(group=None):
    """列出配置项（可按分组过滤），返回 [{key,value,val_type,group,remark}]。"""
    with closing(_connect()) as conn:
        sql = ("SELECT key, value, val_type, group_name, remark FROM setting "
               + ("WHERE group_name=? " if group else "")
               + "ORDER BY group_name, key")
        rows = conn.execute(sql, (group,) if group else ()).fetchall()
    return [{"key": r["key"], "value": r["value"], "val_type": r["val_type"],
             "group": r["group_name"], "remark": r["remark"]} for r in rows]


# =====================================================================
# 写接口
# =====================================================================
def set(key, value, group=None, remark=None):
    """写入/更新配置项并刷新缓存。

    val_type 由 Python 类型自动推断（dict/list→json, int→int, float→float,
    bool→bool, None→NULL, 其余→str）。
    """
    val_str, val_type = _dump(value)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        with closing(_connect()) as conn:
            if group is None or remark is None:
                row = conn.execute(
                    "SELECT group_name, remark FROM setting WHERE key=?",
                    (key,)).fetchone()
                if row:
                    group = group if group is not None else row["group_name"]
                    remark = remark if remark is not None else row["remark"]
            conn.execute(
                """INSERT OR REPLACE INTO setting
                   (key, value, val_type, group_name, remark, updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (key, val_str, val_type, group, remark, now),
            )
            conn.commit()
        if _cache is not None:
            _cache[key] = (val_str, val_type)
        _cache_stamp = _db_stamp()
    return val_type


def update_json(key, fn, default=None):
    """原子地"读-改-写"一个 JSON 配置项：new = fn(旧值)。

    直接用 get_json()+set() 做读改写会在并发下丢更新（两个线程各读到同一份旧值，
    后写的覆盖先写的）。这里用进程内锁保证串行。
    """
    with _lock:
        current = get_json(key, default)
        updated = fn(current)
        set(key, updated)
        return updated


def use_db(path: str | None):
    """切换数据库（CLI 的 --db 用）：改 DB_PATH 并清缓存。

    必须在读配置之前调用，否则配置仍来自旧库（数据写新库、配置读旧库的诡异状态）。
    """
    global DB_PATH
    if path:
        DB_PATH = os.path.abspath(path)
    reload()
    return DB_PATH




def delete(key):
    """删除配置项。"""
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM setting WHERE key=?", (key,))
        conn.commit()
    if _cache is not None:
        _cache.pop(key, None)


# =====================================================================
# 估值目标（valuation_target 表）
# =====================================================================
def _target_row(r) -> dict:
    """valuation_target 的一行 → 目标字典（targets/target 共用，形状只定义一处）。"""
    return {
        "code": r["code"], "name": r["name"], "ktype": r["ktype"],
        "enabled": bool(r["enabled"]), "sort_order": r["sort_order"],
        "remark": r["remark"],
        "weights": {
            "pe": r["w_pe"] or 0.0, "pb": r["w_pb"] or 0.0,
            "ps": r["w_ps"] or 0.0, "pcf": r["w_pcf"] or 0.0,
            "dividend": r["w_dividend"] or 0.0,
        },
    }


def targets(only_enabled=True, ktype=None):
    """读取需要计算综合估值的指数/个股及其权重。

    返回 [{code, name, ktype, enabled, sort_order, remark, weights:{pe,pb,ps,pcf,dividend}}]
    """
    sql = "SELECT * FROM valuation_target"
    conds, params = [], []
    if only_enabled:
        conds.append("enabled=1")
    if ktype:
        conds.append("ktype=?")
        params.append(ktype)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY sort_order, code"
    with closing(_connect()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_target_row(r) for r in rows]


def target(code):
    """按代码取单个估值目标；不存在返回 None。

    **直接走主键查**（`valuation_target.code` 是主键），不要遍历 targets() ——
    后者是整表读取。调用方只要逐只循环，就是 N 次全表读：首页告警卡片曾经循环
    856 个代码、每次都调这里，实测本机 525ms、树莓派上约 3~5s。
    """
    with closing(_connect()) as conn:
        r = conn.execute("SELECT * FROM valuation_target WHERE code=?",
                         (code,)).fetchone()
    return _target_row(r) if r else None


def weights_of(code):
    """取某标的的权重字典；未配置时回退到 COMPOSITE_WEIGHTS 默认权重。"""
    t = target(code)
    if t:
        return t["weights"]
    default = get_json("COMPOSITE_WEIGHTS", {}) or {}
    return {
        "pe": default.get("pe", 0.0), "pb": default.get("pb", 0.0),
        "ps": default.get("ps", 0.0), "pcf": default.get("pcf", 0.0),
        "dividend": default.get("dividend", 0.0),
    }


def set_target(code, name, ktype, weights, enabled=True, sort_order=0, remark=None):
    """新增/更新估值目标与权重。weights 为 {pe,pb,ps,pcf,dividend}。"""
    w = weights or {}
    with closing(_connect()) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO valuation_target
               (code,name,ktype,enabled,w_pe,w_pb,w_ps,w_pcf,w_dividend,sort_order,remark)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (code, name, ktype, 1 if enabled else 0,
             w.get("pe", 0.0), w.get("pb", 0.0), w.get("ps", 0.0),
             w.get("pcf", 0.0), w.get("dividend", 0.0), sort_order, remark),
        )
        conn.commit()


# =====================================================================
# 常用配置的语义化快捷函数（可读性更好）
# =====================================================================
def signal_bands():
    """五档信号：(下限, 上限, 状态, emoji, 动作)。"""
    return get_json("SIGNAL_BANDS", [])


def alert_statuses():
    """进入告警态的状态列表。"""
    return get_json("ALERT_STATUSES", [])


def status_style():
    """状态配色 {状态: [背景色, 文字色]}。"""
    return get_json("STATUS_STYLE", {})


def action_short():
    """状态 → 标题动作短词。"""
    return get_json("ACTION_SHORT", {})


def action_icon():
    """状态 → 动作图标。"""
    return get_json("ACTION_ICON", {})


def signal_of(score):
    """按**当前** SIGNAL_BANDS / STATUS_STYLE / ACTION_* / ALERT_STATUSES 判定信号。

    这是全系统（网页 + 邮件 + 指标计算）唯一的信号判定实现，保证四组配置
    相互适配、口径一致。

    为什么展示层不能直接读 valuation_score.status/action：那两列是**算分位那一刻
    的快照**，用户在设置页改了 SIGNAL_BANDS 之后不会自动更新（除非全量重算分位）。
    因此凡是"展示给用户看的信号"，一律用本函数按当前配置实时推导。

    返回 {status, emoji, action, short, icon, color, text, alert}
    * emoji/action —— SIGNAL_BANDS 该档自带的长文案
    * short/icon   —— ACTION_SHORT / ACTION_ICON 里的短文案与图标（覆盖长文案）
    * color/text   —— STATUS_STYLE 的背景色/文字色
    """
    bands = signal_bands()

    def _unknown():
        style = status_style().get("未知") or []
        return {"status": "未知", "emoji": "❓", "action": "无", "short": "—",
                "icon": action_icon().get("未知", "❓"),
                "color": style[0] if style else "#9aa3b2",
                "text": style[1] if len(style) > 1 else "#ffffff",
                "alert": False}

    if not bands or score is None:
        return _unknown()
    try:
        v = float(score)
    except (TypeError, ValueError):
        return _unknown()
    if not math.isfinite(v):
        return _unknown()

    band = None
    for b in bands:
        if len(b) < 3:
            continue
        if b[0] <= v < b[1]:
            band = b
            break
    if band is None:
        # 超出所有档位时兜底到最近的一端，避免极端值显示"未知/无"
        usable = [b for b in bands if len(b) >= 3]
        if not usable:
            return _unknown()
        band = usable[0] if v < usable[0][0] else usable[-1]

    status = band[2]
    action = band[4] if len(band) > 4 else ""
    emoji = band[3] if len(band) > 3 else ""
    style = status_style().get(status) or []
    return {
        "status": status,
        "emoji": emoji,
        "action": action,
        "short": action_short().get(status) or action,
        "icon": action_icon().get(status) or emoji,
        "color": style[0] if style else "#75839a",
        "text": style[1] if len(style) > 1 else "#ffffff",
        "alert": status in alert_statuses(),
    }


def smtp():
    """SMTP 配置字典（含授权码，注意保密）。"""
    return {
        "host": get_str("SMTP_HOST"), "port": get_int("SMTP_PORT", 465),
        "user": get_str("SMTP_USER"), "password": get_str("SMTP_PASS"),
        "sender": get_str("MAIL_FROM"), "receivers": get_json("MAIL_TO", []),
    }


def windows():
    """分位/图表窗口（年）：main 主窗口(10) / ref 参考窗口(5) / chart 折线图窗口(5)。"""
    return {
        "main": get_int("HISTORY_YEARS_10Y", 10),
        "ref": get_int("HISTORY_YEARS_5Y", 5),
        "chart": get_int("HISTORY_YEARS_CHART", 5),
    }


def ui_meta():
    """下发给前端的展示类配置（估值区间/信号/配色/走势图窗口/顶栏刷新间隔）。

    这些配置过去只作用于后端，网页里各写死了一份副本，于是"改设置页网页没变化"。
    统一由 /api/health 的 meta 字段下发，前端启动时重建，保证设置页改完刷新页面即生效。

    * bands     —— SIGNAL_BANDS + STATUS_STYLE 合并后的区间（含颜色/图标/动作）
    * action_short / action_icon —— 状态 → 短动作词 / 图标（覆盖 SIGNAL_BANDS 里的长句）
    * alert_statuses —— 进入告警态的状态
    * divergence_threshold —— 5 年分位与 10 年分位"显著偏离"的阈值
    """
    style = status_style()
    bands = []
    for b in signal_bands():
        if not isinstance(b, (list, tuple)) or len(b) < 3:
            continue
        lo, hi, name = b[0], b[1], b[2]
        colors = style.get(name) or ["#75839a", "#ffffff"]
        bands.append({
            "lo": lo, "hi": hi, "name": name,
            "icon": b[3] if len(b) > 3 else "",
            "action": b[4] if len(b) > 4 else "",
            "color": colors[0] if colors else "#75839a",
            "text": colors[1] if len(colors) > 1 else "#ffffff",
        })
    return {
        "bands": bands,
        "status_style": style,
        "action_short": action_short(),
        "action_icon": action_icon(),
        "alert_statuses": alert_statuses(),
        "alert_mark": get_str("ALERT_MARK", "⚡") or "⚡",
        "alert_prefix": get_str("ALERT_PREFIX", ""),
        "divergence_threshold": get_int("DIVERGENCE_THRESHOLD", 15),
        "windows": windows(),
        "page_refresh": get_int("WEB_PAGE_REFRESH_SEC", 30),
    }


def runtime():
    """运行控制：交易日跳过 + 手动跑批前是否先抓数。

    （主跑时段/盘中告警时段是旧 cron 方案的遗留，已由 APScheduler 的
    SYNC_RUN_TIME / NOTIFY_* 取代，这里不再提供。）
    """
    return {
        "skip_non_trading": get_bool("SKIP_NON_TRADING_DAY", True),
        "sync_before_score": get_bool("SYNC_BEFORE_SCORE", True),
    }


def _split_hhmm(t: str) -> tuple[int, int]:
    """把 'HH:MM' 拆成 (hour, minute)。"""
    try:
        h, m = str(t).split(":", 1)
        return int(h), int(m)
    except (ValueError, AttributeError):
        return 0, 0


def schedule():
    """定时任务触发时间（拉取数据 + 计算指标 + 通知构建/发送/重发）。"""
    return {
        "sync_time": get_str("SYNC_RUN_TIME", "18:00"),
        "indicators_interval_minutes": get_int("INDICATORS_INTERVAL_MINUTES", 30),
        "notify_build_time": get_str("NOTIFY_BUILD_TIME", "03:00"),
        "notify_send_time": get_str("NOTIFY_SEND_TIME", "08:30"),
        "notify_resend_times": get_json("NOTIFY_RESEND_TIMES", ["11:30", "18:00", "21:00"]),
        "mail_retention_days": get_int("MAIL_RETENTION_DAYS", 7),
    }


def web():
    """Web 服务参数（FastAPI + uvicorn）。"""
    return {
        "host": get_str("WEB_HOST", "0.0.0.0"),
        "port": get_int("WEB_PORT", 8000),
        "log_level": get_str("WEB_LOG_LEVEL", "info"),
        "reload": get_bool("WEB_RELOAD", False),
        "page_refresh": get_int("WEB_PAGE_REFRESH_SEC", 30),
        "preview_dir": get_str("PREVIEW_DIR", "logs/preview"),
    }


def scheduler():
    """调度器参数（APScheduler，与 web 同进程）。"""
    return {
        "enabled": get_bool("SCHEDULER_ENABLED", True),
        "timezone": get_str("SCHEDULER_TIMEZONE", "Asia/Shanghai"),
        "misfire_grace": get_int("SCHEDULER_MISFIRE_GRACE", 300),
        "coalesce": get_bool("SCHEDULER_COALESCE", True),
        "max_instances": get_int("SCHEDULER_MAX_INSTANCES", 1),
    }


def baostock():
    """baostock 抓取参数（复权方式固定，见 data_fetcher 的 ADJUST_* 常量）。"""
    return {
        # K线/不复权收盘价/交易日历的起点：1990-01-01 = A股市场起点，即"拉取全部历史"
        "start_date": get_str("BAOSTOCK_START_DATE", "1990-01-01"),
        # 分红只用于动态股息率（近12个月），单独限制回溯年数，避免随K线全历史拉到几十年
        "dividend_years_back": get_int("DIVIDEND_YEARS_BACK", 3),
        "retry": get_int("BAOSTOCK_RETRY", 3),
        "timeout": get_int("BAOSTOCK_TIMEOUT", 60),
        "sleep": get_float("FETCH_SLEEP", 0.2),
        "batch_log": get_int("FETCH_BATCH_LOG", 50),
        "dividend_lookback_days": get_int("DIVIDEND_LOOKBACK_DAYS", 365),
    }
