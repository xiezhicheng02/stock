# -*- coding: utf-8 -*-
"""APScheduler 封装：BackgroundScheduler，与 FastAPI 同进程运行。

职责边界
--------
  * 本模块只管"什么时候跑"：触发器注册、启停、状态查询、手动触发；
  * "跑什么"定义在 src/web/jobs.py，两者通过 JOBS 登记表衔接。

为什么必须单 worker
-------------------
  调度器随 FastAPI 的 lifespan 启停。若 uvicorn 用 ``--workers N``（N>1），
  每个进程都会起一套调度器 → 同一任务被跑 N 次。因此启动入口
  ``script/run_web.sh`` 固定单 worker，并在 app.py 里做了显式校验。

执行模型
--------
  BackgroundScheduler 用线程池执行任务；任务里请用短事务（storage 层已是 WAL），
  不要跨线程复用 sqlite 连接。

状态留痕
--------
  每次执行结果写入两条留痕：内存环形缓冲 ``recent()``（供页面实时展示，
  进程重启即清空）与配置表 ``SCHEDULER_LAST_RUNS``（持久化，供重启后回看）。
"""

import logging
import os
import threading
from datetime import datetime

from apscheduler.events import (EVENT_JOB_ERROR, EVENT_JOB_EXECUTED,
                                EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED,
                                EVENT_JOB_SUBMITTED)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import config
from src.web import jobs as jobs_mod

log = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()
_single_lock_file = None          # 进程级单实例锁（保持引用，勿被 GC）
_recent: list[dict] = []          # 最近执行记录（新→旧）
_RECENT_MAX = 50
_running: dict[str, str] = {}     # 正在执行的任务：job_id -> 开始时间文本


# =====================================================================
# 内部工具
# =====================================================================
def _resolve(value):
    """触发参数允许写成 lambda: config.get_xxx(...)，取值时统一求值。"""
    return value() if callable(value) else value


def _build_trigger(spec: dict):
    """把 JOBS 里的触发描述转成 APScheduler 的 trigger 对象。"""
    kind = (spec.get("type") or "interval").lower()
    if kind == "interval":
        kwargs = {k: _resolve(v) for k, v in spec.items() if k != "type"}
        return IntervalTrigger(**kwargs)
    if kind == "cron":
        kwargs = {k: _resolve(v) for k, v in spec.items() if k != "type"}
        return CronTrigger(**kwargs)
    raise ValueError(f"不支持的触发器类型：{kind}（只支持 interval / cron）")


def _trigger_text(trigger, trading_day_only: bool = True) -> str:
    """触发器的中文可读描述（供页面展示）。

    trading_day_only=False 时不加"（仅交易日执行）"后缀 —— 例如「计算指标」
    是纯离线补算任务，任何一天都会跑，标成"仅交易日"会与实际行为不符。
    """
    suffix = "（仅交易日执行）" if trading_day_only else ""
    if isinstance(trigger, IntervalTrigger):
        total = int(trigger.interval.total_seconds())
        if total % 3600 == 0:
            return f"每 {total // 3600} 小时"
        if total % 60 == 0:
            return f"每 {total // 60} 分钟"
        return f"每 {total} 秒"
    if isinstance(trigger, CronTrigger):
        # fields 顺序：year,month,day,week,day_of_week,hour,minute,second
        try:
            dow = trigger.fields[4]
            hour = trigger.fields[5]
            minute = trigger.fields[6]

            def _pad(x):
                s = str(x)
                try:
                    return f"{int(s):02d}"
                except (ValueError, TypeError):
                    return s
            every = "*" in str(dow) or str(dow) == "0-6"
            h_str, m_str = str(hour), str(minute)
            # 按间隔跑的（小时不限、分钟是列表）：例如计算指标落在每小时的 05/35 分
            if "*" in h_str and any(c in m_str for c in ",-/"):
                mins = "、".join(_pad(x) for x in m_str.split(","))
                return (f"每小时的 {mins} 分{suffix}" if every
                        else f"每周 {dow} 的 {mins} 分")
            if "*" in h_str:
                return (f"每小时第 {_pad(m_str)} 分{suffix}" if every
                        else f"每周 {dow} 第 {_pad(m_str)} 分")
            time_txt = f"{_pad(h_str)}:{_pad(m_str)}"
            # 触发器本身就是每天；是否真跑由交易日历在任务里判断
            return f"每天 {time_txt}{suffix}" if every else f"每周 {dow} {time_txt}"
        except (IndexError, AttributeError):
            pass
    return str(trigger)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def recent(limit: int = 20) -> list[dict]:
    """最近执行记录（新→旧，内存）。"""
    return _recent[:limit]


