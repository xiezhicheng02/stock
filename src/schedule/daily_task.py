# -*- coding: utf-8 -*-
"""日频定时任务：拉全市场日 K + 复权因子，补证券基本信息与行业分类。

流程
----
1. 用 ``query_daily_history_k_AStock`` + ``query_daily_adjust_factor``
   拉取当日全市场 K 线与复权因子，落库；
2. 对比当日 K 线里的 code 与 stock_basic 表，缺哪些就补拉证券基本信息；
3. 检查 stock_basic 里 industry / industryClassification 为空的记录，
   用 ``query_stock_industry`` 补行业分类（合并到已有记录，不覆盖其他字段）。

用法
----
    python -m src.schedule.daily_task                  # 当天
    python -m src.schedule.daily_task 2026-02-05       # 指定日
"""

import logging
import sys
from datetime import datetime

from src.fetch_data.data_fetcher import BaostockSession
from src.model.Kline import Kline
from src.model.StockBasic import StockBasic
from src.storage.storage import Storage
from src.config import config

log = logging.getLogger("daily_task")


class DailyTask:
    """日频数据同步任务。"""

    def __init__(self):
        self.bs = BaostockSession()
        self.st = Storage()

    def run(self, date: str | None = None) -> dict:
        """执行一天的同步。

        Args:
            date: 交易日 YYYY-MM-DD；None = 今天。

        Returns:
            各步骤行数统计。
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        log.info("===== 日频任务开始 %s =====", date)
        self.bs.login()
        try:
            self.st.ensure_schema()
            self._ensure_index_basics()
            kline_n = self._fetch_daily_kline(date)
            factor_n = self._fetch_daily_adjust_factor(date)
            basic_n = self._sync_missing_stock_basics(date)
            industry_n = self._sync_missing_industries()
            result = {
                "date": date,
                "kline": kline_n,
                "adjust_factor": factor_n,
                "stock_basic_new": basic_n,
                "industry_filled": industry_n,
            }
            log.info("===== 日频任务完成 %s =====", result)
            return result
        finally:
            self.bs.logout()
            self.st.close()

    # ---------- 步骤 1：K 线 + 复权因子 ----------
    def _fetch_daily_kline(self, date: str) -> int:
        """拉当日全市场 A 股 + ETF + 三个指数日 K 并落库。返回总行数。"""
        astock = self.bs.daily_kline_astock(date)
        etf = self.bs.daily_kline_etf(date)
        for k in astock:
            k.ktype = "stock"
        for k in etf:
            k.ktype = "etf"
        self.st.save(astock)
        self.st.save(etf)

        # 三个指数（不复权，估值字段为空）
        idx_rows = []
        for code in config.INDEX_CODES:
            try:
                rows = self.bs.kline(code, date, date)
                for k in rows:
                    k.ktype = "index"
                idx_rows.extend(rows)
            except Exception as e:  # noqa: BLE001
                log.error("  指数 %s 拉取失败: %s", code, e)
        self.st.save(idx_rows)

        total = len(astock) + len(etf) + len(idx_rows)
        log.info("[1/4] 日K %s：A股 %d + ETF %d + 指数 %d = %d 行",
                 date, len(astock), len(etf), len(idx_rows), total)
        return total

    def _ensure_index_basics(self) -> None:
        """首次启动时把三个指数写入 stock_basic（幂等）。"""
        existing = {b.code for b in self.st.load(StockBasic)}
        to_save = []
        for code, name in config.INDEX_CODES.items():
            if code in existing:
                continue
            to_save.append(StockBasic(
                code=code, code_name=name, type=2, status=1))
        if to_save:
            self.st.save(to_save)
            log.info("首次启动：插入 %d 条指数基本信息", len(to_save))

    def _fetch_daily_adjust_factor(self, date: str) -> int:
        """拉当日全市场复权因子，落库并把历史价转成前复权。返回行数。

        当日快照接口返回的是不复权价；对当日除权的 code，用 foreAdjustFactor
        把除权日之前的历史 K 线价格统一乘上，使口径连续（前复权）。
        """
        rows = self.bs.daily_adjust_factor(date)
        self.st.save(rows)
        adjusted = 0
        for r in rows:
            if not r.foreAdjustFactor:
                continue
            ex_date = r.dividOperateDate or date
            self.st.apply_adjust_factor(r.code, r.foreAdjustFactor, ex_date)
            adjusted += 1
        log.info("[2/4] 复权因子 %s：%d 行，前复权调整 %d 只", date, len(rows), adjusted)
        return len(rows)

    # ---------- 步骤 2：补证券基本信息 ----------
    def _sync_missing_stock_basics(self, date: str) -> int:
        """对比当日 K 线 code 与 stock_basic，缺的补拉。

        Returns:
            新落库的 stock_basic 行数。
        """
        # 当日 K 线里出现的 code
        klines = self.st.load(Kline, start=date, end=date)
        kline_codes = {k.code for k in klines}
        # 已有基本信息的 code
        basics = self.st.load(StockBasic)
        have = {b.code for b in basics}
        missing = kline_codes - have
        if not missing:
            log.info("[3/4] 证券基本信息：%d 个 code 全部已有，无需补拉", len(kline_codes))
            return 0
        log.info("[3/4] 证券基本信息：缺 %d 个，全量拉取 stock_basic", len(missing))
        # 全量拉一次（baostock 无按 code 列表查询接口，批量更稳）
        fresh = self.bs.stock_basics()
        # 只保留缺的 code
        to_save = [b for b in fresh if b.code in missing]
        self.st.save(to_save)
        log.info("[3/4] 证券基本信息：补拉 %d 条", len(to_save))
        return len(to_save)

    # ---------- 步骤 3：补行业分类 ----------
    def _sync_missing_industries(self) -> int:
        """检查 stock_basic 里行业为空的记录，用 query_stock_industry 补全。

        为避免 UPSERT 把 name/ipoDate 等已有字段清成 NULL，
        先把行业信息合并到内存里的 StockBasic 再整体写回。

        Returns:
            补全了行业的记录数。
        """
        basics = self.st.load(StockBasic)
        # 需要补行业的 code
        need = {b.code for b in basics
                if not (b.industry or "").strip() or not (b.industryClassification or "").strip()}
        if not need:
            log.info("[4/4] 行业分类：全部齐全，无需补拉")
            return 0
        log.info("[4/4] 行业分类：缺 %d 个，全量拉取 industry", len(need))
        # 全量拉行业
        industry_rows = self.bs.stock_industries()
        ind_map = {b.code: b for b in industry_rows}

        merged = []
        filled = 0
        for b in basics:
            ind = ind_map.get(b.code)
            if ind is None:
                continue
            changed = False
            if not (b.industry or "").strip() and (ind.industry or "").strip():
                b.industry = ind.industry
                changed = True
            if not (b.industryClassification or "").strip() and (ind.industryClassification or "").strip():
                b.industryClassification = ind.industryClassification
                changed = True
            if changed:
                merged.append(b)
                filled += 1
        if merged:
            self.st.save(merged)
        log.info("[4/4] 行业分类：补全 %d 条", filled)
        return filled


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else None
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    DailyTask().run(date)


if __name__ == "__main__":
    main()
