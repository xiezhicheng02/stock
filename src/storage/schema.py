# -*- coding: utf-8 -*-
"""SQLite 表结构定义（baostock 数据 + 配置）。

设计要点
--------
1. kline        个股与指数 K 线共用一张表，用 ktype 区分；含 baostock 四个估值
                指标（pe_ttm/pb_mrq/ps_ttm/pcf_ncf_ttm）与动态股息率 div_yield。
                指数行的估值字段由成分股聚合后写回（本表即指数估值存放处）。
2. index_constituent  指数成分股（按 snapshot 快照日保留历史，便于成分股调整后回溯）。
3. dividend     分红原始数据，用于计算动态股息率（近12个月每股分红 / 当日收盘价）。
4. stock_basic  标的元信息（名称/类型/市场/行业）。
5. valuation_target   需要计算综合估值的指数与个股，含五个指标的权重。
6. setting      全局配置（KV + JSON），替代原 config.py 中的所有参数。
7. sync_state   增量同步状态，支持断点续传（baostock 偶发卡死时可恢复）。
8. trade_date   交易日历（baostock query_trade_dates 落库），
                供"非交易日不发信"与增量区间计算使用（离线可查，不依赖网络）。
9. schema_version     表结构版本，便于后续升级迁移。
"""

# 表结构版本：结构变更时 +1，并在 script/init_db.py 中处理迁移
#   1 → 初始表结构（8 张表）
#   2 → 新增 valuation_score（评分历史，用于走势图）
#   3 → 新增 trade_date（交易日历）；valuation_score 增加 pct5_*（5年分位）
#   4 → 新增 mail_log（历史邮件列表，供首页总览）
#   5 → 新增 pending_alert（暂存待发送/重发的告警邮件正文）
#   6 → kline 新增 close_raw（不复权收盘价，供动态股息率离线计算）
#   7 → 新增 mail_body（已发送邮件的自包含正文快照，供首页点击回看）；
#       mail_log 新增 body_key（指向 mail_body）
#   8 → 新增 pending_image（暂存邮件的内联图片，否则定时发送时图表全是破图）
SCHEMA_VERSION = 8

