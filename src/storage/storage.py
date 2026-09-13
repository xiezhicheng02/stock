# -*- coding: utf-8 -*-
"""SQLite 数据访问层（显式传入 conn）。

职责范围
--------
本模块只负责**行情/成分股/分红/元信息/同步状态**的数据读写：

    kline              个股与指数 K 线（含四个估值指标 + 动态股息率）
    index_constituent  指数成分股
    dividend           分红原始数据
    stock_basic        标的元信息
    sync_state         增量同步状态

配置类数据（setting / valuation_target）由 src/config/config.py 负责，
表结构定义见 src/storage/schema.py，初始化见 script/init_db.py。

用法
----
    from src.storage import storage

    conn = storage.get_conn()                 # 默认取 config.DB_PATH
    storage.ensure_schema(conn)               # 幂等建表（脚本/测试用）

    storage.upsert_kline(conn, "sh.600000", "stock", [
        {"date": "2026-09-11", "open": 9.35, "close": 9.26, "pe_ttm": 6.02},
    ])
    last = storage.latest_kline_date(conn, "sh.600000")   # 增量起点
"""

import logging
import sqlite3
from datetime import datetime

from src.config import config
from src.storage import schema

log = logging.getLogger("storage")


# =====================================================================
# 连接与结构
# =====================================================================
def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """打开数据库连接（WAL 模式，行以 dict 方式访问）。

    busy_timeout：拉取数据（写）与计算指标（写）可并发，WAL 下并发写会在
    事务层串行，这里设等待超时避免短事务相撞直接抛 SQLITE_BUSY。
    """
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表 + 补列 + 补新表 + 记录版本（幂等）。正式初始化走 script/init_db.py。

    注意要带上 schema.migrate：只跑 DDL 的话，老库上 CREATE TABLE IF NOT EXISTS
    不会给已存在的表补新列，会得到一个"表在、列不全"的半迁移库。
    新表靠 DDL 的 IF NOT EXISTS 建；版本号靠 stamp_version 补上。
    """
    conn.executescript(schema.DDL)
    schema.migrate(conn)
    schema.stamp_version(conn)
    conn.commit()


# =====================================================================
# 数据清洗工具：baostock 返回字符串，空值为 ""，需转成 None / 数值
# =====================================================================
def _f(v):
    """转 float；空值/非法值返回 None。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _i(v):
    """转 int；空值/非法值返回 None。"""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pick_fields(fields, allowed, default, table: str) -> tuple:
    """校验要查询的列名（白名单），空则用默认列；出现非法列直接报错。

    这些列名会拼进 SQL 字符串，虽然当前调用方都传常量元组，
    但作为公共 API 必须自己卡住（将来接 ?fields= 之类的参数就是注入口）。
    """
    if not fields:
        return tuple(default)
    cols = tuple(fields)
    bad = [f for f in cols if f not in allowed]
    if bad:
        raise ValueError(f"{table} 不允许查询的列：{bad}（可选：{list(allowed)}）")
    return cols


# =====================================================================
# K 线（kline 表）
# =====================================================================
# 除 code/date/ktype 之外的字段（与建表语句顺序一致）
KLINE_VALUE_FIELDS = (
    "open", "high", "low", "close", "close_raw", "preclose", "volume", "amount",
    "turn", "pct_chg", "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm",
    "div_yield", "is_st",
)

#: 重写 K 线时**必须保留**原值的列（新值为 NULL 时不覆盖）。
#:
#: close_raw 是不复权收盘价，取自另一路 baostock 查询，**不由 K 线数据推导**：
#: 一旦被抹掉就再也算不回来（只能重新联网拉一次）。而 K 线重写（前复权历史修正、
#: 新分红重拉）本来就不该动它 —— 所以这里用 COALESCE 保住。
#:
#: div_yield 故意**不**保留：它是由 close_raw + dividend 离线算出来的，
#: 前复权历史被重写后旧值就失效了，必须置空让 fill_dividend_yields 重算。
_KLINE_KEEP_ON_REWRITE = ("close_raw",)

_INSERT_KLINE_SQL = (
    "INSERT INTO kline(code,date,ktype," + ",".join(KLINE_VALUE_FIELDS) + ") "
    "VALUES(" + ",".join(["?"] * (3 + len(KLINE_VALUE_FIELDS))) + ") "
    "ON CONFLICT(code,date) DO UPDATE SET "
    + ",".join(
        (f"{f}=COALESCE(excluded.{f},kline.{f})" if f in _KLINE_KEEP_ON_REWRITE
         else f"{f}=excluded.{f}")
        for f in KLINE_VALUE_FIELDS)
)


def upsert_kline(conn, code: str, ktype: str, rows, batch: int = 2000) -> int:
    """批量写入 K 线（UPSERT；重写时保留 close_raw，见 _KLINE_KEEP_ON_REWRITE）。

    code   标的代码，如 sh.600000 / sh.000300
    ktype  'stock' 或 'index'
    rows   [{字段: 值}]，须含 date；其余字段缺失按 None 处理
    batch  每多少行提交一次（大历史数据避免单次事务过大）
    返回写入行数

    这里**不能**用 INSERT OR REPLACE：那是"先 DELETE 再 INSERT"，
    没出现在 rows 里的列（例如取数阶段才写的 close_raw）会被一起抹成 NULL。
    曾经就是因为这个 + 后续回补失败，导致 212 只个股的 close_raw 整条丢失。
    """
    payload = []
    for r in rows:
        vals = tuple(_i(r.get(f)) if f == "is_st" else _f(r.get(f))
                     for f in KLINE_VALUE_FIELDS)
        payload.append((code, r.get("date"), ktype) + vals)
    if not payload:
        return 0
    total = 0
    for i in range(0, len(payload), batch):
        chunk = payload[i:i + batch]
        conn.executemany(_INSERT_KLINE_SQL, chunk)
        conn.commit()
        total += len(chunk)
    return total


# 允许被 load_kline 选取的列（列名会拼进 SQL，必须白名单化）
KLINE_QUERY_FIELDS = ("date", "ktype") + KLINE_VALUE_FIELDS


