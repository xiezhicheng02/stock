# -*- coding: utf-8 -*-
"""baostock 数据抓取会话。

每个方法对应 src/model/ 下的一个 pydantic 对象，返回该对象的 list。
统一走 _call() 做登录态管理、重试、限流、结果解析。

用法
----
    with BaostockSession() as bs:
        klines = bs.kline("sh.600000", "2024-01-01", "2024-01-31")
        astock = bs.daily_kline_astock("2026-02-05")
        etfs   = bs.daily_kline_etf("2026-02-05")
"""

import logging
import socket
import time

import baostock as bs

from src.config import config
from src.model.Kline import Kline
from src.model.StockBasic import StockBasic
from src.model.TradeDate import TradeDate
from src.model.ProfitData import ProfitData
from src.model.OperationData import OperationData
from src.model.GrowthData import GrowthData
from src.model.BalanceData import BalanceData
from src.model.CashFlowData import CashFlowData
from src.model.DupontData import DupontData
from src.model.PerformanceExpress import PerformanceExpress
from src.model.ForecastReport import ForecastReport
from src.model.DepositRate import DepositRate
from src.model.LoanRate import LoanRate
from src.model.RequiredReserveRatio import RequiredReserveRatio
from src.model.MoneySupplyMonth import MoneySupplyMonth
from src.model.MoneySupplyYear import MoneySupplyYear
from src.model.AdjustFactor import AdjustFactor

log = logging.getLogger("fetch")


KLINE_FIELDS = ("date,open,high,low,close,preclose,volume,amount,"
                "adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST")
ADJUST_KLINE = "2"
_NETWORK_ERRORS = (OSError, TimeoutError, RuntimeError, ConnectionError)