DDL = """
-- ① K 线表（个股 + 指数共用）
CREATE TABLE IF NOT EXISTS kline (
    code        TEXT    NOT NULL,          -- sh.600000（个股）/ sh.000300（指数）
    date        TEXT    NOT NULL,          -- YYYY-MM-DD
    ktype       TEXT    NOT NULL,          -- stock=个股, index=指数
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    close_raw   REAL,                      -- 不复权收盘价（供动态股息率离线计算）
    preclose    REAL,
    volume      REAL,
    amount      REAL,                      -- 成交额（元）
    turn        REAL,                      -- 换手率（%），指数无
    pct_chg     REAL,                      -- 涨跌幅（%）
    pe_ttm      REAL,                      -- 滚动市盈率（baostock peTTM）
    pb_mrq      REAL,                      -- 市净率（baostock pbMRQ）
    ps_ttm      REAL,                      -- 滚动市销率（baostock psTTM）
    pcf_ncf_ttm REAL,                      -- 滚动市现率（baostock pcfNcfTTM）
    div_yield   REAL,                      -- 动态股息率（%）: 近12月每股分红/收盘价
    is_st       INTEGER,                   -- 是否 ST（0/1）
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_kline_type_code_date ON kline(ktype, code, date);
-- 说明：用 (ktype, code, date) 覆盖索引，而不是早期的 (ktype, date)：
--   * "按类型算最新日期/去重个股数"（latest_kline_dates / count_stocks）只需扫索引，不回表；
--   * 早期 (ktype,date) 不含 code，COUNT(DISTINCT code) 要回表 260 万次（实测 15 秒）。
-- 注意：不要再建 (code,date) 上的索引 —— 主键 (code,date) 已有隐式索引，
-- 多一个同构索引只会让最大表 kline 的每次写入多维护一棵 B 树。

-- ② 指数成分股（snapshot 为快照日，保留历史以便回溯）
CREATE TABLE IF NOT EXISTS index_constituent (
    index_code  TEXT    NOT NULL,          -- 指数代码 sh.000300
    stock_code  TEXT    NOT NULL,          -- 成分股代码 sh.600000
    snapshot    TEXT    NOT NULL,          -- 快照日期 YYYY-MM-DD
    is_active   INTEGER DEFAULT 1,         -- 1=当前有效, 0=已剔除
    updated_at  TEXT,
    PRIMARY KEY (index_code, stock_code, snapshot)
);
CREATE INDEX IF NOT EXISTS idx_cons_index ON index_constituent(index_code, is_active);

-- ③ 分红数据（算动态股息率的依据）
CREATE TABLE IF NOT EXISTS dividend (
    code        TEXT    NOT NULL,          -- 个股代码
    ex_date     TEXT    NOT NULL,          -- 除权除息日 YYYY-MM-DD
    cash_ps     REAL,                      -- 每股税前现金分红（元）
    stock_ps    REAL,                      -- 每股送股（股）
    PRIMARY KEY (code, ex_date)
);
CREATE INDEX IF NOT EXISTS idx_div_date ON dividend(code, ex_date);

-- ④ 标的元信息
CREATE TABLE IF NOT EXISTS stock_basic (
    code        TEXT    PRIMARY KEY,       -- sh.600000 / sh.000300
    name        TEXT,                      -- 名称（浦发银行 / 沪深300）
    ktype       TEXT,                      -- stock / index
    market      TEXT,                      -- sh / sz
    industry    TEXT,                      -- 所属行业（个股）
    listed_date TEXT,                      -- 上市日期
    updated_at  TEXT
);

-- ⑤ 估值目标与权重（需要算综合评分的指数/个股）
CREATE TABLE IF NOT EXISTS valuation_target (
    code        TEXT    PRIMARY KEY,       -- sh.000300
    name        TEXT    NOT NULL,          -- 沪深300
    ktype       TEXT    NOT NULL,          -- index / stock
    enabled     INTEGER DEFAULT 1,         -- 是否参与每日计算
    w_pe        REAL    DEFAULT 0,         -- 各估值指标权重（合计应为 1.0）
    w_pb        REAL    DEFAULT 0,
    w_ps        REAL    DEFAULT 0,
    w_pcf       REAL    DEFAULT 0,
    w_dividend  REAL    DEFAULT 0,
    sort_order  INTEGER DEFAULT 0,         -- 邮件/图表展示顺序
    remark      TEXT
);

-- ⑥ 全局配置（替代 config.py，支持 JSON 值）
CREATE TABLE IF NOT EXISTS setting (
    key         TEXT    PRIMARY KEY,       -- 如 SMTP_HOST / SIGNAL_BANDS
    value       TEXT,                      -- 值（JSON 类型时存序列化结果）
    val_type    TEXT,                      -- str / int / float / bool / json
    group_name  TEXT,                      -- smtp / runtime / chart / signal ...
    remark      TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_setting_group ON setting(group_name);

-- ⑦ 增量同步状态（断点续传）
CREATE TABLE IF NOT EXISTS sync_state (
    code        TEXT    NOT NULL,          -- 标的代码
    dtype       TEXT    NOT NULL,          -- kline / dividend / constituent
    last_date   TEXT,                      -- 已同步到的最新日期
    row_count   INTEGER DEFAULT 0,
    updated_at  TEXT,
    PRIMARY KEY (code, dtype)
);

-- ⑧ 评分历史（用于展示评分/分位的走势图）
--    每标的每估值日一条；分位已统一为"越高越贵"方向
CREATE TABLE IF NOT EXISTS valuation_score (
    code         TEXT    NOT NULL,         -- 标的代码
    date         TEXT    NOT NULL,         -- 估值日 YYYY-MM-DD
    ktype        TEXT,                     -- index / stock
    score        REAL,                     -- 综合评分（10年口径，0~100 越高越贵）
    score5       REAL,                     -- 综合评分（5年口径，参考）
    pct_pe       REAL,                     -- 各指标分位（10年）
    pct_pb       REAL,
    pct_ps       REAL,
    pct_pcf      REAL,
    pct_dividend REAL,
    pct5_pe      REAL,                     -- 各指标分位（5年，参考口径）
    pct5_pb      REAL,
    pct5_ps      REAL,
    pct5_pcf     REAL,
    pct5_dividend REAL,
    status       TEXT,                     -- 信号状态快照（低估/偏低/正常/偏高/高估）
    action       TEXT,                     -- 动作建议快照
    n_used       INTEGER,                  -- 参与计算的成分股数量
    created_at   TEXT,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_score_date ON valuation_score(date);
-- 注意：不要建 (code,date) 索引 —— 主键 (code,date) 已有隐式唯一索引，
-- 再建一棵只会让每次写 valuation_score（全量分位重建时上百万行）多维护一棵 B 树。

-- ⑨ 交易日历（baostock query_trade_dates 落库，离线判断交易日/算增量区间）
CREATE TABLE IF NOT EXISTS trade_date (
    date        TEXT    PRIMARY KEY,       -- YYYY-MM-DD
    is_open     INTEGER NOT NULL,          -- 1=交易日, 0=非交易日
    source      TEXT,                      -- 来源标记（baostock / manual）
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_open ON trade_date(is_open, date);

-- ⑩ 历史邮件（只存标题+摘要，不存正文，供首页"历史邮件列表"）
CREATE TABLE IF NOT EXISTS mail_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at     TEXT,                       -- 发送时间 YYYY-MM-DD HH:MM:SS
    subject     TEXT,                       -- 邮件标题
    summary     TEXT,                       -- 内容摘要（一行，各标的 名称+状态+评分）
    receivers   TEXT,                       -- 收件人（逗号分隔）
    kind        TEXT,                       -- daily / alert / manual
    ok          INTEGER,                    -- 1=成功 0=失败
    body_key    TEXT                        -- 指向 mail_body.body_key（正文快照）
);
CREATE INDEX IF NOT EXISTS idx_mail_sent ON mail_log(sent_at);

-- ⑬ 已发送邮件的正文快照（首页点击邮件时原样回看）
-- 存的是**自包含 HTML**（图片已 base64 内联），单个文件即可在浏览器里正常显示，
-- 不依赖 cid: 附件。日常/告警邮件按日期存一份（重发共用），手动发送按时间戳各存一份。
CREATE TABLE IF NOT EXISTS mail_body (
    body_key    TEXT PRIMARY KEY,           -- 与 mail_log.body_key 对应
    subject     TEXT,
    html        TEXT,                       -- 自包含正文
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_mail_body_created ON mail_body(created_at);

-- ⑪ 暂存邮件（告警邮件正文：生成后暂存 → 08:30 发送 → 告警重发；保留最近一周）
CREATE TABLE IF NOT EXISTS pending_alert (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    build_date  TEXT UNIQUE,               -- 生成日期 YYYY-MM-DD（一天一封）
    subject     TEXT,
    summary     TEXT,                       -- 摘要（一行）
    html        TEXT,                       -- 完整邮件正文（重发用）
    receivers   TEXT,                       -- 收件人（逗号分隔）
    is_alert    INTEGER,                    -- 1=告警邮件（需重发），0=普通通知
    sent_count  INTEGER DEFAULT 0,          -- 已发送次数（0=未发）
    created_at  TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_date ON pending_alert(build_date);

-- ⑭ 暂存邮件的内联图片（构建时一起存，发送/重发时作为 cid 附件带上）
-- 不存的话定时发送的邮件里 8 张图表全是破图（HTML 里只有 cid: 引用）。
CREATE TABLE IF NOT EXISTS pending_image (
    build_date  TEXT    NOT NULL,           -- 与 pending_alert.build_date 对应
    cid         TEXT    NOT NULL,           -- HTML 里 <img src="cid:xxx"> 的 xxx
    png         BLOB    NOT NULL,           -- PNG 原始字节
    PRIMARY KEY (build_date, cid)
);

-- ⑫ 表结构版本
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT,
    description TEXT
);
"""