def load_kline(conn, code: str, start: str | None = None, end: str | None = None,
               fields=None) -> list[dict]:
    """读取某标的 K 线（按日期升序）。

    fields 为 None 时返回 date 及全部指标字段。
    fields 里的列名会拼进 SQL，因此必须落在白名单内（防注入）。
    """
    cols = _pick_fields(fields, KLINE_QUERY_FIELDS, ("date",) + KLINE_VALUE_FIELDS,
                        "kline")
    sql = f"SELECT {','.join(cols)} FROM kline WHERE code=?"
    params = [code]
    if start:
        sql += " AND date>=?"
        params.append(start)
    if end:
        sql += " AND date<=?"
        params.append(end)
    sql += " ORDER BY date"
    return [dict(r) for r in conn.execute(sql, params)]


def latest_kline_date(conn, code: str) -> str | None:
    """某标的已入库的最新日期；无数据返回 None（增量拉取的起点）。"""
    row = conn.execute("SELECT MAX(date) FROM kline WHERE code=?", (code,)).fetchone()
    return row[0] if row and row[0] else None


def latest_kline_dates(conn, ktype: str | None = None) -> dict[str, str]:
    """一次取出所有标的的最新日期 {code: date}，避免逐只查询。"""
    sql = "SELECT code, MAX(date) AS d FROM kline"
    params = []
    if ktype:
        sql += " WHERE ktype=?"
        params.append(ktype)
    sql += " GROUP BY code"
    return {r["code"]: r["d"] for r in conn.execute(sql, params)}


def code_ktypes(conn) -> dict[str, str]:
    """一次取出所有 K 线标的的类型 {code: ktype}（stock / index / portfolio）。"""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT code, ktype FROM kline GROUP BY code")}


def stock_name(conn, code: str) -> str:
    """标的名称（stock_basic 里查不到时回退成代码本身）。"""
    row = conn.execute("SELECT name FROM stock_basic WHERE code=?",
                       (code,)).fetchone()
    return (row["name"] if row and row["name"] else "") or code


VALUATION_FIELDS = ("pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm", "div_yield")


def update_index_valuations(conn, code: str, rows, batch: int = 2000,
                            fields=None) -> int:
    """批量写回估值字段。

    rows:   [{date, pe_ttm, pb_mrq, ps_ttm, pcf_ncf_ttm, div_yield}]
            各行应提供相同的字段集合
    fields: 明确指定要更新的列；为 None 时**从 rows 的键推断**
            （只更新实际给出的字段，避免把未提供的字段清成 NULL）

    例：只更新股息率时传 [{"date": d, "div_yield": v}]，
        则 SQL 只更新 div_yield，不会动 pe_ttm 等字段。
    """
    rows = [r for r in rows if r.get("date")]
    if not rows:
        return 0
    if fields is None:
        given = set()
        for r in rows:
            given |= set(r.keys())
        fields = [c for c in VALUATION_FIELDS if c in given]
    fields = [c for c in fields if c in VALUATION_FIELDS]
    if not fields:
        return 0
    sets = ",".join(f"{c}=?" for c in fields)
    sql = f"UPDATE kline SET {sets} WHERE code=? AND date=?"
    payload = [tuple(_f(r.get(c)) for c in fields) + (code, r.get("date"))
               for r in rows]
    total = 0
    for i in range(0, len(payload), batch):
        chunk = payload[i:i + batch]
        conn.executemany(sql, chunk)
        conn.commit()
        total += len(chunk)
    return total


def update_valuation_fields(conn, code: str, rows, batch: int = 2000,
                            fields=None) -> int:
    """批量更新任意标的的估值字段（语义化包装，见 update_index_valuations）。

    个股：写回动态股息率 div_yield
    指数：写回成分股聚合估值（五个字段）
    只更新 rows 中实际给出的字段。
    """
    return update_index_valuations(conn, code, rows, batch, fields)


def update_close_raw(conn, code: str, rows, batch: int = 2000) -> int:
    """批量写回不复权收盘价 close_raw（UPDATE，不触碰其它字段）。

    close_raw 是取数阶段落的原始数据，必须用 UPDATE 定点写，
    不能用 upsert_kline 的 INSERT OR REPLACE（那会把整行其它字段抹成 NULL）。
    """
    payload = [(_f(r.get("close_raw")), code, r.get("date"))
               for r in rows if r.get("date")]
    if not payload:
        return 0
    sql = "UPDATE kline SET close_raw=? WHERE code=? AND date=?"
    total = 0
    for i in range(0, len(payload), batch):
        before = conn.total_changes
        conn.executemany(sql, payload[i:i + batch])
        conn.commit()
        # 返回**真实更新行数**（不是 payload 长度）：如果 (code,date) 对不上，
        # 真实更新是 0，调用方/日志才看得出来 —— 以前返回 payload 长度，
        # 明明一行都没写上也报"成功 N 行"。
        total += conn.total_changes - before
    return total


def count_kline(conn, code: str | None = None, ktype: str | None = None) -> int:
    """K 线行数（可按标的/类型过滤）。"""
    sql, params, conds = "SELECT COUNT(*) FROM kline", [], []
    if code:
        conds.append("code=?")
        params.append(code)
    if ktype:
        conds.append("ktype=?")
        params.append(ktype)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    return conn.execute(sql, params).fetchone()[0]


def count_stocks(conn) -> int:
    """有个股K线的个股数量（与 list_stocks 同口径，但不做 LIMIT 截断）。

    list_stocks 为「个股管理」侧栏渲染加了 LIMIT 500，首页"已拉取个股"不能复用它，
    否则超过 500 只时数字会停在 500。
    """
    return conn.execute(
        "SELECT COUNT(DISTINCT code) FROM kline WHERE ktype='stock'").fetchone()[0]


