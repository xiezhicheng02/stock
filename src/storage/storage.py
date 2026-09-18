# -*- coding: utf-8 -*-
"""SQLite 数据访问层（封装）。

对外只暴露 ``Storage`` 类：
    Storage(db_path=None)        打开连接
    st.ensure_schema()           建表 + 记录版本（幂等）
    st.check_schema()            检查 schema.DDL 中的表是否都已建，缺则补建
    st.save(items)               保存单个或 list[Model]，自动按类型分派表
    st.load(Model, code, start, end)  通用查询，返回 list[Model]
    st.close()                   关闭连接

内部用一张「model -> 表名/时间列」注册表封装所有表细节；新增 model 时
只需在 _REGISTRY 里加一行，调用方代码不用改。
"""

import logging
import re as _re
import sqlite3

from src.config import config
from src.storage import schema

log = logging.getLogger("storage")


# =====================================================================
# 内部工具
# =====================================================================
def _to_snake(name: str) -> str:
    """驼峰 -> snake_case：pctChg->pct_chg、peTTM->pe_ttm。"""
    s1 = _re.sub(r'(.)([A-Z][a-z]+)', r'\1_\2', name)
    return _re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', s1).lower()


def _cols_of(model_cls) -> list:
    """model 字段名 -> 表列名列表。"""
    return [_to_snake(f) for f in model_cls.model_fields]


def _row_to_model(row, model_cls):
    """sqlite.Row -> model 实例。"""
    back = {_to_snake(f): f for f in model_cls.model_fields}
    kwargs = {back[c]: row[c] for c in row.keys() if c in back}
    return model_cls(**kwargs)


# =====================================================================
# model -> 表注册表（新增 model 在这里加一行即可）
# =====================================================================
# table: 表名；time_col: 查询用时间列（无时间过滤则 None）；has_code: 表是否有 code 列
_REGISTRY = {
    "Kline": {"table": "kline", "time_col": "date", "has_code": True},
    "AdjustFactor": {"table": "adjust_factor", "time_col": "date", "has_code": True},
    "StockBasic": {"table": "stock_basic", "time_col": None, "has_code": True},
    "TradeDate": {"table": "trade_date", "time_col": "calendar_date", "has_code": False},
    "ProfitData": {"table": "profit_data", "time_col": "stat_date", "has_code": True},
    "OperationData": {"table": "operation_data", "time_col": "stat_date", "has_code": True},
    "GrowthData": {"table": "growth_data", "time_col": "stat_date", "has_code": True},
    "BalanceData": {"table": "balance_data", "time_col": "stat_date", "has_code": True},
    "CashFlowData": {"table": "cash_flow_data", "time_col": "stat_date", "has_code": True},
    "DupontData": {"table": "dupont_data", "time_col": "stat_date", "has_code": True},
    "PerformanceExpress": {"table": "performance_express", "time_col": "performance_exp_pub_date", "has_code": True},
    "ForecastReport": {"table": "forecast_report", "time_col": "profit_forcast_exp_pub_date", "has_code": True},
    "DepositRate": {"table": "deposit_rate", "time_col": "pub_date", "has_code": False},
    "LoanRate": {"table": "loan_rate", "time_col": "pub_date", "has_code": False},
    "RequiredReserveRatio": {"table": "reserve_ratio", "time_col": "pub_date", "has_code": False},
    "MoneySupplyMonth": {"table": "money_supply_month", "time_col": "stat_year", "has_code": False},
    "MoneySupplyYear": {"table": "money_supply_year", "time_col": "stat_year", "has_code": False},
}


def _reg(model_cls) -> dict:
    """按 model 类查注册表；未注册抛错。"""
    name = model_cls.__name__
    if name not in _REGISTRY:
        raise KeyError(f"model {name} 未在 storage._REGISTRY 注册")
    return _REGISTRY[name]