# 依赖的标的与默认配置会在 script/init_db.py 中写入初始数据；
# 指数代码 / 名称映射（baostock 格式）
INDEX_CODES = {
    "沪深300": "sh.000300",
    "中证500": "sh.000905",
    "科创50":  "sh.000688",   # 暂不支持（baostock 无数据），成分股需手工维护
}


# =====================================================================
# 结构升级（老库补列 / 清理冗余对象）
# =====================================================================
# DDL 里的 CREATE TABLE IF NOT EXISTS 不会修改**已存在**的表：
# 给已有表新增列必须显式 ALTER TABLE。这里维护"老库需要补什么"。
# ⚠️ 给 schema.DDL 里的已有表加列时，务必同步加到下面。
MIGRATION_COLUMNS = {
    "valuation_score": {
        "pct5_pe": "REAL",           # 3：五指标 5 年分位
        "pct5_pb": "REAL",
        "pct5_ps": "REAL",
        "pct5_pcf": "REAL",
        "pct5_dividend": "REAL",
    },
    "kline": {
        "close_raw": "REAL",          # 6：不复权收盘价
    },
    "mail_log": {
        "body_key": "TEXT",           # 7：指向 mail_body（已发送邮件的正文快照）
    },
}

# 需要删除的冗余对象（老库清理用）
MIGRATION_DROP_INDEXES = (
    "idx_kline_code_date",      # 与 kline 主键 (code,date) 重复
    "idx_score_code_date",      # 与 valuation_score 主键 (code,date) 重复
    "idx_kline_type_date",      # 被覆盖索引 idx_kline_type_code_date 取代
)


