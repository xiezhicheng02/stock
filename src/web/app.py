# -*- coding: utf-8 -*-
"""FastAPI 应用入口：只做装配、生命周期、挂静态文件。

所有业务接口在 src/web/routers/ 下。定时任务在 lifespan 里随服务启动，
服务停止时一起关闭。

启动方式
--------
    .venv/Scripts/python -m src.web.app
    .venv/Scripts/python -m src.web.app --port 8080
"""

import argparse
import logging
import os
import sys

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if __package__ in (None, ""):
    sys.path.insert(0, _ROOT)

from src.config import config  # noqa: E402
from src.storage.storage import Storage  # noqa: E402
from src.web.routers import dashboard  # noqa: E402

STATIC_DIR = os.path.join(_HERE, "static")
APP_NAME = "股票数据系统"
APP_VERSION = "0.1.0"

log = logging.getLogger("web")

#: 进程级调度器（lifespan 启停）
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：建表 + 起定时任务；关闭：停定时任务。"""
    global _scheduler
    log.info("启动 %s v%s", APP_NAME, APP_VERSION)

    # 建表
    try:
        st = Storage()
        st.ensure_schema()
        st.close()
    except Exception as e:  # noqa: BLE001
        log.error("建表失败：%s", e)

    # 起定时任务（BackgroundScheduler，不阻塞）
    try:
        from src.schedule.APScheduler import build_scheduler
        _scheduler = build_scheduler()
        _scheduler.start()
        log.info("定时任务已启动：%s", [j.id for j in _scheduler.get_jobs()])
    except Exception as e:  # noqa: BLE001
        log.error("定时任务启动失败：%s", e)

    yield  # ---- 服务运行中 ----

    if _scheduler:
        _scheduler.shutdown(wait=False)
        log.info("定时任务已关闭")


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(dashboard.router)


@app.get("/", include_in_schema=False)
def index():
    """主页。"""
    path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=500, detail="缺少 static/index.html")
    return FileResponse(path)


@app.get("/api/health", summary="健康检查")
def health():
    """简单检查：服务存活 + 数据库可查 + 调度器在跑。"""
    db_ok = False
    try:
        st = Storage()
        st.conn.execute("SELECT 1").fetchone()
        st.close()
        db_ok = True
    except Exception as e:  # noqa: BLE001
        log.warning("健康检查数据库失败：%s", e)
    return {
        "ok": db_ok,
        "app": APP_NAME,
        "version": APP_VERSION,
        "db": "ok" if db_ok else "error",
        "scheduler_running": bool(_scheduler and _scheduler.running),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="启动 Web 服务")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--log-level", default="info")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    host = args.host or config.WEB["host"]
    port = args.port or config.WEB["port"]

    import uvicorn
    log.info("启动 http://%s:%d/", host, port)
    uvicorn.run("src.web.app:app", host=host, port=port,
                log_level=args.log_level.lower(), workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