def _remember(job_id: str, ok: bool, msg: str, set_last: bool = True):
    """记一次执行结果：内存缓冲 + 配置表持久化。

    set_last=False 用于"这次根本没执行"的事件（被 max_instances 挡掉、错过触发）：
    它们只进"最近执行记录"列表，**不覆盖**该任务的"最近一次执行结果"。
    否则会出现"任务明明跑完了，界面上却显示失败"——例如 20:00 的 data_sync 还在跑，
    20:21 有人手动点了一次被挡掉，那条"跳过"就把成功的记录顶掉了。
    """
    item = {"job_id": job_id, "at": _now_text(), "ok": ok, "msg": msg}
    _recent.insert(0, item)
    del _recent[_RECENT_MAX:]
    if not set_last:
        return
    try:
        # 用 update_json 做原子的读-改-写：线程池里多个任务的完成事件会并发写这个键，
        # 直接 get+set 会互相覆盖（某个任务的执行结果会凭空消失）
        config.update_json("SCHEDULER_LAST_RUNS",
                           lambda d: {**(d or {}), job_id: item})
    except Exception as e:                      # noqa: BLE001
        log.warning("执行结果落库失败（不影响任务本身）：%s", e)


def _on_event(event):
    """APScheduler 事件监听：统一记录成功/失败/错过 + 跟踪运行状态。"""
    job_id = event.job_id
    if event.code == EVENT_JOB_SUBMITTED:
        # 任务真正开始执行（提交到线程池的时刻），记录为"运行中"
        _running[job_id] = _now_text()
    elif event.code == EVENT_JOB_EXECUTED:
        _running.pop(job_id, None)
        retval = getattr(event, "retval", None)
        _remember(job_id, True, str(retval) if retval is not None else "执行完成")
    elif event.code == EVENT_JOB_ERROR:
        _running.pop(job_id, None)
        _remember(job_id, False, f"异常：{getattr(event, 'exception', None)}")
        log.exception("任务 %s 执行失败", job_id,
                      exc_info=getattr(event, "exception", None))
    elif event.code == EVENT_JOB_MISSED:
        # 没执行（进程没运行/任务过慢）：只进历史列表，不覆盖"最近一次执行结果"
        _remember(job_id, False, "错过触发时间（进程未运行或任务过慢）",
                  set_last=False)
        log.warning("任务 %s 错过触发时间", job_id)
    elif event.code == EVENT_JOB_MAX_INSTANCES:
        # 被"上一次还没跑完"挡掉：这次根本没执行，
        # 而且**上一次还在跑**，它跑完自然会写结果 —— 不能在这里覆盖
        _remember(job_id, False, "跳过：上一次还没跑完（max_instances 限制）",
                  set_last=False)
        log.warning("任务 %s 因上一次仍在运行被跳过", job_id)


# =====================================================================
# 生命周期
# =====================================================================
def get_scheduler() -> BackgroundScheduler:
    """取（必要时创建）调度器单例，但不启动。"""
    global _scheduler
    with _lock:
        if _scheduler is None:
            cfg = config.scheduler()
            sched = BackgroundScheduler(
                timezone=cfg["timezone"],
                job_defaults={
                    "coalesce": cfg["coalesce"],
                    "misfire_grace_time": cfg["misfire_grace"],
                    "max_instances": cfg["max_instances"],
                },
            )
            sched.add_listener(_on_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR
                               | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES
                               | EVENT_JOB_SUBMITTED)
            _scheduler = sched
        return _scheduler


def register_jobs(sched: BackgroundScheduler | None = None) -> list[str]:
    """按 JOBS 登记表注册任务，返回已注册的任务 id 列表。"""
    sched = sched or get_scheduler()
    added = []
    for spec in jobs_mod.JOBS():
        job_id = spec["id"]
        try:
            if spec.get("enabled") and not spec["enabled"]():
                log.info("任务 %s 未启用（配置关闭），跳过注册", job_id)
                continue
            sched.add_job(spec["func"], _build_trigger(spec["trigger"]),
                          id=job_id, name=spec.get("name", job_id),
                          replace_existing=True)
            added.append(job_id)
        except Exception as e:                  # noqa: BLE001
            # 注册失败（例如配置写成了非法值 MAIN_RUN_HOUR=25）不能静默：
            # 否则页面上只是少一行任务，用户不知道定时任务已经没了
            log.exception("任务 %s 注册失败：%s", job_id, e)
            _remember(job_id, False, f"注册失败（配置可能有误）：{e}")
    return added


