# -*- coding: utf-8 -*-
"""定时任务调度入口（APScheduler）。

三个任务：
    daily_task            日频：全市场日 K + 复权因子 + 证券基本/行业补全
    week_task             周频：季频财务 + 宏观经济 + 交易日历
    kline_backfill_task   周频：补齐每只证券历史 kline 缺口（前复权）

时间安排（收盘后跑，避开盘中）：
    daily_task          周一~周五 18:00（交易日收盘后）
    kline_backfill_task 周六 20:00（日频跑完后补历史缺口）
    week_task           周日 10:00（季频/宏观）

用法
----
    python -m src.schedule.APScheduler
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src.schedule.daily_task import DailyTask
from src.schedule.week_task import WeekTask
from src.schedule.kline_backfill_task import KlineBackfillTask

log = logging.getLogger("scheduler")


def _run_daily():
    """日频任务包装：异常不中断调度器。"""
    try:
        DailyTask().run()
    except Exception as e:  # noqa: BLE001
        log.exception("daily_task 失败: %s", e)


def _run_week():
    """周频任务包装。"""
    try:
        WeekTask().run()
    except Exception as e:  # noqa: BLE001
        log.exception("week_task 失败: %s", e)


def _run_backfill():
    """kline 补缺口任务包装。"""
    try:
        KlineBackfillTask().run()
    except Exception as e:  # noqa: BLE001
        log.exception("kline_backfill_task 失败: %s", e)


def build_scheduler(blocking: bool = False):
    """构建调度器（不启动）。

    :param blocking: True 时返回 BlockingScheduler（命令行独立跑）；
                     False 时返回 BackgroundScheduler（嵌入 web 进程，不阻塞事件循环）。
    """
    cls = BlockingScheduler if blocking else BackgroundScheduler
    sched = cls(timezone="Asia/Shanghai")

    # 日频：周一~周五 18:00（交易日收盘后）
    sched.add_job(
        _run_daily,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=0),
        id="daily_task",
        misfire_grace_time=3600,
    )

    # kline 补缺口：周六 20:00（日频跑完后）
    sched.add_job(
        _run_backfill,
        CronTrigger(day_of_week="sat", hour=20, minute=0),
        id="kline_backfill_task",
        misfire_grace_time=7200,
    )

    # 周频：周日 10:00
    sched.add_job(
        _run_week,
        CronTrigger(day_of_week="sun", hour=10, minute=0),
        id="week_task",
        misfire_grace_time=7200,
    )

    return sched


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sched = build_scheduler(blocking=True)
    log.info("定时任务已启动：")
    for j in sched.get_jobs():
        log.info("  %s -> %s", j.id, j.trigger)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("调度器退出")


if __name__ == "__main__":
    main()