# =====================================================================
# 指数成分股（index_constituent 表）
# =====================================================================
def save_constituents(conn, index_code: str, stock_codes,
                      snapshot: str | None = None) -> tuple[int, int]:
    """保存指数成分股快照。

    先将该指数已有记录全部置 is_active=0，再写入本次列表（is_active=1）。
    返回 (写入数, 停用数)
    """
    # 空列表直接返回：调用方可能拿到"接口成功但 0 行"的空结果，
    # 若照常执行会把该指数全部成分股置为停用（此后抓不到任何个股数据）
    if not stock_codes:
        log.warning("%s 成分股为空，保留库里已有记录（不执行停用）", index_code)
        return 0, 0
    snap = snapshot or datetime.now().strftime("%Y-%m-%d")
    now = _now()
    cur = conn.execute(
        "UPDATE index_constituent SET is_active=0, updated_at=? "
        "WHERE index_code=? AND is_active=1", (now, index_code))
    deactivated = cur.rowcount
    payload = [(index_code, c, snap, 1, now) for c in stock_codes]
    if payload:
        conn.executemany(
            "INSERT OR REPLACE INTO index_constituent"
            "(index_code,stock_code,snapshot,is_active,updated_at) VALUES(?,?,?,?,?)",
            payload)
    conn.commit()
    return len(payload), deactivated


def load_constituents(conn, index_code: str, active_only: bool = True) -> list[str]:
    """读取指数成分股代码列表。"""
    sql = "SELECT DISTINCT stock_code FROM index_constituent WHERE index_code=?"
    if active_only:
        sql += " AND is_active=1"
    sql += " ORDER BY stock_code"
    return [r[0] for r in conn.execute(sql, (index_code,))]




# =====================================================================
# 分红（dividend 表）
# =====================================================================
def upsert_dividends(conn, code: str, rows) -> int:
    """写入分红记录。rows: [{ex_date, cash_ps, stock_ps}]。"""
    payload = [(code, r.get("ex_date"), _f(r.get("cash_ps")), _f(r.get("stock_ps")))
               for r in rows if r.get("ex_date")]
    if not payload:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO dividend(code,ex_date,cash_ps,stock_ps) "
        "VALUES(?,?,?,?)", payload)
    conn.commit()
    return len(payload)


def load_dividends(conn, code: str, since: str | None = None) -> list[dict]:
    """读取分红记录（按除息日升序）；since 用于只取某日期之后的记录。"""
    sql = "SELECT ex_date, cash_ps, stock_ps FROM dividend WHERE code=?"
    params = [code]
    if since:
        sql += " AND ex_date>=?"
        params.append(since)
    sql += " ORDER BY ex_date"
    return [dict(r) for r in conn.execute(sql, params)]




# =====================================================================
# 标的元信息（stock_basic 表）
# =====================================================================
def upsert_stock_basic(conn, rows) -> int:
    """写入标的元信息。rows: [{code, name, ktype, market, industry, listed_date}]。

    **不能用 INSERT OR REPLACE**：那是"先 DELETE 再 INSERT"，没出现的列会被抹成
    NULL。而 `sync_constituents` 每次拉取都会把**所有成分股**upsert 进来，那条路径
    只带 {code,name,ktype,market} —— 于是每跑一次拉取，就把 `sync_stock_basics`
    刚补好的 industry / listed_date 全部抹掉一次，覆盖率永远停在个位数百分比。
    改为 UPSERT + COALESCE：新值为 NULL 时保留原值。
    """
    now = _now()
    payload = [(r.get("code"), r.get("name"), r.get("ktype"), r.get("market"),
                r.get("industry"), r.get("listed_date"), now)
               for r in rows if r.get("code")]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO stock_basic"
        "(code,name,ktype,market,industry,listed_date,updated_at) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET "
        "name=COALESCE(excluded.name, stock_basic.name), "
        "ktype=COALESCE(excluded.ktype, stock_basic.ktype), "
        "market=COALESCE(excluded.market, stock_basic.market), "
        "industry=COALESCE(excluded.industry, stock_basic.industry), "
        "listed_date=COALESCE(excluded.listed_date, stock_basic.listed_date), "
        "updated_at=excluded.updated_at",
        payload)
    conn.commit()
    return len(payload)


def load_stock_basic(conn, code: str | None = None, ktype: str | None = None) -> list[dict]:
    """读取标的元信息（可按代码或类型过滤）。"""
    sql = "SELECT * FROM stock_basic"
    conds, params = [], []
    if code:
        conds.append("code=?")
        params.append(code)
    if ktype:
        conds.append("ktype=?")
        params.append(ktype)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY code"
    return [dict(r) for r in conn.execute(sql, params)]


# =====================================================================
# 历史邮件（mail_log 表）—— 标题+摘要+状态；正文快照见 mail_body
# =====================================================================
def add_mail_log(conn, subject: str, summary: str, receivers: list | str,
                 kind: str = "manual", ok: bool = True,
                 body_key: str | None = None) -> None:
    """记一条发送记录（供首页"历史邮件列表"）。

    body_key 指向 mail_body 里那封邮件的自包含正文，首页点击即可原样回看。
    """
    to = ", ".join(receivers) if isinstance(receivers, (list, tuple)) else str(receivers or "")
    conn.execute(
        "INSERT INTO mail_log(sent_at, subject, summary, receivers, kind, ok, body_key) "
        "VALUES(?,?,?,?,?,?,?)",
        (_now(), subject, summary, to, kind, 1 if ok else 0, body_key))
    conn.commit()