# =====================================================================
# Storage
# =====================================================================
class Storage:
    """SQLite 存储：对 src/model 的统一读写封装。

    用法：
        st = Storage()
        st.ensure_schema()
        st.save([Kline(...), Kline(...)])
        rows = st.load(Kline, code="sh.600000", start="2024-01-01")
    """

    def __init__(self, db_path: str | None = None):
        """打开数据库连接（WAL 模式）。

        Args:
            db_path: 数据库文件路径；为空则用 ``config.DB_PATH``。
        """
        path = db_path or config.DB_PATH
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        log.debug("打开数据库 %s", path)

    # ---------- 建表 / 表检查 ----------
    def ensure_schema(self) -> None:
        """按 schema.DDL 建表（幂等）并记录版本号。"""
        self.conn.executescript(schema.DDL)
        schema.migrate(self.conn)
        old, new = schema.stamp_version(self.conn)
        self.conn.commit()
        log.info("schema 就绪：v%s -> v%s", old, new)

    def check_schema(self) -> list:
        """检查 schema.DDL 中的建表语句是否都已执行；缺哪些补建哪些。

        Returns:
            本次补建的表名列表（空 = 全部已存在）。
        """
        want = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql NOT NULL")}
        # 从 DDL 里解析应有的表名（CREATE TABLE IF NOT EXISTS xxx）
        import re as _re2
        need = set(_re2.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema.DDL))
        missing = sorted(need - want)
        if missing:
            log.warning("缺表 %s，执行补建", missing)
            self.conn.executescript(schema.DDL)
            self.conn.commit()
        else:
            log.debug("schema 检查：%d 张表全部存在", len(need))
        return missing

    # ---------- 写 ----------
    def save(self, items) -> int:
        """保存 model 对象（单个或 list）。自动按类型分派到对应表。

        Args:
            items: 单个 pydantic model 实例或 list[model]。

        Returns:
            写入行数。
        """
        # 统一成 list
        if not isinstance(items, (list, tuple)):
            items = [items]
        if not items:
            return 0
        # 按 model 类型分组
        groups: dict[type, list] = {}
        for it in items:
            groups.setdefault(type(it), []).append(it)
        total = 0
        for model_cls, batch in groups.items():
            meta = _reg(model_cls)
            cols = _cols_of(model_cls)
            ph = ",".join("?" * len(cols))
            sql = f"INSERT OR REPLACE INTO {meta['table']}({','.join(cols)}) VALUES({ph})"
            rows = []
            for it in batch:
                d = it.model_dump()
                back = {_to_snake(f): f for f in it.model_fields}
                rows.append(tuple(d.get(back.get(c, c)) for c in cols))
            self.conn.executemany(sql, rows)
            total += len(rows)
            log.info("保存 %s: %d 行", meta["table"], len(rows))
        self.conn.commit()
        return total

    # ---------- 读 ----------
    def load(self, model_cls, code=None, start=None, end=None) -> list:
        """通用查询。

        Args:
            model_cls: model 类（如 ``Kline``、``ProfitData``）。
            code:      证券代码；None 表示不限。仅对有 code 的表生效。
            start:     起始日期/年份（闭区间）；None 不限。
            end:       结束日期/年份（闭区间）；None 不限。

        Returns:
            list[model_cls]。
        """
        meta = _reg(model_cls)
        where, params = [], []
        if meta["has_code"] and code:
            where.append("code = ?")
            params.append(code)
        tc = meta["time_col"]
        if tc:
            if start:
                where.append(f"{tc} >= ?")
                params.append(start)
            if end:
                where.append(f"{tc} <= ?")
                params.append(end)
        sql = f"SELECT * FROM {meta['table']}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if tc:
            sql += f" ORDER BY {tc}"
        rows = self.conn.execute(sql, params).fetchall()
        log.debug("查询 %s: %d 行 (code=%s, %s~%s)", meta["table"], len(rows), code, start, end)
        return [_row_to_model(r, model_cls) for r in rows]

    # ---------- 前复权调整 ----------
    def apply_adjust_factor(self, code: str, factor: float, before_date: str) -> int:
        """把某 code 在 before_date 之前的历史 K 线价格乘以复权因子。

        daily_task 拉到的全市场快照是不复权价；当某 code 除权除息时，
        baostock 给出 foreAdjustFactor，需要把除权日之前的历史价统一乘上，
        才能和除权后的新价格口径连续（即前复权）。

        Args:
            code: 证券代码。
            factor: 向前复权因子（foreAdjustFactor，通常 < 1）。
            before_date: 除权除息日 YYYY-MM-DD；只调整这一天之前的行。

        Returns:
            被调整的行数。
        """
        if factor is None or factor == 1:
            return 0
        sql = (
            "UPDATE kline SET "
            "open = open * ?, high = high * ?, low = low * ?, "
            "close = close * ?, preclose = preclose * ? "
            "WHERE code = ? AND date < ? AND open IS NOT NULL"
        )
        cur = self.conn.execute(sql, (factor, factor, factor, factor, factor, code, before_date))
        self.conn.commit()
        n = cur.rowcount
        if n:
            log.info("前复权调整 %s: factor=%.6f, < %s, %d 行", code, factor, before_date, n)
        return n

    # ---------- 连接管理 ----------
    def close(self) -> None:
        """关闭数据库连接。"""
        self.conn.close()
        log.debug("数据库连接已关闭")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
