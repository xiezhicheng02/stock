#!/usr/bin/env bash
# ============================================================
# 指数估值评分 - Web 服务启动脚本（含 APScheduler 定时任务）
#
# 用法:
#   ./run_web.sh                 # 后台启动（日志进 logs/web_<日期>.log）
#   ./run_web.sh -f              # 前台启动（Ctrl+C 停止，调试用）
#   ./run_web.sh --port 8080     # 透传参数给 src/web/app.py
#   ./run_web.sh status          # 查看运行状态
#   ./run_web.sh stop            # 停止后台进程
#   ./run_web.sh --reinstall     # 强制重装依赖后再启动
#
# 说明:
#   * 定时任务与 web 同进程，因此**固定单 worker**；多 worker 会重复执行任务。
#   * 原 cron 入口 script/run.sh 已废弃，定时能力统一交给 APScheduler。
#   * 启动前会自动**引导环境**：没有 .venv 就用 python3 创建，
#     依赖缺失或 requirements.txt 变过就 pip install -r（status/stop 不做这些）。
# ============================================================
set -u

# 脚本所在目录 → 仓库根目录（兼容从任意 cwd 调用）
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT" || exit 1

# --reinstall 摘出来，别透传给 src/web/app.py（那个 argparse 不认）
REINSTALL=0
ARGS=()
for _a in "$@"; do
    if [ "$_a" = "--reinstall" ]; then REINSTALL=1; continue; fi
    ARGS+=("$_a")
done
set -- ${ARGS[@]+"${ARGS[@]}"}

PY=".venv/bin/python"
VENV_DIR=".venv"
STAMP="$VENV_DIR/.requirements.stamp"
LOG_DIR="logs"
PID_FILE="$LOG_DIR/web.pid"
mkdir -p "$LOG_DIR"

# ---------- 环境引导：venv + 依赖 ----------
# 只在真正要启动时调用（status/stop 不建环境、不装依赖）
bootstrap_env() {
    local created=0
    if [ ! -x "$PY" ]; then
        local base=""
        for c in python3 python; do
            command -v "$c" >/dev/null 2>&1 && { base="$c"; break; }
        done
        if [ -z "$base" ]; then
            echo "找不到 python3，请先安装 Python 3（推荐 3.12）" >&2
            exit 1
        fi
        echo "未找到虚拟环境 $VENV_DIR，用 $base 创建…"
        "$base" -m venv "$VENV_DIR" || { echo "创建虚拟环境失败" >&2; exit 1; }
        echo "  虚拟环境已就绪：$VENV_DIR"
        created=1
    fi

    [ -f requirements.txt ] || return 0
    # 依赖指纹：requirements.txt 变了（或新环境没装过）才重装，避免每次启动都等 pip
    local want have=""
    want="$(sha1sum requirements.txt 2>/dev/null | awk '{print $1}')"
    [ -n "$want" ] || want="$(date -r requirements.txt +%s 2>/dev/null || echo unknown)"
    [ -f "$STAMP" ] && have="$(cat "$STAMP" 2>/dev/null)"
    if [ "$have" = "$want" ] && [ "$REINSTALL" != "1" ]; then
        return 0
    fi
    echo "安装/更新依赖（requirements.txt）…"
    "$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
    if "$PY" -m pip install -r requirements.txt; then
        printf '%s' "$want" >"$STAMP"
        echo "  依赖已就绪"
        return 0
    fi
    # 装失败时**不要一刀切中止启动**：已有环境很可能本来就能跑
    # （网络抖动、某个包临时拉不到都会返回非 0）。只有"刚建的空环境"才是致命的。
    if [ "$created" = "1" ]; then
        echo "依赖安装失败，且虚拟环境是刚创建的（里面什么都没有）" >&2
        echo "请检查网络/pip 源后重试：./run_web.sh --reinstall" >&2
        exit 1
    fi
    echo "警告：依赖安装未成功，但沿用现有环境继续启动" >&2
    echo "      如启动异常请执行：./run_web.sh --reinstall" >&2
    return 0
}