def load_mail_log(conn, limit: int = 50) -> list[dict]:
    """历史邮件列表（新→旧），带"是否有正文快照"标记。"""
    rows = conn.execute(
        """SELECT id, sent_at, subject, summary, receivers, kind, ok, body_key,
                  (body_key IS NOT NULL) AS _has_key
           FROM mail_log ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("_has_key", None)
        key = d.get("body_key")
        d["has_body"] = bool(key) and conn.execute(
            "SELECT 1 FROM mail_body WHERE body_key=?", (key,)).fetchone() is not None
        out.append(d)
    return out


# =====================================================================
# 邮件正文快照（mail_body 表）—— 首页点击邮件时原样回看
# =====================================================================
def save_mail_body(conn, body_key: str, subject: str, html: str) -> None:
    """保存一封邮件的**自包含**正文（图片已内联为 base64）。"""
    conn.execute(
        "INSERT OR REPLACE INTO mail_body(body_key, subject, html, created_at) "
        "VALUES(?,?,?,?)", (body_key, subject, html, _now()))
    conn.commit()


def load_mail_body(conn, body_key: str) -> dict | None:
    row = conn.execute("SELECT * FROM mail_body WHERE body_key=?", (body_key,)).fetchone()
    return dict(row) if row else None


def load_mail_body_by_id(conn, mail_id: int) -> dict | None:
    """按 mail_log.id 取对应正文快照（含邮件本身的元信息）。"""
    row = conn.execute(
        """SELECT b.html, b.subject AS body_subject, b.created_at,
                  m.id, m.sent_at, m.subject, m.summary, m.receivers, m.kind, m.ok
           FROM mail_log m LEFT JOIN mail_body b ON b.body_key = m.body_key
           WHERE m.id=?""", (mail_id,)).fetchone()
    return dict(row) if row else None


def purge_old_mail_body(conn, days: int = 7) -> int:
    """删除 N 天前的正文快照，防止库体积无限增长。"""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    cur = conn.execute("DELETE FROM mail_body WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# =====================================================================
# 暂存邮件（pending_alert 表）—— 通知任务"构建→发送→重发"用
# =====================================================================
def save_pending_mail(conn, build_date: str, subject: str, summary: str,
                      html: str, receivers: list | str, is_alert: bool) -> None:
    """暂存（或覆盖）某天的邮件正文（一天一封）。

    重新构建时**保留 sent_count**：正文被最新数据替换，但"今天已经发过几次"
    这件事不该被抹掉，否则下一次发送/重发时点会重复发信。
    """
    to = ", ".join(receivers) if isinstance(receivers, (list, tuple)) else str(receivers or "")
    conn.execute(
        """INSERT INTO pending_alert
           (build_date, subject, summary, html, receivers, is_alert, sent_count, created_at, updated_at)
           VALUES(?,?,?,?,?,?,0,?,?)
           ON CONFLICT(build_date) DO UPDATE SET
             subject=excluded.subject, summary=excluded.summary, html=excluded.html,
             receivers=excluded.receivers, is_alert=excluded.is_alert,
             updated_at=excluded.updated_at""",
        (build_date, subject, summary, html, to, 1 if is_alert else 0, _now(), _now()))
    conn.commit()


def load_pending_mail(conn, build_date: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM pending_alert WHERE build_date=? ORDER BY id DESC LIMIT 1",
        (build_date,)).fetchone()
    return dict(row) if row else None


def bump_pending_sent(conn, build_date: str) -> int:
    """已发送次数 +1，返回新的发送次数。"""
    conn.execute(
        "UPDATE pending_alert SET sent_count=sent_count+1, updated_at=? WHERE build_date=?",
        (_now(), build_date))
    conn.commit()
    row = conn.execute("SELECT sent_count FROM pending_alert WHERE build_date=?",
                       (build_date,)).fetchone()
    return row[0] if row else 0


def save_pending_images(conn, build_date: str, images) -> int:
    """暂存正文里的内联图片 [(cid, png), ...]（重发时作为 cid 附件带上）。"""
    if not images:
        return 0
    conn.execute("DELETE FROM pending_image WHERE build_date=?", (build_date,))
    conn.executemany(
        "INSERT OR REPLACE INTO pending_image(build_date, cid, png) VALUES(?,?,?)",
        [(build_date, cid, sqlite3.Binary(png)) for cid, png in images])
    conn.commit()
    return len(images)


def load_pending_images(conn, build_date: str) -> list:
    """取暂存的内联图片，返回 [(cid, png_bytes), ...]（供 mailer 内联）。"""
    rows = conn.execute(
        "SELECT cid, png FROM pending_image WHERE build_date=? ORDER BY cid",
        (build_date,)).fetchall()
    return [(r["cid"], bytes(r["png"])) for r in rows]


def purge_old_pending(conn, days: int = 7) -> int:
    """删除 N 天前的暂存邮件正文与内联图片，防止磁盘占满。"""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    cur = conn.execute("DELETE FROM pending_alert WHERE build_date < ?", (cutoff,))
    n = cur.rowcount
    cur2 = conn.execute("DELETE FROM pending_image WHERE build_date < ?", (cutoff,))
    conn.commit()
    return n + cur2.rowcount


# =====================================================================
# 目标与成分股维护（Web 管理页用）
# =====================================================================
def set_target_weights(conn, code: str, weights: dict) -> int:
    """只更新某标的的五指标权重列。返回受影响行数（0=目标不存在）。"""
    cur = conn.execute(
        """UPDATE valuation_target SET w_pe=?, w_pb=?, w_ps=?, w_pcf=?, w_dividend=?
           WHERE code=?""",
        (weights.get("pe", 0.0), weights.get("pb", 0.0), weights.get("ps", 0.0),
         weights.get("pcf", 0.0), weights.get("dividend", 0.0), code))
    conn.commit()
    return cur.rowcount


def add_target(conn, code: str, name: str, ktype: str, weights: dict,
               enabled: bool = True, sort_order: int = 0, remark: str = "") -> None:
    """新增一个估值目标（指数/组合共用）。"""
    conn.execute(
        """INSERT OR REPLACE INTO valuation_target
           (code,name,ktype,enabled,w_pe,w_pb,w_ps,w_pcf,w_dividend,sort_order,remark)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (code, name, ktype, 1 if enabled else 0,
         weights.get("pe", 0.0), weights.get("pb", 0.0), weights.get("ps", 0.0),
         weights.get("pcf", 0.0), weights.get("dividend", 0.0),
         sort_order, remark))
    conn.commit()


def upsert_target_weights(conn, code: str, weights: dict,
                          name: str | None = None,
                          ktype: str | None = None) -> int:
    """设置某标的的评分权重；目标不存在则**新建**（供页面直接给任意个股/指数配权重）。

    返回受影响/新建行数（正常为 1）。
    """
    n = set_target_weights(conn, code, weights)
    if n:
        return n
    b = conn.execute("SELECT name, ktype FROM stock_basic WHERE code=?",
                     (code,)).fetchone()
    nm = name or (b["name"] if b else None) or code
    kt = ktype or (b["ktype"] if b else None) or "stock"
    if kt not in ("index", "portfolio", "stock"):
        kt = "stock"
    nxt = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM valuation_target").fetchone()[0]
    add_target(conn, code, nm, kt, weights, enabled=True, sort_order=nxt,
               remark="页面配置")
    return 1


