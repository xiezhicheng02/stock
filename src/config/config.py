# -*- coding: utf-8 -*-
"""系统配置：数据库 / baostock / 邮件 / Web / 管理员账号。

所有路径均相对项目根（``D:\\project\\PycharmProjects\\stock``）解析，
不依赖运行时 cwd。敏感项（密码、SMTP 授权码）请填到本地后勿提交。
"""

import os
from pathlib import Path

# 项目根：src/config/config.py -> 上溯三级
BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent

# data 目录（SQLite 库文件放这里）
DATA_DIR: Path = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================================
# SQLite 数据库
# =====================================================================
# 相对项目根的库文件路径；storage.Storage() 默认用它
DB_PATH: str = str(DATA_DIR / "stock.db")


# =====================================================================
# baostock 数据源
# =====================================================================
BAOSTOCK = {
    # 单次 socket 超时（秒）——baostock 底层是裸 socket，不配会永久挂起
    "timeout": 60,
    # 单次查询失败重试次数（只重试网络类异常）
    "retry": 3,
    # 每次查询间隔（秒），用于限流；0 表示不限
    "sleep": 0.2,
    # 全量拉取时的最早日期
    "start_date": "1990-01-01",
}

# 系统只跟踪这三个指数（其余指数不拉）
INDEX_CODES = {
    "sh.000001": "上证综合指数",
    "sh.000300": "沪深300",
    "sh.000922": "中证红利",
}


# =====================================================================
# 邮件 SMTP（告警 / 日报推送）
# =====================================================================
SMTP = {
    "host": os.getenv("SMTP_HOST", "smtp.example.com"),
    "port": int(os.getenv("SMTP_PORT", "465")),
    "ssl": True,                     # 465 端口用 SSL；587 用 STARTTLS 时改 False
    "user": os.getenv("SMTP_USER", ""),
    "password": os.getenv("SMTP_PASSWORD", ""),   # 授权码，不是登录密码
    "sender": os.getenv("SMTP_SENDER", ""),       # 发件人地址
    "receivers": [],                              # 默认收件人列表，如 ["me@example.com"]
}


# =====================================================================
# Web 服务
# =====================================================================
WEB = {
    "host": os.getenv("WEB_HOST", "0.0.0.0"),
    "port": int(os.getenv("WEB_PORT", "8000")),
    "debug": False,
}


# =====================================================================
# 管理员账号（Web 登录用）
# =====================================================================
ADMIN = {
    "username": os.getenv("ADMIN_USER", "admin"),
    "password": os.getenv("ADMIN_PASSWORD", "admin"),
}