# 日志/预览/图表只保留 30 天，避免占满树莓派 TF 卡
find "$LOG_DIR" -maxdepth 1 -name 'web_*.log' -mtime +30 -delete 2>/dev/null
find "$LOG_DIR/preview" -maxdepth 1 -name '*.html' -mtime +30 -delete 2>/dev/null
find "$LOG_DIR/charts" -maxdepth 1 -name '*.png' -mtime +30 -delete 2>/dev/null

ACTION="${1:-start}"

# ---------- 状态 ----------
if [ "$ACTION" = "status" ]; then
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "运行中：pid $(cat "$PID_FILE")"
        # status 不该因为"没有 venv"而失败，也不该顺手建环境
        STATUS_PY="$PY"; [ -x "$STATUS_PY" ] || STATUS_PY="python3"
        "$STATUS_PY" - <<'EOF' 2>/dev/null || true
import sys, json, urllib.request
sys.path.insert(0, ".")
from src.config import config
web = config.web()
url = f"http://127.0.0.1:{web['port']}/api/health"
try:
    with urllib.request.urlopen(url, timeout=5) as r:
        d = json.load(r)
    print(f"  健康检查 ok={d['ok']}  版本={d['version']}")
    print(f"  调度器 running={d['scheduler']['running']} 任务数={d['scheduler']['job_count']}")
    print(f"  最近跑批={d['runtime']['last_run_at']}  最近发信={d['runtime']['last_mail_at']}")
except Exception as e:
    print("  健康检查失败（服务可能仍在启动中）：", e)
EOF
    else
        echo "未运行（$PID_FILE 不存在或进程已退出）"
        rm -f "$PID_FILE"
    fi
    exit 0
fi

# ---------- 停止 ----------
if [ "$ACTION" = "stop" ]; then
    STOPPED=0
    # ① 按 PID 文件停（后台/前台模式都会写这个文件）
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        PID="$(cat "$PID_FILE")"
        kill "$PID" 2>/dev/null && echo "已停止 web 服务（pid $PID）"
        STOPPED=1
    fi
    # ② 兜底：按命令行匹配杀掉残留进程（含前台 Ctrl+Z 挂起的、PID 文件丢失的）
    if pkill -f "$PY -m src.web.app" 2>/dev/null; then
        echo "已清理残留的 web 进程"
        STOPPED=1
    fi
    sleep 1
    # ③ 仍有残留则强杀（进程处于 T=停止 状态时普通 kill 可能无效）
    if pgrep -f "$PY -m src.web.app" >/dev/null 2>&1; then
        pkill -9 -f "$PY -m src.web.app" 2>/dev/null
        echo "已强制清理残留进程"
        STOPPED=1
    fi
    rm -f "$PID_FILE"
    [ "$STOPPED" = "1" ] || echo "没有正在运行的 web 服务"
    exit 0
fi

# ---------- 前台 ----------
if [ "$ACTION" = "-f" ] || [ "$ACTION" = "--foreground" ]; then
    bootstrap_env
    shift 2>/dev/null || true
    # 前台也记录 PID（exec 后进程号不变），这样 stop 能停掉它
    echo $$ >"$PID_FILE"
    trap 'rm -f "$PID_FILE"' EXIT
    exec "$PY" -m src.web.app "$@"
fi

# ---------- 后台（默认）----------
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "web 服务已在运行（pid $(cat "$PID_FILE")），如需重启请先 ./run_web.sh stop"
    exit 1
fi

bootstrap_env

LOG_FILE="$LOG_DIR/web_$(date +%Y%m%d).log"
nohup "$PY" -m src.web.app "$@" >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
sleep 2

if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    HOST="$("$PY" -c 'import sys; sys.path.insert(0,"."); from src.config import config; print(config.web()["host"])' 2>/dev/null)"
    PORT="$("$PY" -c 'import sys; sys.path.insert(0,"."); from src.config import config; print(config.web()["port"])' 2>/dev/null)"
    echo "web 服务已启动：pid $(cat "$PID_FILE")"
    echo "  访问地址 http://${HOST:-0.0.0.0}:${PORT:-8000}/"
    echo "  日志文件 $LOG_FILE"
    echo "  停止命令 $0 stop"
else
    echo "启动失败，请查看日志：$LOG_FILE"
    tail -n 20 "$LOG_FILE" 2>/dev/null
    rm -f "$PID_FILE"
    exit 1
fi
