# -*- coding: utf-8 -*-
"""FastAPI 应用入口：Web 界面 + AJAX 接口。

启动方式
--------
  script/run_web.sh                     # 正常启动（读取配置里的 host/port）
  .venv/bin/python -m src.web.app       # 等价写法
  .venv/bin/python -m src.web.app --port 8080 --no-scheduler

目录约定
--------
  src/web/app.py        本文件：应用装配、生命周期、接口
  src/web/scheduler.py  APScheduler 引擎封装
  src/web/jobs.py       定时任务本体与登记表
  src/web/static/       前端页面（原生 JS + fetch，无 CDN 依赖）

实现约定
--------
  * 调度器与 web 同进程，由 lifespan 启停 → uvicorn 必须单 worker；
    多 worker 会让定时任务重复执行，本模块在启动时显式校验。
  * 接口一律用同步 ``def``：FastAPI 会自动丢到线程池执行，SQLite 是阻塞 IO，
    用 async def 反而会卡住事件循环。
  * 数据库连接按请求创建/关闭，不跨请求复用（sqlite 连接不可跨线程）。
  * 密码类/账号类配置在 /api/settings 里打码后再返回。

访问控制（局域网部署必读）
--------------------------
  默认监听 0.0.0.0 且**没有任何账号体系**，同一局域网内任何人都能打开页面。
  因此：
    * 写操作（POST）必须带自定义头 X-Requested-With，浏览器跨站表单伪造不了它，
      可以挡住"恶意网页偷偷触发发信"（CSRF）；
    * 若设置了 WEB_AUTH_TOKEN，所有 /api/* 接口都要求
      X-Auth-Token 头（或 ?token=）匹配，前端会在 localStorage 里记住；
    * 只在本机用就把 WEB_HOST 设成 127.0.0.1。
"""

import argparse
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if __package__ in (None, ""):              # 支持 python src/web/app.py
    sys.path.insert(0, _ROOT)

from src.config import config            # noqa: E402
from src.notify import mailer, pipeline, report   # noqa: E402
from src.storage import storage          # noqa: E402
from src.web import deps, scheduler as sched        # noqa: E402
from src.web.routers import assets, dashboard, indexmgmt, portfolio, settings, sync  # noqa: E402

STATIC_DIR = os.path.join(_HERE, "static")
APP_NAME = "指数估值评分"
APP_VERSION = "0.2.0"

log = logging.getLogger("web")


def _preview_dir() -> str:
    """预览 HTML 目录（相对路径按仓库根解析）。"""
    d = config.web()["preview_dir"]
    if not os.path.isabs(d):
        d = os.path.join(_ROOT, d)
    os.makedirs(d, exist_ok=True)
    return d