def set_target_order(conn, codes) -> int:
    """按给定顺序重设 valuation_target.sort_order（1..N）。

    标的信息的展示顺序与**邮件里的顺序**都用这一列，所以调完两边一致。
    返回实际更新的行数。
    """
    n = 0
    for i, code in enumerate(codes, 1):
        cur = conn.execute("UPDATE valuation_target SET sort_order=? WHERE code=?",
                           (i, code))
        n += cur.rowcount
    conn.commit()
    return n


def delete_target(conn, code: str) -> int:
    """删除目标及其成分股、评分记录。返回删除的目标行数。"""
    n = conn.execute("DELETE FROM valuation_target WHERE code=?", (code,)).rowcount
    conn.execute("DELETE FROM index_constituent WHERE index_code=?", (code,))
    conn.execute("DELETE FROM valuation_score WHERE code=?", (code,))
    conn.commit()
    return n


def add_constituent(conn, index_code: str, stock_code: str,
                    snapshot: str | None = None) -> int:
    """把一个成分股置为有效（幂等）。返回 1=本次新增，0=本来就是有效成分股。

    已经是有效成分股时直接返回，不再写库：既避免重复插入快照行，
    也让调用方知道"这次到底有没有变化"（决定要不要重算组合）。
    """
    cur = conn.execute(
        "UPDATE index_constituent SET updated_at=? "
        "WHERE index_code=? AND stock_code=? AND is_active=1",
        (_now(), index_code, stock_code))
    if cur.rowcount:
        conn.commit()
        return 0
    snap = snapshot or datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """INSERT OR REPLACE INTO index_constituent
           (index_code, stock_code, snapshot, is_active, updated_at)
           VALUES(?,?,?,1,?)""",
        (index_code, stock_code, snap, _now()))
    conn.commit()
    return 1


def remove_constituent(conn, index_code: str, stock_code: str) -> int:
    """把一个成分股从指数/组合中移除（置为停用，保留历史快照）。"""
    cur = conn.execute(
        "UPDATE index_constituent SET is_active=0, updated_at=? "
        "WHERE index_code=? AND stock_code=? AND is_active=1",
        (_now(), index_code, stock_code))
    conn.commit()
    return cur.rowcount


def constituent_rows(conn, index_code: str) -> list[dict]:
    """指数/组合的当前成分股明细（连 stock_basic 补名称/类型/行业）。"""
    rows = conn.execute(
        """SELECT c.stock_code AS code, COALESCE(s.name, c.stock_code) AS name,
                  COALESCE(s.ktype, 'stock') AS ktype,
                  COALESCE(s.market, '') AS market,
                  COALESCE(s.industry, '') AS industry
           FROM index_constituent c
           LEFT JOIN stock_basic s ON s.code = c.stock_code
           WHERE c.index_code=? AND c.is_active=1
           GROUP BY c.stock_code
           ORDER BY c.stock_code""", (index_code,)).fetchall()
    return [dict(r) for r in rows]


def search_stocks(conn, q: str, limit: int = 20) -> list[dict]:
    """按代码/名称模糊搜索股票（stock_basic 优先，兜底扫 kline 里出现过的代码）。"""
    like = f"%{q}%"
    rows = conn.execute(
        """SELECT code, name, ktype, market, industry FROM stock_basic
           WHERE code LIKE ? OR name LIKE ?
           ORDER BY code LIMIT ?""", (like, like, limit)).fetchall()
    out = [dict(r) for r in rows]
    if len(out) < limit:
        # 兜底：还没进 stock_basic 但已有 K 线的代码（例如刚抓的成分股）
        have = {r["code"] for r in out}
        for r in conn.execute(
                "SELECT DISTINCT code FROM kline WHERE code LIKE ? ORDER BY code LIMIT ?",
                (like, limit)):
            if r[0] not in have:
                out.append({"code": r[0], "name": r[0], "ktype": "stock",
                            "market": r[0].split(".")[0], "industry": ""})
        out = out[:limit]
    return out


def list_stocks(conn, limit: int = 60, after: str | None = None) -> list[dict]:
    """列出"有个股K线"的个股清单（个股管理页侧栏，游标分页），附最新K线日期。

    两步走：① 用覆盖索引取本页 code（`code > after` 游标，常数时间，
               比 OFFSET 深翻页快十几倍）；② 再对本页 code 取名称与最新日期。
    """
    sql = "SELECT DISTINCT code FROM kline WHERE ktype='stock'"
    params: list = []
    if after:
        sql += " AND code>?"
        params.append(after)
    sql += " ORDER BY code LIMIT ?"
    params.append(limit)
    codes = [r[0] for r in conn.execute(sql, params)]
    if not codes:
        return []
    ph = ",".join("?" * len(codes))
    rows = conn.execute(
        f"""SELECT k.code, COALESCE(s.name, k.code) AS name,
                   COALESCE(s.market, '') AS market, MAX(k.date) AS latest_kline
            FROM kline k LEFT JOIN stock_basic s ON s.code = k.code
            WHERE k.ktype='stock' AND k.code IN ({ph})
            GROUP BY k.code ORDER BY k.code""", codes).fetchall()
    return [{"code": r["code"], "name": r["name"], "market": r["market"],
             "latest_kline": r["latest_kline"]} for r in rows]


def kline_span(conn, code: str) -> dict:
    """某标的 K 线的起止日期与行数（走主键，快）。"""
    row = conn.execute(
        "SELECT MIN(date) AS first, MAX(date) AS last, COUNT(*) AS n "
        "FROM kline WHERE code=?", (code,)).fetchone()
    return {"first": row["first"], "last": row["last"], "rows": row["n"] or 0}