class BaostockSession:
    """baostock 查询会话（上下文管理器）。

    所有查询统一走 _call()，带查询间隔、失败重试、连续失败熔断/重连。
    每个业务方法返回 list[对应 model]。
    """

    RECONNECT_AFTER = 5
    ABORT_AFTER = 12

    def __init__(self, cfg=None):
        """会话初始化。cfg 为空则用 config.BAOSTOCK。"""
        self.cfg = cfg or config.BAOSTOCK
        self._logged = False
        self._fail_streak = 0

    def login(self):
        """登录 baostock（幂等）。"""
        if self._logged:
            return
        timeout = int(self.cfg.get("timeout", 60) or 60)
        socket.setdefaulttimeout(timeout)
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        self._logged = True
        self._fail_streak = 0
        log.info("baostock 登录成功（socket 超时 %ds）", timeout)

    def reconnect(self):
        """重新登录（会话失效时用）。"""
        log.warning("baostock 会话疑似失效，尝试重新登录…")
        self.logout()
        self.login()

    def logout(self):
        """登出 baostock（幂等）。"""
        if self._logged:
            try:
                bs.logout()
            finally:
                self._logged = False

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.logout()
        return False

    @staticmethod
    def _rows(rs, what=""):
        """把 baostock ResultData 转成 list[dict]，失败抛异常。"""
        if rs.error_code != "0":
            raise RuntimeError(f"{what} 查询失败: {rs.error_msg}")
        fields = rs.fields
        out = []
        while rs.next():
            out.append(dict(zip(fields, rs.get_row_data())))
        return out

    def _call(self, fn, *args, what="", **kwargs):
        """带重试、限流、失败熔断的查询包装。

        - 单次查询最多重试 retry 次（只重试网络类异常）；
        - 连续失败达 RECONNECT_AFTER 次 → 重新登录；
        - 连续失败达 ABORT_AFTER 次 → 熔断抛错。
        """
        retry = max(1, int(self.cfg.get("retry", 3)))
        sleep = float(self.cfg.get("sleep", 0.2))
        last_err = None
        for attempt in range(1, retry + 1):
            try:
                if sleep:
                    time.sleep(sleep)
                out = self._rows(fn(*args, **kwargs), what=what)
                self._fail_streak = 0
                return out
            except _NETWORK_ERRORS as e:
                last_err = e
                self._fail_streak += 1
                log.warning("%s 第 %d/%d 次失败（连续 %d 次）: %s",
                            what or "查询", attempt, retry, self._fail_streak, e)
                if self._fail_streak >= self.ABORT_AFTER:
                    raise RuntimeError(
                        f"{what or '查询'} 连续失败 {self._fail_streak} 次，"
                        f"判定数据源不可用，终止本次同步") from e
                if self._fail_streak == self.RECONNECT_AFTER:
                    try:
                        self.reconnect()
                    except Exception as re_:  # noqa: BLE001
                        log.error("重新登录失败：%s", re_)
                if attempt < retry:
                    time.sleep(sleep * attempt * 3)
        raise RuntimeError(f"{what or '查询'} 重试 {retry} 次仍失败: {last_err}")

    # ---------- K 线 ----------
    def kline(self, code, start, end):
        """单只证券日 K 线（前复权，含估值指标）。Returns: list[Kline]."""
        rows = self._call(bs.query_history_k_data_plus, code, KLINE_FIELDS,
                          start_date=start, end_date=end, frequency="d",
                          adjustflag=ADJUST_KLINE, what=f"K线 {code}")
        return [Kline(**r) for r in rows]

    def daily_kline_astock(self, date):
        """指定日期全部 A 股日 K 线（不复权）。Returns: list[Kline]。

        一次拉全市场某日快照，适合批量补数据；字段与 Kline model 完全一致。
        """
        rows = self._call(bs.query_daily_history_k_AStock, date=date,
                          what=f"全市场日K {date}")
        return [Kline(**r) for r in rows]

    def daily_kline_etf(self, date):
        """指定日期全部 ETF 日 K 线（不复权）。Returns: list[Kline]。

        字段与 Kline model 完全一致；ETF 的 peTTM/pbMRQ 等估值列为空。
        """
        rows = self._call(bs.query_daily_history_k_ETF, date=date,
                          what=f"ETF日K {date}")
        return [Kline(**r) for r in rows]

    def daily_adjust_factor(self, date):
        """指定日期全部证券复权因子。Returns: list[AdjustFactor]。

        与 daily_kline_astock 同日拉取，用于复权口径换算。
        """
        rows = self._call(bs.query_daily_adjust_factor, date=date,
                          what=f"复权因子 {date}")
        for r in rows:
            r["date"] = date
        return [AdjustFactor(**r) for r in rows]

    # ---------- 交易日历 / 证券元信息 ----------
    def trade_dates(self, start, end):
        """交易日历。Returns: list[TradeDate]."""
        rows = self._call(bs.query_trade_dates, start_date=start, end_date=end,
                          what="交易日历")
        return [TradeDate(**r) for r in rows]

    def stock_basics(self, code=None):
        """证券基本资料。code=None 批量查全市场。Returns: list[StockBasic]（不含行业）。"""
        rows = (self._call(bs.query_stock_basic, code=code, what=f"证券基本资料 {code}")
                if code else self._call(bs.query_stock_basic, what="证券基本资料"))
        return [StockBasic(**r) for r in rows]

    def stock_industries(self, code=None):
        """股票行业分类。Returns: list[StockBasic]（含 industry / industryClassification）。

        注意：行业字段来自本接口，与 stock_basics() 是两个接口，调用方自行合并。
        """
        rows = (self._call(bs.query_stock_industry, code=code, what=f"行业分类 {code}")
                if code else self._call(bs.query_stock_industry, what="行业分类"))
        return [StockBasic(**r) for r in rows]

    # ---------- 季频财务 ----------
    def profit_data(self, code, year, quarter):
        """季频盈利能力。Returns: list[ProfitData]."""
        rows = self._call(bs.query_profit_data, code=code, year=year, quarter=quarter,
                         what=f"盈利能力 {code} {year}Q{quarter}")
        return [ProfitData(**r) for r in rows]

    def operation_data(self, code, year, quarter):
        """季频营运能力。Returns: list[OperationData]."""
        rows = self._call(bs.query_operation_data, code=code, year=year, quarter=quarter,
                         what=f"营运能力 {code} {year}Q{quarter}")
        return [OperationData(**r) for r in rows]

    def growth_data(self, code, year, quarter):
        """季频成长能力。Returns: list[GrowthData]."""
        rows = self._call(bs.query_growth_data, code=code, year=year, quarter=quarter,
                         what=f"成长能力 {code} {year}Q{quarter}")
        return [GrowthData(**r) for r in rows]

    def balance_data(self, code, year, quarter):
        """季频偿债能力。Returns: list[BalanceData]."""
        rows = self._call(bs.query_balance_data, code=code, year=year, quarter=quarter,
                         what=f"偿债能力 {code} {year}Q{quarter}")
        return [BalanceData(**r) for r in rows]

    def cash_flow_data(self, code, year, quarter):
        """季频现金流量。Returns: list[CashFlowData]."""
        rows = self._call(bs.query_cash_flow_data, code=code, year=year, quarter=quarter,
                         what=f"现金流量 {code} {year}Q{quarter}")
        return [CashFlowData(**r) for r in rows]

    def dupont_data(self, code, year, quarter):
        """季频杜邦指数。Returns: list[DupontData]."""
        rows = self._call(bs.query_dupont_data, code=code, year=year, quarter=quarter,
                         what=f"杜邦指数 {code} {year}Q{quarter}")
        return [DupontData(**r) for r in rows]

    # ---------- 公司报告 ----------
    def performance_express(self, code, year, quarter):
        """季频业绩快报。Returns: list[PerformanceExpress]."""
        rows = self._call(bs.query_performance_express, code=code, year=year, quarter=quarter,
                         what=f"业绩快报 {code} {year}Q{quarter}")
        return [PerformanceExpress(**r) for r in rows]

    def forecast_report(self, code, year, quarter):
        """季频业绩预告。Returns: list[ForecastReport]."""
        rows = self._call(bs.query_forecast_report, code=code, year=year, quarter=quarter,
                         what=f"业绩预告 {code} {year}Q{quarter}")
        return [ForecastReport(**r) for r in rows]

    # ---------- 宏观经济 ----------
    def deposit_rate(self, start_date, end_date):
        """存款利率。Returns: list[DepositRate]."""
        rows = self._call(bs.query_deposit_rate_data,
                          start_date=start_date, end_date=end_date,
                          what=f"存款利率 {start_date}~{end_date}")
        return [DepositRate(**r) for r in rows]

    def loan_rate(self, start_date, end_date):
        """贷款利率。Returns: list[LoanRate]."""
        rows = self._call(bs.query_loan_rate_data,
                          start_date=start_date, end_date=end_date,
                          what=f"贷款利率 {start_date}~{end_date}")
        return [LoanRate(**r) for r in rows]

    def reserve_ratio(self, start_date, end_date):
        """存款准备金率。Returns: list[RequiredReserveRatio]."""
        rows = self._call(bs.query_required_reserve_ratio_data,
                          start_date=start_date, end_date=end_date,
                          what=f"准备金率 {start_date}~{end_date}")
        return [RequiredReserveRatio(**r) for r in rows]

    def money_supply_month(self, start_date, end_date):
        """货币供应量（月度）。Returns: list[MoneySupplyMonth]."""
        rows = self._call(bs.query_money_supply_data_month,
                          start_date=start_date, end_date=end_date,
                          what=f"货币供应量月 {start_date}~{end_date}")
        return [MoneySupplyMonth(**r) for r in rows]

    def money_supply_year(self, start_date, end_date):
        """货币供应量余额（年度）。Returns: list[MoneySupplyYear]."""
        rows = self._call(bs.query_money_supply_data_year,
                          start_date=start_date, end_date=end_date,
                          what=f"货币供应量年 {start_date}~{end_date}")
        return [MoneySupplyYear(**r) for r in rows]