# -*- coding: utf-8 -*-
"""SQLite 表结构定义：与 src/model 下的 pydantic 对象一一对应。

设计约定
--------
* 每张表对应一个 model，字段统一 snake_case；
  model 驼峰名经 ``storage._to_snake()`` 自动转换；
* 有 code 的表以 (code, stat_date / pub_date) 为主键；
* 无 code 的宏观表以 pub_date / stat_year 为主键；
* 新表用 ``CREATE TABLE IF NOT EXISTS``，建表幂等。
"""

SCHEMA_VERSION = 1


DDL = """
-- ① K 线 kline（Kline）
CREATE TABLE IF NOT EXISTS kline (
    date          TEXT NOT NULL,
    code          TEXT NOT NULL,
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL,
    preclose      REAL,
    volume        REAL,
    amount        REAL,
    adjustflag    TEXT,
    tradestatus   INTEGER,
    turn          REAL,
    pct_chg       REAL,
    pe_ttm        REAL,
    pb_mrq        REAL,
    ps_ttm        REAL,
    pcf_ncf_ttm   REAL,
    is_st         INTEGER,
    ktype         TEXT,    -- stock / etf / index
    PRIMARY KEY (code, date)
);

-- ② 证券基本资料 stock_basic（StockBasic）
CREATE TABLE IF NOT EXISTS stock_basic (
    code                     TEXT PRIMARY KEY,
    code_name                TEXT,
    ipo_date                 TEXT,
    out_date                 TEXT,
    type                     TEXT,
    status                   TEXT,
    industry                 TEXT,   -- 来自 query_stock_industry，另一个接口
    industry_classification  TEXT,   -- 来自 query_stock_industry，另一个接口
    kline_full_sync_date     TEXT    -- 本地记录：完整 kline 已补到哪天
);

-- ③ 交易日历 trade_date（TradeDate）
CREATE TABLE IF NOT EXISTS trade_date (
    calendar_date   TEXT PRIMARY KEY,
    is_trading_day  INTEGER
);

-- ④ 季频盈利能力 profit_data（ProfitData）
CREATE TABLE IF NOT EXISTS profit_data (
    code          TEXT NOT NULL,
    pub_date      TEXT,
    stat_date     TEXT NOT NULL,
    roe_avg       REAL,
    np_margin     REAL,
    gp_margin     REAL,
    net_profit    REAL,
    eps_ttm       REAL,
    mb_revenue    REAL,
    total_share   REAL,
    liqa_share    REAL,
    PRIMARY KEY (code, stat_date)
);

-- ⑤ 季频营运能力 operation_data（OperationData）
CREATE TABLE IF NOT EXISTS operation_data (
    code            TEXT NOT NULL,
    pub_date        TEXT,
    stat_date       TEXT NOT NULL,
    nr_turn_ratio   REAL,
    nr_turn_days    REAL,
    inv_turn_ratio  REAL,
    inv_turn_days   REAL,
    ca_turn_ratio   REAL,
    asset_turn_ratio REAL,
    PRIMARY KEY (code, stat_date)
);

-- ⑥ 季频成长能力 growth_data（GrowthData）
CREATE TABLE IF NOT EXISTS growth_data (
    code           TEXT NOT NULL,
    pub_date       TEXT,
    stat_date      TEXT NOT NULL,
    yoy_equity     REAL,
    yoy_asset      REAL,
    yoyni          REAL,
    yoyeps_basic   REAL,
    yoypni         REAL,
    PRIMARY KEY (code, stat_date)
);

-- ⑦ 季频偿债能力 balance_data（BalanceData）
CREATE TABLE IF NOT EXISTS balance_data (
    code              TEXT NOT NULL,
    pub_date          TEXT,
    stat_date         TEXT NOT NULL,
    current_ratio     REAL,
    quick_ratio       REAL,
    cash_ratio        REAL,
    yoy_liability    REAL,
    liability_to_asset REAL,
    asset_to_equity   REAL,
    PRIMARY KEY (code, stat_date)
);

-- ⑧ 季频现金流量 cash_flow_data（CashFlowData）
CREATE TABLE IF NOT EXISTS cash_flow_data (
    code                  TEXT NOT NULL,
    pub_date              TEXT,
    stat_date             TEXT NOT NULL,
    ca_to_asset           REAL,
    nca_to_asset          REAL,
    tangible_asset_to_asset REAL,
    ebit_to_interest      REAL,
    cfo_to_or             REAL,
    cfo_to_np             REAL,
    cfo_to_gr             REAL,
    PRIMARY KEY (code, stat_date)
);

-- ⑨ 季频杜邦指数 dupont_data（DupontData）
CREATE TABLE IF NOT EXISTS dupont_data (
    code                  TEXT NOT NULL,
    pub_date              TEXT,
    stat_date             TEXT NOT NULL,
    dupont_roe            REAL,
    dupont_asset_sto_equity REAL,
    dupont_asset_turn     REAL,
    dupont_pnitoni        REAL,
    dupont_nitogr         REAL,
    dupont_tax_burden     REAL,
    dupont_intburden      REAL,
    dupont_ebittogr       REAL,
    PRIMARY KEY (code, stat_date)
);

-- ⑩ 季频业绩快报 performance_express（PerformanceExpress）
CREATE TABLE IF NOT EXISTS performance_express (
    code                              TEXT NOT NULL,
    performance_exp_pub_date          TEXT NOT NULL,
    performance_exp_stat_date         TEXT,
    performance_exp_update_date       TEXT,
    performance_express_total_asset   REAL,
    performance_express_net_asset     REAL,
    performance_express_eps_chg_pct   REAL,
    performance_express_roe_wa        REAL,
    performance_express_eps_diluted   REAL,
    performance_express_gryoy         REAL,
    performance_express_opoyoy        REAL,
    PRIMARY KEY (code, performance_exp_pub_date)
);

-- ⑪ 季频业绩预告 forecast_report（ForecastReport）
CREATE TABLE IF NOT EXISTS forecast_report (
    code                          TEXT NOT NULL,
    profit_forcast_exp_pub_date   TEXT NOT NULL,
    profit_forcast_exp_stat_date  TEXT,
    profit_forcast_type           TEXT,
    profit_forcast_abstract       TEXT,
    profit_forcast_chg_pct_up     REAL,
    profit_forcast_chg_pct_dwn    REAL,
    PRIMARY KEY (code, profit_forcast_exp_pub_date)
);

-- ⑫ 存款利率 deposit_rate（DepositRate）
CREATE TABLE IF NOT EXISTS deposit_rate (
    pub_date                           TEXT PRIMARY KEY,
    demand_deposit_rate                REAL,
    fixed_deposit_rate3_month          REAL,
    fixed_deposit_rate6_month          REAL,
    fixed_deposit_rate1_year           REAL,
    fixed_deposit_rate2_year           REAL,
    fixed_deposit_rate3_year           REAL,
    fixed_deposit_rate5_year           REAL,
    installment_fixed_deposit_rate1_year REAL,
    installment_fixed_deposit_rate3_year REAL,
    installment_fixed_deposit_rate5_year REAL
);

-- ⑬ 贷款利率 loan_rate（LoanRate）
CREATE TABLE IF NOT EXISTS loan_rate (
    pub_date                  TEXT PRIMARY KEY,
    loan_rate6_month          REAL,
    loan_rate6_month_to1_year REAL,
    loan_rate1_year_to3_year  REAL,
    loan_rate3_year_to5_year  REAL,
    loan_rate_above5_year     REAL,
    mortgate_rate_below5_year REAL,
    mortgate_rate_above5_year REAL
);

-- ⑭ 存款准备金率 reserve_ratio（RequiredReserveRatio）
CREATE TABLE IF NOT EXISTS reserve_ratio (
    pub_date                       TEXT PRIMARY KEY,
    effective_date                 TEXT,
    big_institutions_ratio_pre     REAL,
    big_institutions_ratio_after   REAL,
    medium_institutions_ratio_pre  REAL,
    medium_institutions_ratio_after REAL
);

-- ⑮ 货币供应量（月）money_supply_month（MoneySupplyMonth）
CREATE TABLE IF NOT EXISTS money_supply_month (
    stat_year           TEXT NOT NULL,
    stat_month          TEXT NOT NULL,
    m0_month            REAL,
    m0_yoy              REAL,
    m0_chain_relative   REAL,
    m1_month            REAL,
    m1_yoy              REAL,
    m1_chain_relative   REAL,
    m2_month            REAL,
    m2_yoy              REAL,
    m2_chain_relative   REAL,
    PRIMARY KEY (stat_year, stat_month)
);

-- ⑯ 货币供应量（年）money_supply_year（MoneySupplyYear）
CREATE TABLE IF NOT EXISTS money_supply_year (
    stat_year     TEXT PRIMARY KEY,
    m0_year       REAL,
    m0_year_yoy   REAL,
    m1_year       REAL,
    m1_year_yoy   REAL,
    m2_year       REAL,
    m2_year_yoy   REAL
);

-- ⑰ 日频复权因子 adjust_factor（AdjustFactor）
CREATE TABLE IF NOT EXISTS adjust_factor (
    date               TEXT NOT NULL,
    code               TEXT NOT NULL,
    divid_operate_date TEXT,
    fore_adjust_factor REAL,
    back_adjust_factor REAL,
    adjust_factor      REAL,
    PRIMARY KEY (date, code)
);

-- ⑱ 表结构版本
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT,
    description TEXT
);
"""


def migrate(conn) -> list:
    """老库升级（当前无迁移项）。返回实际变更列表。"""
    return []


def stamp_version(conn) -> tuple:
    """把 schema_version 记为当前版本。返回 (旧版本, 现版本)。"""
    import datetime as _dt
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    cur = int(row[0]) if row and row[0] else 0
    if cur < SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(version, applied_at, description) "
            "VALUES(?,?,?)",
            (SCHEMA_VERSION,
             _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             f"v{SCHEMA_VERSION}"))
        conn.commit()
    return cur, SCHEMA_VERSION