def update_stock_basic_info(conn, rows, batch: int = 300) -> int:
    """只更新 stock_basic 的 industry / listed_date（不动 name/ktype/market）。

    rows: [{code, industry?, listed_date?}]；某字段为 None/缺省时保持原值。
    用定点 UPDATE 而不是 upsert_stock_basic——后者是 INSERT OR REPLACE，
    只带行业/上市日会把已有的 name/ktype/market 抹成 NULL。

    **按批提交**（而不是全部更新完再提交一次）：全市场有几千条，中途一旦失败，
    一次性提交会让**已经写成功的部分全部回滚** —— 实测生产库里这两个字段的覆盖率
    长期停在 6%，就是这么一点点丢掉的。按批提交保证部分成功也能留下。
    """
    now = _now()
    n = 0
    for i, r in enumerate(rows, 1):
        code = r.get("code")
        if not code:
            continue
        sets, params = [], []
        for col in ("industry", "listed_date"):
            if r.get(col) is not None:
                sets.append(f"{col}=?")
                params.append(r[col])
        if not sets:
            continue
        sets.append("updated_at=?")
        params.extend([now, code])
        cur = conn.execute(
            f"UPDATE stock_basic SET {','.join(sets)} WHERE code=?", params)
        n += cur.rowcount
        if i % batch == 0:
            conn.commit()
    conn.commit()
    return n


# =====================================================================
# 同步状态（sync_state 表）—— 增量拉取与断点续传
# =====================================================================
def get_sync(conn, code: str, dtype: str) -> dict | None:
    """读取某标的某类数据的同步状态。"""
    row = conn.execute(
        "SELECT code,dtype,last_date,row_count,updated_at FROM sync_state "
        "WHERE code=? AND dtype=?", (code, dtype)).fetchone()
    return dict(row) if row else None


#: sync_state 里"分位历史已补齐"的 dtype 标记
PCT_HISTORY_DTYPE = "pct_history"
#: sync_state 里"历史分位需要整条重算"的 dtype 标记（输入数据变了）
PCT_DIRTY_DTYPE = "pct_dirty"


def score_kline_coverage(conn) -> dict:
    """每个标的的 K 线行数 / 评分行数，用来判断**分位历史是否残缺**。

    只看「最新一天有没有评分」是不够的：后加入的个股（例如刚被拉进个股池的）
    可能只在最新一天算过一次，历史整条都是空的 —— 那要靠行数对比才看得出来。
    """
    kl = {r[0]: r[1] for r in conn.execute(
        "SELECT code, count(*) FROM kline GROUP BY code")}
    sc = {r[0]: r[1] for r in conn.execute(
        "SELECT code, count(*) FROM valuation_score GROUP BY code")}
    return {code: {"kline": n, "scores": sc.get(code, 0)}
            for code, n in kl.items()}


def history_built_codes(conn) -> set:
    """已补齐过分位历史的标的（sync_state 里打了 pct_history 标记的）。"""
    return {r[0] for r in conn.execute(
        "SELECT code FROM sync_state WHERE dtype=?", (PCT_HISTORY_DTYPE,))}


def mark_history_built(conn, code: str, last_date: str | None = None,
                       row_count: int = 0) -> None:
    """标记某标的的分位历史已补齐。

    打上之后只补最新交易日，不再全量重建；否则每天都会把整条历史重算一遍，
    而且早期日期（窗口长度不够、分位天然算不出来）会永远被当成"缺失"反复重试。
    """
    set_sync(conn, code, PCT_HISTORY_DTYPE, last_date=last_date,
             row_count=row_count, incremental=False)


#: sync_state 里"计算指标上次跑完时的数据指纹"的哨兵 code（不是真实标的）
COMPUTE_FP_CODE = "__compute__"
#: sync_state 里"某组合K线上次合成时的数据指纹"
PF_FP_DTYPE = "pf_fp"
#: sync_state 里"前复权历史修正重拉失败，需要重试"的待办标记
READJUST_DTYPE = "kline_readjust"


def mark_readjust_pending(conn, code: str, reason: str = "") -> None:
    """标记某标的的**前复权历史需要重拉修正**（上次重拉失败了）。

    为什么必须单独记：新分红会让 baostock 的前复权历史整体变化，所以第 ⑦ 步要
    全历史重拉。但分红记录这时**已经落库**了，下一轮 `sync_dividends` 不会再报
    "新分红"，重拉就再也不会被触发 —— 失败会静默永久丢失，库里那只股票的历史
    会**混着两套复权基准**（旧的一段 + 新的一段），且不报任何错。
    """
    set_sync(conn, code, READJUST_DTYPE, last_date=_now()[:10], row_count=0,
             incremental=False)
    if reason:
        log.warning("%s 前复权历史需重拉（已记为待办）：%s", code, reason)


def readjust_pending_codes(conn) -> set:
    """前复权历史待重拉修正的标的。"""
    return {r[0] for r in conn.execute(
        "SELECT code FROM sync_state WHERE dtype=?", (READJUST_DTYPE,))}


def clear_readjust_pending(conn, code: str) -> None:
    """重拉成功后清掉待办标记。"""
    conn.execute("DELETE FROM sync_state WHERE code=? AND dtype=?",
                 (code, READJUST_DTYPE))
    conn.commit()


#: 取数侧（data_fetcher）写的 sync_state dtype —— 只有它们变了才算"数据变了"。
#: 用白名单而不是黑名单：计算任务以后再加新的 dtype 也不会悄悄把闸门弄坏
#: （黑名单就踩过这个坑：计算任务自己写 pf_fp，把指纹改了，早退永远不生效）。
_FETCH_DTYPES = ("kline", "dividend", "constituent", "close_raw",
                 "trade_date", "kline_full")


def data_fingerprint(conn) -> str:
    """数据指纹 = **取数侧**最后写入的时间戳，用于"数据没变就不用重算"。

    只认上面白名单里的 dtype（都是 data_fetcher 写的），计算任务自己写的
    （pct_history / pct_dirty / pf_fp / 哨兵行）一律不算 —— 否则计算任务
    一写标记就把指纹改了，"数据没变就早退"永远不生效。

    实测 0.3ms（sync_state 只有两千行），比"重建一遍再发现没事"便宜太多。
    """
    ph = ",".join("?" * len(_FETCH_DTYPES))
    row = conn.execute(
        f"SELECT MAX(updated_at) FROM sync_state WHERE dtype IN ({ph})",
        _FETCH_DTYPES).fetchone()
    return (row[0] if row and row[0] else "")