def acquire_single_instance_lock() -> bool:
    """抢占"本机只有一个调度器"的文件锁。

    uvicorn --workers N（或手工起第二个进程）时每个进程都会跑一套定时任务 →
    重复抓数、重复发信。这里用 flock 独占锁做硬保证：抢不到锁的进程不启动调度器。
    依赖 POSIX（树莓派/Linux 均可用）。
    """
    global _single_lock_file
    try:
        import fcntl
    except ImportError:                      # 非 POSIX 平台：退化为不校验
        log.warning("当前平台不支持 fcntl，跳过调度器单实例锁")
        return True
    # 锁文件默认在项目根的 logs/ 下（不要用数据库目录推导：--db 传绝对路径时会算错）。
    # 可用 SCHEDULER_LOCK_FILE 改成别的路径 —— 例如同一台机器上用不同数据库跑
    # 第二套实例（测试库）时，各用各的锁即可互不干扰。
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    lock_rel = config.get_str("SCHEDULER_LOCK_FILE", "logs/scheduler.lock")
    path = lock_rel if os.path.isabs(lock_rel) else os.path.join(root, lock_rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "w")                  # noqa: SIM115 需长期持有
    except OSError as e:
        # 建不了锁文件（目录权限等）不该拦住服务：退化为不校验并明确告警
        log.warning("无法创建调度器锁文件 %s（%s），本次不做单实例校验", path, e)
        return True
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        log.error("已有另一个进程持有调度器锁（%s）：本进程不启动定时任务，"
                  "避免重复抓数与重复发信", path)
        return False
    f.write(str(os.getpid()))
    f.flush()
    _single_lock_file = f
    return True


def start() -> BackgroundScheduler | None:
    """启动调度器并注册任务；已启动则直接返回（幂等）。"""
    cfg = config.scheduler()
    if not cfg["enabled"]:
        log.warning("SCHEDULER_ENABLED=False，调度器不启动")
        return None
    if not is_running() and not acquire_single_instance_lock():
        return None
    sched = get_scheduler()
    if sched.running:
        return sched
    added = register_jobs(sched)
    sched.start()
    log.info("调度器已启动（时区 %s），注册任务：%s",
             cfg["timezone"], ", ".join(added) or "无")
    return sched


def shutdown(wait: bool = False):
    """停止调度器（幂等）。

    必须把实例置空：APScheduler 的 ThreadPoolExecutor 在 shutdown 后不可复用，
    同一个实例再 start() 会注册成功、状态显示"运行中"，但每次触发都抛
    "cannot schedule new futures after shutdown" —— 也就是安静地死掉。
    置空后下次 start() 会重建实例与执行器。
    """
    global _scheduler, _single_lock_file
    with _lock:
        sched, _scheduler = _scheduler, None
    if sched and sched.running:
        sched.shutdown(wait=wait)
        log.info("调度器已停止")
    # 释放单实例锁：否则同一进程里"停后再启"会以为自己被别的进程占着（flock 认 fd）
    if _single_lock_file is not None:
        try:
            _single_lock_file.close()
        except OSError:
            pass
        _single_lock_file = None


def is_running() -> bool:
    sched = _scheduler
    return bool(sched and sched.running)


# =====================================================================
# 配置变更后让调度器生效（设置页保存后调用，无需重启服务）
# =====================================================================
_reload_lock = threading.Lock()


def reload_jobs() -> dict:
    """按**当前配置**重新注册任务（只改了触发时间/间隔时用，不中断调度器）。

    ``add_job(replace_existing=True)`` 会原地替换触发器；同时移除已不存在的任务
    （例如把 NOTIFY_RESEND_TIMES 从 3 个减到 1 个）。
    """
    sched = _scheduler
    if not sched or not sched.running:
        return {"ok": False, "running": False, "jobs": [],
                "msg": "调度器未运行，无法应用新配置"}
    want = {spec["id"]: spec for spec in jobs_mod.JOBS()}
    removed = []
    for job in list(sched.get_jobs()):
        if job.id not in want:
            try:
                sched.remove_job(job.id)
                removed.append(job.id)
            except Exception as e:                  # noqa: BLE001
                log.warning("移除任务 %s 失败：%s", job.id, e)
    ids = []
    for jid, spec in want.items():
        try:
            sched.add_job(spec["func"], _build_trigger(spec["trigger"]),
                          id=jid, name=spec.get("name", jid),
                          replace_existing=True)
            ids.append(jid)
        except Exception as e:                      # noqa: BLE001
            log.exception("任务 %s 重新注册失败（配置可能有误）：%s", jid, e)
    log.info("定时任务已按新配置重新注册：%s%s", ", ".join(ids),
             ("；移除 " + ", ".join(removed)) if removed else "")
    return {"ok": True, "running": True, "jobs": ids, "removed": removed,
            "msg": f"定时任务已按新配置生效（{len(ids)} 个任务）"}