def migrate(conn) -> list:
    """把老库升到当前结构（幂等）。返回本次实际执行的变更列表。"""
    done = []
    for table, cols in MIGRATION_COLUMNS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if not exists:
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col in have:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            done.append(f"{table}.{col}")
    for idx in MIGRATION_DROP_INDEXES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                        (idx,)).fetchone():
            conn.execute(f"DROP INDEX {idx}")
            done.append(f"drop index {idx}")
    if done:
        conn.commit()
    return done


def stamp_version(conn) -> tuple:
    """把 schema_version 记为当前版本（幂等）。返回 (原版本, 现版本)。

    服务启动时的 storage.ensure_schema() 也会调用它 —— 这样"只更新代码、
    不跑 init_db"的老库也能把版本号补上，而不是长期停在旧版本号。

    库版本比代码新时抛 RuntimeError：那种情况必须换代码，不能盲目往下写。
    """
    import datetime as _dt
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    cur = int(row[0]) if row and row[0] else 0
    if cur > SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库结构版本（v{cur}）比代码（v{SCHEMA_VERSION}）新："
            f"请先更新代码，或换用对应的数据库。")
    if cur < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(version, applied_at, description) "
            "VALUES(?,?,?)",
            (SCHEMA_VERSION,
             _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             f"由 v{cur} 升级到 v{SCHEMA_VERSION}"))
        conn.commit()
    return cur, SCHEMA_VERSION


def drift(conn) -> list:
    """检查已有库的表结构是否与 DDL 一致。返回差异描述列表（空=一致）。

    做法：用 DDL 建一个内存库，逐表比较列名集合 —— 以后有人改了 DDL 却忘了
    写 MIGRATION_COLUMNS，会在 init_db 时立刻暴露，而不是等到某次写入报错。
    """
    import sqlite3 as _sq
    mem = _sq.connect(":memory:")
    try:
        mem.executescript(DDL)
        out = []
        def cols(c, table):
            return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
        want_tables = {r[0] for r in mem.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        have_tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in sorted(want_tables - have_tables):
            out.append(f"缺表：{t}")
        for t in sorted(want_tables & have_tables):
            missing = set(cols(mem, t)) - set(cols(conn, t))
            if missing:
                out.append(f"{t} 缺列：{sorted(missing)}")
        return out
    finally:
        mem.close()