def get_compute_fingerprint(conn) -> str | None:
    """上次"数据没变、也没留下待办"时记录的数据指纹；没有则 None。"""
    rec = get_sync(conn, COMPUTE_FP_CODE, "fingerprint")
    if rec and rec.get("row_count") == 1:
        return rec.get("last_date")
    return None


def set_compute_fingerprint(conn, fingerprint: str) -> None:
    """记录"这次跑完是干净的"（数据没变 + 没有待办），下次可据此早退。"""
    set_sync(conn, COMPUTE_FP_CODE, "fingerprint", last_date=fingerprint,
             row_count=1, incremental=False)


def get_portfolio_fingerprint(conn, code: str) -> str | None:
    """某组合上次合成 K 线时的数据指纹。"""
    rec = get_sync(conn, code, PF_FP_DTYPE)
    return rec.get("last_date") if rec else None


def set_portfolio_fingerprint(conn, code: str, fingerprint: str) -> None:
    set_sync(conn, code, PF_FP_DTYPE, last_date=fingerprint, row_count=1,
             incremental=False)


def mark_history_dirty(conn, code: str, reason: str = "") -> None:
    """标记某标的的历史分位**需要整条重算**（它的输入数据变了）。

    这是"取数改了原始数据 → 计算要重算派生历史"之间缺的那一环：
    例如 `close_raw` 补齐后 `div_yield` 才算得出来，历史股息率分位就全变了。
    光靠"评分行数/K线行数"的覆盖率判据发现不了这种变化（K线行数没变），
    所以必须由**改数据的那一方**显式打标记。

    同时清掉"已补齐"标记 —— 否则 history_gap_codes 会以为这只已经核对过。
    """
    set_sync(conn, code, PCT_DIRTY_DTYPE, last_date=_now()[:10], row_count=0,
             incremental=False)
    conn.execute("DELETE FROM sync_state WHERE code=? AND dtype=?",
                 (code, PCT_HISTORY_DTYPE))
    conn.commit()
    if reason:
        log.info("%s 的历史分位已标记为需重算：%s", code, reason)


def dirty_codes(conn) -> set:
    """历史分位需要重算的标的。"""
    return {r[0] for r in conn.execute(
        "SELECT code FROM sync_state WHERE dtype=?", (PCT_DIRTY_DTYPE,))}


def clear_history_dirty(conn, code: str) -> None:
    """清掉"需要重算"的标记（重算完成后调用）。"""
    conn.execute("DELETE FROM sync_state WHERE code=? AND dtype=?",
                 (code, PCT_DIRTY_DTYPE))
    conn.commit()


def set_sync(conn, code: str, dtype: str, last_date: str | None = None,
             row_count: int = 0, incremental: bool = True) -> None:
    """记录同步进度。

    incremental=True 时仅在 last_date 前进时更新（避免回退覆盖已有进度）。
    """
    now = _now()
    cur = get_sync(conn, code, dtype)
    if incremental:
        if last_date is None:
            # 只更新行数，不要把已有进度清成 NULL（否则下次会全量重拉）
            if cur:
                conn.execute(
                    "UPDATE sync_state SET row_count=?, updated_at=? "
                    "WHERE code=? AND dtype=?", (row_count, now, code, dtype))
                conn.commit()
            return
        if cur and cur.get("last_date") and cur["last_date"] >= last_date:
            conn.execute(
                "UPDATE sync_state SET row_count=?, updated_at=? WHERE code=? AND dtype=?",
                (row_count, now, code, dtype))
            conn.commit()
            return
    conn.execute(
        "INSERT OR REPLACE INTO sync_state(code,dtype,last_date,row_count,updated_at) "
        "VALUES(?,?,?,?,?)", (code, dtype, last_date, row_count, now))
    conn.commit()






# =====================================================================
# 交易日历（trade_date 表）—— 离线判断交易日 / 计算增量区间
# =====================================================================
def upsert_trade_dates(conn, rows, source: str = "baostock",
                       batch: int = 2000) -> int:
    """批量写入交易日历。

    rows: [(date, is_open), ...] 或 [{"date":..., "is_open":...}, ...]
          is_open 支持 1/0、True/False、"1"/"0"
    返回写入行数。
    """
    now = _now()
    payload = []
    for r in rows:
        if isinstance(r, dict):
            d, op = r.get("date"), r.get("is_open")
        else:
            d, op = r[0], r[1]
        if not d:
            continue
        flag = 1 if str(op).strip().lower() in ("1", "true", "yes", "是") else 0
        payload.append((d, flag, source, now))
    for i in range(0, len(payload), batch):
        conn.executemany(
            "INSERT OR REPLACE INTO trade_date(date,is_open,source,updated_at) "
            "VALUES(?,?,?,?)", payload[i:i + batch])
    conn.commit()
    return len(payload)




def is_trading_day(conn, date: str) -> bool | None:
    """是否交易日。日历里没有该日期则返回 None（未知，调用方自行兜底）。"""
    row = conn.execute("SELECT is_open FROM trade_date WHERE date=?", (date,)).fetchone()
    if not row or row[0] is None:
        return None
    return bool(row[0])


def load_trade_dates(conn, start: str | None = None, end: str | None = None,
                     open_only: bool = False) -> list[dict]:
    """读取指定区间的交易日历（升序）。"""
    sql = "SELECT date,is_open,source FROM trade_date WHERE 1=1"
    params = []
    if start:
        sql += " AND date>=?"
        params.append(start)
    if end:
        sql += " AND date<=?"
        params.append(end)
    if open_only:
        sql += " AND is_open=1"
    sql += " ORDER BY date"
    return [dict(r) for r in conn.execute(sql, params)]


def latest_trade_date(conn, on_or_before: str | None = None,
                      open_only: bool = True) -> str | None:
    """不晚于指定日期的最近一个交易日（默认取全库最新交易日）。"""
    sql = "SELECT MAX(date) FROM trade_date"
    cond, params = [], []
    if on_or_before:
        cond.append("date<=?")
        params.append(on_or_before)
    if open_only:
        cond.append("is_open=1")
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    row = conn.execute(sql, params).fetchone()
    return row[0] if row and row[0] else None


def trade_date_range(conn) -> tuple[str | None, str | None]:
    """日历覆盖范围 (最早, 最晚)。"""
    row = conn.execute("SELECT MIN(date), MAX(date) FROM trade_date").fetchone()
    return (row[0], row[1]) if row else (None, None)