def apply_config(structural: bool = False) -> dict:
    """让设置页改动的调度配置立即生效。

    * 关了 SCHEDULER_ENABLED → 停掉调度器；
    * structural=True（时区/错过容忍/并发数等构造参数）→ 重建调度器；
    * 其余（触发时间/间隔）→ 原地重注册任务，不中断调度。
    """
    with _reload_lock:
        cfg = config.scheduler()
        if not cfg["enabled"]:
            shutdown()
            log.info("调度器已按配置关闭")
            return {"ok": True, "running": False, "jobs": [],
                    "msg": "调度器已按配置关闭（定时任务不再执行）"}
        if structural or not is_running():
            if is_running():
                shutdown()
            sched = start()
            if not sched:
                return {"ok": False, "running": False, "jobs": [],
                        "msg": "调度器启动失败（锁被占用？详见日志）"}
            return {"ok": True, "running": True,
                    "jobs": [j.id for j in sched.get_jobs()],
                    "msg": "调度器已按新配置重建"}
        return reload_jobs()


# =====================================================================
# 状态查询 / 手动触发
# =====================================================================
def jobs_info() -> list[dict]:
    """当前任务清单（含下次运行时间与最近一次执行结果）。"""
    sched = _scheduler
    last_runs = {}
    try:
        last_runs = config.get_json("SCHEDULER_LAST_RUNS", {}) or {}
    except Exception:                           # noqa: BLE001
        pass

    out = []
    if sched:
        for job in sched.get_jobs():
            nxt = job.next_run_time
            spec = jobs_mod.by_id(job.id) or {}
            out.append({
                "id": job.id,
                "name": job.name,
                "remark": spec.get("remark", ""),
                "trigger": _trigger_text(
                    job.trigger,
                    trading_day_only=job.id not in jobs_mod.NO_HOLIDAY_GUARD),
                "next_run": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None,
                "running": job.id in _running,
                "running_since": _running.get(job.id),
                "last": last_runs.get(job.id),
            })
    out.sort(key=lambda x: x["id"])
    return out


def status() -> dict:
    """调度器整体状态（供 /api/health 与页面展示）。"""
    cfg = config.scheduler()
    return {
        "enabled": cfg["enabled"],
        "running": is_running(),
        "timezone": cfg["timezone"],
        "misfire_grace": cfg["misfire_grace"],
        "job_count": len(_scheduler.get_jobs()) if _scheduler else 0,
    }


def run_now(job_id: str) -> dict:
    """立即触发一次任务（把 next_run_time 改为当前时刻，由调度器线程执行）。

    返回 {ok, msg}；任务失败不会在这里抛出，结果通过 recent()/日志查看。
    """
    sched = _scheduler
    if not sched or not sched.running:
        return {"ok": False, "msg": "调度器未运行，无法手动触发"}
    job = sched.get_job(job_id)
    if not job:
        return {"ok": False, "msg": f"任务不存在：{job_id}"}
    if job_id in _running:
        return {"ok": False, "msg": f"{job_id} 正在运行中，请等它结束"}
    if job.next_run_time is not None and job.next_run_time <= datetime.now(
            sched.timezone):
        # 已经排了"立即运行"但还没被取走 → 说明上一次还没跑完（max_instances=1）
        return {"ok": False, "msg": f"{job_id} 正在运行中，请等它结束"}
    # 手动触发是明确的用户意图：让任务忽略"非交易日跳过"之类的保护性判断
    jobs_mod.request_force(job_id)
    sched.modify_job(job_id, next_run_time=datetime.now(sched.timezone))
    log.info("手动触发任务 %s", job_id)
    return {"ok": True, "msg": f"已触发 {job_id}，结果稍后刷新查看"}
