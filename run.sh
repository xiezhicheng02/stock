#!/usr/bin/env bash
# ============================================================
# 指数估值提醒 - 定时入口（供 cron 调用）
# 用法:
#   ./run.sh            # 正常模式（按时段+告警规则发邮件）
#   ./run.sh --preview  # 生成 preview.html（不发邮件，调试用）
# ============================================================
set -u

# 脚本所在目录（兼容从任意 cwd 被 cron 调用）
cd "$(dirname "$0")" || exit 1

# 激活 venv（没有则用系统 python）
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

STAMP=$(date +%Y%m%d-%H%M)
LOG_FILE="$LOG_DIR/run_$STAMP.log"

# 执行主脚本，输出同时进屏幕与日志文件
"$PY" main.py "$@" 2>&1 | tee "$LOG_FILE"

# 只保留最近 30 天日志，避免占满树莓派 TF 卡
find "$LOG_DIR" -name "run_*.log" -mtime +30 -delete 2>/dev/null

exit ${PIPESTATUS[0]}
