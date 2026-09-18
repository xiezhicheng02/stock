# -*- coding: utf-8 -*-
"""周频定时任务：拉取季频财务、宏观经济、交易日历等"非日频"数据。

覆盖的 model
------------
* 宏观：DepositRate / LoanRate / RequiredReserveRatio / MoneySupplyMonth / MoneySupplyYear
* 交易日历：TradeDate
* 季频财务：ProfitData / OperationData / GrowthData / BalanceData /
            CashFlowData / DupontData
* 季频报告：PerformanceExpress / ForecastReport

与 daily_task.py 的分工
-----------------------
* 日频：全市场日 K、复权因子、证券基本信息、行业分类
* 周频：全市场季频财务 + 宏观（季频接口必须按 code 遍历，慢，放周频）

用法
----
    python -m src.schedule.week_task
"""

import logging
from datetime import datetime, timedelta

from src.fetch_data.data_fetcher import BaostockSession
from src.storage.storage import Storage
from src.model.StockBasic import StockBasic

log = logging.getLogger("week_task")


class WeekTask:
    """周频数据同步任务。"""

    # 季频往前回溯几个季度（含本季）
    QUARTERS_BACK = 4
    # 宏观/交易日历回溯年限
    MACRO_YEARS = 2

    def __init__(self):
        self.bs = BaostockSession()
        self.st = Storage()

    def run(self) -> dict:
        """执行一周的同步。返回各步骤行数统计。"""
        log.info("===== 周频任务开始 %s =====", datetime.now().strftime("%Y-%m-%d"))
        self.bs.login()
        try:
            self.st.ensure_schema()
            macro = self._sync_macro()
            trade = self._sync_trade_dates()
            quarterly = self._sync_quarterly_financials()
            result = {"macro": macro, "trade_dates": trade, "quarterly": quarterly}
            log.info("===== 周频任务完成 %s =====", result)
            return result
        finally:
            self.bs.logout()
            self.st.close()

    # ---------- 宏观经济 ----------
    def _sync_macro(self) -> dict:
        """按近 MACRO_YEARS 年区间拉取 5 个宏观接口。"""
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=365 * self.MACRO_YEARS)).strftime("%Y-%m-%d")
        log.info("[1/3] 宏观经济区间 %s ~ %s", start, end)

        counts = {}
        for name, fn in [
            ("deposit_rate", self.bs.deposit_rate),
            ("loan_rate", self.bs.loan_rate),
            ("reserve_ratio", self.bs.reserve_ratio),
            ("money_supply_month", self.bs.money_supply_month),
            ("money_supply_year", self.bs.money_supply_year),
        ]:
            try:
                rows = fn(start, end)
                self.st.save(rows)
                counts[name] = len(rows)
                log.info("  %s: %d 行", name, len(rows))
            except Exception as e:  # noqa: BLE001
                log.error("  %s 失败: %s", name, e)
                counts[name] = 0
        return counts

    # ---------- 交易日历 ----------
    def _sync_trade_dates(self) -> int:
        """拉近一年交易日历。"""
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        rows = self.bs.trade_dates(start, end)
        self.st.save(rows)
        log.info("[2/3] 交易日历 %s ~ %s：%d 行", start, end, len(rows))
        return len(rows)

    # ---------- 季频财务 ----------
    def _quarter_list(self) -> list[tuple[int, int]]:
        """往前推 QUARTERS_BACK 个季度，返回 [(year, quarter), ...]。"""
        now = datetime.now()
        y, q = now.year, (now.month - 1) // 3 + 1
        out = []
        for _ in range(self.QUARTERS_BACK):
            out.append((y, q))
            q -= 1
            if q == 0:
                y -= 1
                q = 4
        return out

    def _sync_quarterly_financials(self) -> dict:
        """遍历 stock_basic 所有 code，拉近 N 个季度的 8 个季频接口。"""
        basics = self.st.load(StockBasic)
        codes = [b.code for b in basics]
        quarters = self._quarter_list()
        log.info("[3/3] 季频财务：%d 只证券 × %d 个季度", len(codes), len(quarters))

        api_names = [
            ("profit_data",        self.bs.profit_data),
            ("operation_data",     self.bs.operation_data),
            ("growth_data",        self.bs.growth_data),
            ("balance_data",       self.bs.balance_data),
            ("cash_flow_data",     self.bs.cash_flow_data),
            ("dupont_data",        self.bs.dupont_data),
            ("performance_express", self.bs.performance_express),
            ("forecast_report",    self.bs.forecast_report),
        ]
        total = {n: 0 for n, _ in api_names}

        for i, code in enumerate(codes, 1):
            for year, quarter in quarters:
                for name, fn in api_names:
                    try:
                        rows = fn(code, year, quarter)
                        if rows:
                            self.st.save(rows)
                            total[name] += len(rows)
                    except Exception as e:  # noqa: BLE001
                        log.debug("  %s %s %dQ%d 失败: %s", name, code, year, quarter, e)
            if i % 100 == 0:
                log.info("  进度 %d/%d", i, len(codes))

        log.info("  季频合计: %s", total)
        return total


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    WeekTask().run()


if __name__ == "__main__":
    main()