# =====================================================================
# 生命周期：启动时起调度器，关闭时停
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    config.reload()                        # 配置可能在启动前被 init_db 改过
    log.info("配置库：%s", config.DB_PATH)

    # 自愈：确保表结构/列与代码一致（幂等）。升级后忘了跑 init_db 也不会
    # 出现"no such table"这类运行时错误。
    try:
        c = storage.get_conn()
        try:
            storage.ensure_schema(c)
        finally:
            c.close()
    except Exception as e:                      # noqa: BLE001
        log.error("数据库结构自愈失败（请手动运行 script/init_db.py）：%s", e)

    # 自愈：把代码里新增的默认配置补进老库、清理已废弃的键（幂等，不动已有值）
    try:
        config.ensure_settings()
    except Exception as e:                      # noqa: BLE001
        log.error("配置自愈失败（设置页可能少几项）：%s", e)

    # 调度器与 web 同进程：多 worker 会重复执行定时任务，这里直接拦住
    if os.environ.get("WEB_CONCURRENCY") not in (None, "", "1"):
        log.error("检测到 WEB_CONCURRENCY=%s（多 worker）：定时任务会被重复执行！"
                  " 请用单 worker 启动（script/run_web.sh 或 --workers 1）。",
                  os.environ["WEB_CONCURRENCY"])

    if os.environ.get("STOCK_NO_SCHEDULER") == "1":
        log.warning("--no-scheduler：本次不启动定时任务")
    elif config.scheduler()["enabled"]:
        sched.start()
    else:
        log.warning("SCHEDULER_ENABLED=False，定时任务不启动")

    yield                                  # ---- 服务运行中 ----

    sched.shutdown(wait=False)


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class NoCacheStaticMiddleware:
    """纯 ASGI 中间件：给首页/静态资源加 no-cache 响应头，不缓冲响应体。

    之前用 ``@app.middleware("http")`` 实现（即 BaseHTTPMiddleware），它会把
    响应体整体缓冲后再重发，在部分 starlette 版本里对较大的响应会触发
    h11 ``Too much data for declared Content-Length``。这里改成纯 ASGI 中间件，
    只在 http.response.start 消息上改头，不碰响应体。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        no_cache = path == "/" or path.startswith("/static")

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and no_cache:
                headers = [
                    h for h in message.get("headers", [])
                    if h[0].lower() not in (b"cache-control", b"pragma", b"expires")
                ]
                headers.extend([
                    (b"cache-control", b"no-store, no-cache, must-revalidate"),
                    (b"pragma", b"no-cache"),
                    (b"expires", b"0"),
                ])
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(NoCacheStaticMiddleware)
app.include_router(assets.router)
app.include_router(sync.router)
app.include_router(dashboard.router)
app.include_router(indexmgmt.router)
app.include_router(portfolio.router)
app.include_router(settings.router)


# =====================================================================
# 内部工具
# =====================================================================
# =====================================================================
# 页面
# =====================================================================
@app.get("/", include_in_schema=False)
def index():
    """主页（静态 HTML，页面内用 fetch 调 /api/*）。"""
    path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=500, detail="缺少前端文件 static/index.html")
    return FileResponse(path)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # 204 必须无响应体：之前用 JSONResponse(204, None) 会带一个 "null" 正文，
    # 触发 h11 "Too much data for declared Content-Length"。
    return Response(status_code=204)


# =====================================================================
# 接口（占位：先打通链路，业务接口后续再加）
# =====================================================================
@app.get("/api/health", summary="服务与数据库健康状态")
def health(_: None = Depends(deps.require_auth)):
    """服务探针：进程、数据库、各表行数、调度器状态。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = {
        "ok": True,
        "app": APP_NAME,
        "version": APP_VERSION,
        "time": now,
        "pid": os.getpid(),
        # 展示类配置：前端据此重建估值区间/配色/走势图窗口/刷新间隔，
        # 保证设置页的改动能真正反映到网页上（而非各写死一份）。
        "meta": config.ui_meta(),
        "db": {"path": os.path.abspath(config.DB_PATH), "ok": False},
        "scheduler": sched.status(),
        "runtime": {
            "last_run_at": config.get_str("LAST_RUN_AT", "") or None,
            "last_mail_at": config.get_str("LAST_MAIL_AT", "") or None,
            "last_mail_subject": config.get_str("LAST_MAIL_SUBJECT", "") or None,
        },
    }
    try:
        conn = deps.conn()
        try:
            out["db"]["ok"] = True
            out["db"]["tables"] = storage.table_stats(conn)
            # K线覆盖情况：标的数 + 最新日期（一次分组查询，避免逐只查）
            kd = storage.latest_kline_dates(conn)
            out["db"]["kline_codes"] = len(kd)
            out["db"]["kline_latest"] = max(kd.values()) if kd else None
            out["db"]["score_latest"] = storage.latest_score_dates(conn)
            # 交易日历覆盖 + 今天是否交易日
            lo, hi = storage.trade_date_range(conn)
            out["runtime"]["trade_calendar"] = {"start": lo, "end": hi}
            out["runtime"]["is_trading_today"] = pipeline.is_trading_day(conn, now[:10])
        finally:
            conn.close()
    except Exception as e:                      # noqa: BLE001
        out["ok"] = False
        out["db"]["error"] = str(e)
        log.exception("健康检查读库失败")
    return out


@app.get("/api/scheduler/jobs", summary="定时任务清单")
def scheduler_jobs(_: None = Depends(deps.require_auth)):
    """当前已注册的任务、下次运行时间、最近一次执行结果。"""
    return {
        "scheduler": sched.status(),
        "jobs": sched.jobs_info(),
        "recent": sched.recent(20),
    }


@app.post("/api/scheduler/jobs/{job_id}/run", summary="手动触发任务")
def scheduler_run(job_id: str, _: None = Depends(deps.require_auth)):
    """立即触发一次任务（异步执行，结果请稍后刷新查看）。"""
    r = sched.run_now(job_id)
    if not r["ok"]:
        raise HTTPException(status_code=400, detail=r["msg"])
    return r


@app.get("/api/settings", summary="配置项（按分组，账号密码已打码）")
def settings(group: str | None = None, _: None = Depends(deps.require_auth)):
    """读取配置表（只读展示用）。"""
    items = config.all_settings(group)
    rows = []
    for it in items:
        rows.append({
            "key": it["key"],
            "value": deps.mask(it["key"], it["value"]),
            "val_type": it.get("val_type"),
            "group": it.get("group"),
            "remark": it.get("remark"),
        })
    return {"count": len(rows), "items": rows}


# =====================================================================
# 报告：预览（不发信）/ 发送
# =====================================================================
def _prune_previews(keep: int = 50) -> int:
    """只保留最近 N 份预览（自包含 HTML 每份几百 KB，树莓派 SD 卡要省着用）。"""
    d = _preview_dir()
    files = sorted((os.path.join(d, f) for f in os.listdir(d)
                    if f.endswith(".html")), key=os.path.getmtime, reverse=True)
    removed = 0
    for p in files[keep:]:
        try:
            os.remove(p)
            removed += 1
        except OSError:
            pass
    if removed:
        log.info("清理旧预览文件 %d 份（保留最近 %d 份）", removed, keep)
    return removed


@app.post("/api/report/preview", summary="生成邮件预览 HTML（不发信）")
def report_preview(codes: str | None = None, years: int | None = None,
                   _: None = Depends(deps.require_auth)):
    """渲染一份自包含的预览 HTML（图片内嵌 base64），返回可访问的 URL。

    codes: 逗号分隔的标的代码，省略则用全部启用标的；years: 图表窗口（年）。
    """
    code_list = [c.strip() for c in codes.split(",")] if codes else None
    name = report.preview_filename()
    path = os.path.join(_preview_dir(), name)
    conn = deps.conn()
    try:
        r = report.save_preview(conn, path, codes=code_list, years=years)
    except Exception as e:                      # noqa: BLE001
        log.exception("生成预览失败")
        # 不把内部异常原文（可能含路径）回给浏览器
        raise HTTPException(status_code=400, detail=f"生成预览失败：{deps.safe_msg(e)}")
    finally:
        conn.close()
    _prune_previews()
    return {"ok": True, "file": name, "url": f"/preview/{name}",
            "subject": r["subject"], "items": r["items"], "images": r["images"],
            "kb": round(r["bytes"] / 1024, 1)}


@app.get("/preview/{name}", summary="查看已生成的预览 HTML")
def preview_file(name: str, _: None = Depends(deps.require_auth)):
    """访问预览文件（限制在预览目录内，防目录穿越）。"""
    if os.path.sep in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="文件名不合法")
    path = os.path.join(_preview_dir(), name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="预览文件不存在（先生成一次）")
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/api/mail/{mail_id}/html", summary="查看某封已发邮件的正文（新窗口打开）")
def mail_body_html(mail_id: int, _: None = Depends(deps.require_auth)):
    """按 mail_log.id 返回那封邮件的**自包含**正文快照（图片已内联）。

    直接返回 HTML（而不是 JSON），这样首页点一下就能用新窗口打开看到原样正文。
    没有快照时返回一段说明页，而不是 404 —— 新窗口里看到空白会更让人困惑。
    """
    conn = deps.conn()
    try:
        row = storage.load_mail_body_by_id(conn, mail_id)
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"邮件记录不存在：#{mail_id}")
    html = row.get("html")
    if not html:
        html = _no_body_page(row)
    return Response(content=html, media_type="text/html; charset=utf-8")


def _no_body_page(row: dict) -> str:
    """正文快照缺失时的说明页（多为功能上线前发出的历史邮件）。"""
    sent = row.get("sent_at") or "—"
    subject = row.get("subject") or "（无标题）"
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>无正文快照</title></head>
<body style="font-family:-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;
color:#2f3542;background:#f4f5f8;margin:0;padding:48px 20px;">
<div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e6e8ef;
border-radius:12px;padding:24px 26px;">
<div style="font-size:16px;font-weight:800;margin-bottom:10px;">这封邮件没有保存正文</div>
<div style="font-size:13px;line-height:1.8;color:#55606e;">
发送时间：{sent}<br>标题：{subject}</div>
<div style="font-size:12px;line-height:1.8;color:#8b93a5;margin-top:14px;">
正文快照是后加的功能，此前发出的邮件只记录了标题与摘要；<br>
之后新发送的邮件都能在首页点击原样回看。</div>
</div></body></html>"""


@app.get("/api/mail/body/{body_key}/html",
         summary="按正文快照 key 查看邮件正文（首页「待发送」那封）")
def mail_body_by_key(body_key: str, _: None = Depends(deps.require_auth)):
    """按 mail_body.body_key 取正文。

    首页「生成邮件正文」后，那封邮件还没发送、在 mail_log 里还没有 id，
    只有 body_key（= 构建日期），所以单独给一个入口。
    """
    conn = deps.conn()
    try:
        row = storage.load_mail_body(conn, body_key)
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404,
                            detail=f"没有找到正文快照：{body_key}")
    return Response(content=row["html"], media_type="text/html; charset=utf-8")


@app.get("/api/report/previews", summary="预览文件列表")
def preview_list(_: None = Depends(deps.require_auth)):
    """列出已生成的预览文件（新→旧）。"""
    d = _preview_dir()
    if not os.path.isdir(d):
        return {"count": 0, "items": []}
    files = [f for f in os.listdir(d) if f.endswith(".html")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)
    return {"count": len(files),
            "items": [{"file": f, "url": f"/preview/{f}",
                       "kb": round(os.path.getsize(os.path.join(d, f)) / 1024, 1),
                       "mtime": datetime.fromtimestamp(
                           os.path.getmtime(os.path.join(d, f))
                       ).strftime("%Y-%m-%d %H:%M:%S")} for f in files[:20]]}


@app.post("/api/report/send", summary="立即计算并发送邮件")
def report_send(codes: str | None = None, sync: bool = True,
                _: None = Depends(deps.require_auth)):
    """立刻跑一次（可选先抓数）并发信，忽略交易日/时段判断（人工触发）。"""
    missing = mailer.check_config()
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"邮件配置不完整，缺少：{', '.join(missing)}")
    code_list = [c.strip() for c in codes.split(",")] if codes else None
    if pipeline.is_busy():
        raise HTTPException(status_code=409, detail="已有跑批在进行中，请稍后再试")
    conn = deps.conn()
    try:
        r = pipeline.run(conn, sync=sync, send=True, codes=code_list)
    except Exception as e:                      # noqa: BLE001
        log.exception("发送失败")
        raise HTTPException(status_code=400, detail=f"发送失败：{deps.safe_msg(e)}")
    finally:
        conn.close()
    if r.get("busy"):
        raise HTTPException(status_code=409, detail=r.get("message", "已有跑批在进行中"))
    return {"ok": r["ok"], "sent": r["sent"], "subject": r["subject"],
            "steps": r["steps"], "items": r.get("items") or [],
            "elapsed": r.get("elapsed")}


@app.post("/api/report/run", summary="立即跑批（只算不发）")
def report_run(codes: str | None = None, _: None = Depends(deps.require_auth)):
    """跑一次评分（不抓数、不发信），用于页面上的"重新计算"。"""
    code_list = [c.strip() for c in codes.split(",")] if codes else None
    if pipeline.is_busy():
        raise HTTPException(status_code=409, detail="已有跑批在进行中，请稍后再试")
    conn = deps.conn()
    try:
        r = pipeline.run(conn, sync=False, send=False, codes=code_list)
    except Exception as e:                      # noqa: BLE001
        log.exception("跑批失败")
        raise HTTPException(status_code=400, detail=f"跑批失败：{deps.safe_msg(e)}")
    finally:
        conn.close()
    if r.get("busy"):
        raise HTTPException(status_code=409, detail=r.get("message", "已有跑批在进行中"))
    return {"ok": r["ok"], "subject": r["subject"], "steps": r["steps"],
            "items": r.get("items") or [], "elapsed": r.get("elapsed")}


@app.post("/api/report/rebuild-percentiles",
          summary="全量重建所有个股/指数的分位历史")
def report_rebuild_percentiles(freq: str = "D", clean: bool = True,
                               workers: int | None = None,
                               dividend_full: bool = True,
                               _: None = Depends(deps.require_auth)):
    """把全部个股 + 指数的历史分位（10年/5年双窗口）重算落库。

    个股/组合多进程并行（先全量重算动态股息率，再算分位）；指数复用成分股已存
    分位加权聚合。综合评分只算配置了权重的标的。长任务，请求立即返回、后台线程
    执行，进度看服务日志。
    """
    freq = (freq or "D").upper()
    if freq not in ("D", "W", "M"):
        raise HTTPException(status_code=400, detail="freq 只支持 D/W/M")
    if pipeline.rebuild_busy():
        raise HTTPException(status_code=409, detail="已有分位重建在进行中，请稍后")

    def _run():
        c = storage.get_conn()
        try:
            pipeline.rebuild_percentiles(c, freq=freq, clean=clean, wait=True,
                                         workers=workers,
                                         dividend_full=dividend_full)
        except Exception:                       # noqa: BLE001
            log.exception("全量分位重建失败")
        finally:
            c.close()

    threading.Thread(target=_run, name="rebuild-percentiles", daemon=True).start()
    log.info("已在后台开始全量分位重建（freq=%s, clean=%s, workers=%s）",
             freq, clean, workers)
    return {"ok": True, "started": True, "freq": freq,
            "msg": "已开始重建全部分位（多进程并行，后台进行，进度见服务日志）"}


# =====================================================================
# 命令行入口
# =====================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="启动估值评分 Web 服务（含定时任务）")
    p.add_argument("--host", default=None, help="监听地址，默认取配置 WEB_HOST")
    p.add_argument("--port", type=int, default=None, help="监听端口，默认取配置 WEB_PORT")
    p.add_argument("--db", default=None, help="数据库路径，默认取配置 DB_PATH / 环境变量 STOCK_DB")
    p.add_argument("--log-level", default=None, help="uvicorn 日志级别，默认取配置 WEB_LOG_LEVEL")
    p.add_argument("--reload", action="store_true",
                   help="代码热重载（开发用，需 watchfiles；热重载期间不要依赖定时任务）")
    p.add_argument("--no-scheduler", action="store_true", help="本次不启动定时任务")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, (args.log_level or "info").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # --db 必须在读配置前生效（config.use_db 会改 DB_PATH 并清缓存）
    config.use_db(args.db)

    web = config.web()
    host = args.host or web["host"]
    port = args.port or web["port"]
    level = (args.log_level or web["log_level"]).lower()
    reload = args.reload or web["reload"]

    if reload:
        log.warning("热重载已开启：会有两个进程，定时任务可能被重复执行（仅建议开发时用）")
    if args.no_scheduler:
        os.environ["STOCK_NO_SCHEDULER"] = "1"

    # uvicorn 以 import string 方式加载应用，需要仓库根目录在 sys.path 上
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    import uvicorn
    log.info("启动 %s v%s → http://%s:%d/ （调度器：%s）",
             APP_NAME, APP_VERSION, host, port,
             "关闭" if args.no_scheduler else "随服务启动")
    uvicorn.run("src.web.app:app", host=host, port=port, log_level=level,
                reload=reload, workers=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