def count_trade_dates(conn, open_only: bool = False) -> int:
    """交易日历行数（open_only=True 时只数交易日）。"""
    sql = "SELECT COUNT(*) FROM trade_date"
    if open_only:
        sql += " WHERE is_open=1"
    return conn.execute(sql).fetchone()[0]


# =====================================================================
# 评分历史（valuation_score 表）—— 供评分/分位走势图使用
# =====================================================================
SCORE_FIELDS = ("score", "score5",
                "pct_pe", "pct_pb", "pct_ps", "pct_pcf", "pct_dividend",
                "pct5_pe", "pct5_pb", "pct5_ps", "pct5_pcf", "pct5_dividend",
                "status", "action", "n_used", "ktype")

# 允许被 load_scores 选取的列
SCORE_QUERY_FIELDS = ("date", "created_at") + SCORE_FIELDS


def upsert_scores(conn, rows, batch: int = 2000) -> int:
    """批量写入评分记录（INSERT OR REPLACE）。

    rows: [{code, date, ktype, score, score5, pct_pe, pct_pb, pct_ps, pct_pcf,
            pct_dividend, status, action, n_used}]
    返回写入行数。
    """
    now = _now()
    payload = []
    for r in rows:
        if not r.get("code") or not r.get("date"):
            continue
        vals = []
        for f in SCORE_FIELDS:
            v = r.get(f)
            if f in ("status", "action", "ktype"):
                vals.append(v)
            elif f == "n_used":
                vals.append(_i(v))
            else:
                vals.append(_f(v))
        payload.append(tuple([r["code"], r["date"]] + vals + [now]))
    if not payload:
        return 0
    cols = ("code", "date") + SCORE_FIELDS + ("created_at",)
    sql = (f"INSERT OR REPLACE INTO valuation_score({','.join(cols)}) "
           f"VALUES({','.join(['?'] * len(cols))})")
    total = 0
    for i in range(0, len(payload), batch):
        chunk = payload[i:i + batch]
        conn.executemany(sql, chunk)
        conn.commit()
        total += len(chunk)
    return total


def load_scores(conn, code: str, start: str | None = None, end: str | None = None,
                fields=None) -> list[dict]:
    """读取某标的的评分历史（按日期升序）；用于画评分走势图。

    fields 同样走白名单校验（列名拼进 SQL）。
    """
    cols = _pick_fields(fields, SCORE_QUERY_FIELDS, ("date",) + SCORE_FIELDS,
                        "valuation_score")
    sql = f"SELECT {','.join(cols)} FROM valuation_score WHERE code=?"
    params = [code]
    if start:
        sql += " AND date>=?"
        params.append(start)
    if end:
        sql += " AND date<=?"
        params.append(end)
    sql += " ORDER BY date"
    return [dict(r) for r in conn.execute(sql, params)]


def latest_score(conn, code: str) -> dict | None:
    """某标的最新一条评分记录。"""
    row = conn.execute(
        "SELECT * FROM valuation_score WHERE code=? ORDER BY date DESC LIMIT 1",
        (code,)).fetchone()
    return dict(row) if row else None




def score_before(conn, code: str, date: str) -> dict | None:
    """取某日期（不含）之前最近的一条评分记录（邮件里算"较前日"变化用）。"""
    row = conn.execute(
        "SELECT * FROM valuation_score WHERE code=? AND date<? "
        "ORDER BY date DESC LIMIT 1", (code, date)).fetchone()
    return dict(row) if row else None


def latest_score_dates(conn, ktype: str | None = None) -> dict[str, str]:
    """一次取出所有标的的最新评分日期 {code: date}。"""
    sql = "SELECT code, MAX(date) AS d FROM valuation_score"
    params = []
    if ktype:
        sql += " WHERE ktype=?"
        params.append(ktype)
    sql += " GROUP BY code"
    return {r["code"]: r["d"] for r in conn.execute(sql, params)}


def latest_score_dates_for(conn, codes) -> dict[str, str]:
    """只取**指定**标的的最新评分日期。

    用逐只主键 seek（`MAX(date) WHERE code=?`，走 (code,date) 索引，O(log n)）；
    写成 `code IN (300 个参数) GROUP BY code` 反而会让 SQLite 全扫索引（实测慢 50 倍）。
    """
    out = {}
    for c in codes or []:
        if not c:
            continue
        row = conn.execute("SELECT MAX(date) FROM valuation_score WHERE code=?",
                           (c,)).fetchone()
        if row and row[0]:
            out[c] = row[0]
    return out


def latest_kline_dates_for(conn, codes) -> dict[str, str]:
    """只取**指定**标的的最新 K 线日期（同样用逐只主键 seek）。"""
    out = {}
    for c in codes or []:
        if not c:
            continue
        row = conn.execute("SELECT MAX(date) FROM kline WHERE code=?",
                           (c,)).fetchone()
        if row and row[0]:
            out[c] = row[0]
    return out






def delete_kline(conn, code: str) -> int:
    """删除某标的的全部 K 线（组合全量重算前先清干净）。返回删除行数。"""
    cur = conn.execute("DELETE FROM kline WHERE code=?", (code,))
    conn.commit()
    return cur.rowcount


def delete_scores(conn, code: str, start: str | None = None) -> int:
    """删除某标的评分历史（用于重建）。返回删除行数。"""
    if start:
        cur = conn.execute("DELETE FROM valuation_score WHERE code=? AND date>=?",
                           (code, start))
    else:
        cur = conn.execute("DELETE FROM valuation_score WHERE code=?", (code,))
    conn.commit()
    return cur.rowcount


# =====================================================================
# 运维辅助
# =====================================================================
def table_stats(conn) -> dict:
    """各表行数统计（排查/监控用）。"""
    out = {}
    for name in ("kline", "index_constituent", "dividend", "stock_basic",
                 "sync_state", "valuation_score", "setting", "valuation_target",
                 "trade_date", "mail_log", "pending_alert", "schema_version"):
        try:
            out[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        except sqlite3.OperationalError:
            out[name] = None       # 表不存在
    return out
