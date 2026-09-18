# -*- coding: utf-8 -*-
"""周频任务：补齐每只证券/ETF 的历史 kline 缺口，断点续传。

流程
----
1. 遍历 stock_basic 每个 code：
   - 若 ``kline_full_sync_date`` 为空 → 首次同步，起点 = ipoDate（无则 10 年前）；
   - 若已有值 → 增量，起点 = kline_full_sync_date。
2. 用 trade_date 表算出 [起点, 今天] 的所有交易日，对比 kline 表该 code
   已有的 date，差集即缺口；有缺口就用 ``bs.kline()``（前复权）补拉落库。
3. 补完后把 ``kline_full_sync_date`` 更新为今天。

与 daily_task 的区别
--------------------
* daily_task：拉全市场当日快照（接口固定不复权）；
* 本任务：补历史缺口（前复权），并维护断点，避免每次全量重拉。

用法
----
    python -m src.schedule.kline_backfill_task
"""

import logging
from datetime import datetime, timedelta

from src.fetch_data.data_fetcher import BaostockSession
from src.storage.storage import Storage
from src.model.Kline import Kline
from src.model.TradeDate import TradeDate
from src.model.StockBasic import StockBasic

log = logging.getLogger("kline_backfill")


class KlineBackfillTask:
    """补齐历史 kline 缺口的周频任务。"""

    # ipoDate 为空时往前取多少年
    FALLBACK_YEARS = 10

    def __init__(self):
        self.bs = BaostockSession()
        self.st = Storage()

    def run(self) -> dict:
        """执行一轮补缺口。返回统计。"""
        log.info("===== kline 补缺口任务开始 =====")
        self.bs.login()
        try:
            self.st.ensure_schema()
            today = datetime.now().strftime("%Y-%m-%d")
            basics = self.st.load(StockBasic)
            log.info("共 %d 只证券待检查", len(basics))

            stats = {"checked": 0, "backfilled": 0, "rows": 0, "skipped": 0}
            for b in basics:
                stats["checked"] += 1
                try:
                    n = self._backfill_one(b, today)
                    if n > 0:
                        stats["backfilled"] += 1
                        stats["rows"] += n
                    else:
                        stats["skipped"] += 1
                except Exception as e:  # noqa: BLE001
                    log.error("  %s 补缺口失败: %s", b.code, e)
                if stats["checked"] % 100 == 0:
                    log.info("  进度 %d/%d", stats["checked"], len(basics))

            log.info("===== kline 补缺口完成 %s =====", stats)
            return stats
        finally:
            self.bs.logout()
            self.st.close()

    def _backfill_one(self, b: StockBasic, today: str) -> int:
        """补单只证券的 kline 缺口。返回新拉的行数。"""
        # 1) 决定起点
        if b.kline_full_sync_date:
            start = b.kline_full_sync_date
        elif b.ipoDate:
            start = b.ipoDate
        else:
            start = (datetime.now() - timedelta(days=365 * self.FALLBACK_YEARS)).strftime("%Y-%m-%d")

        # 2) 该区间所有交易日
        trade_dates = self.st.load(TradeDate, start=start, end=today)
        trading = {t.calendar_date for t in trade_dates if t.is_trading_day == 1}
        if not trading:
            log.debug("  %s 区间无交易日，跳过", b.code)
            return 0

        # 3) kline 表已有哪些 date
        have = {k.date for k in self.st.load(Kline, code=b.code, start=start, end=today)}
        missing = sorted(trading - have)
        if not missing:
            log.debug("  %s 无缺口（%s ~ %s）", b.code, start, today)
            # 即使没缺口也要推进断点
            self._mark_synced(b, today)
            return 0

        # 4) 有缺口：从最早缺的那天拉到今天（baostock 按区间拉，一次搞定）
        pull_start = missing[0]
        log.info("  %s 缺 %d 个交易日，拉取 %s ~ %s", b.code, len(missing), pull_start, today)
        rows = self.bs.kline(b.code, pull_start, today)
        self.st.save(rows)

        # 5) 更新断点
        self._mark_synced(b, today)
        return len(rows)

    def _mark_synced(self, b: StockBasic, today: str) -> None:
        """更新 stock_basic.kline_full_sync_date 为今天。"""
        b.kline_full_sync_date = today
        self.st.save(b)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    KlineBackfillTask().run()


if __name__ == "__main__":
    main()
