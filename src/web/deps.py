# -*- coding: utf-8 -*-
"""Web 层共享工具：鉴权依赖、连接、脱敏、错误清洗。

独立成模块是为了让 app.py 与 routers/* 都能 import 它，避免循环导入。
"""

import logging
import os
from datetime import datetime

from fastapi import HTTPException, Request

from src.config import config
from src.storage import storage

log = logging.getLogger("web")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 这些关键字命中的配置一律打码（授权码、账号、收件人都算隐私）
SECRET_HINTS = ("PASS", "SECRET", "TOKEN", "KEY", "USER", "MAIL_TO", "MAIL_FROM")


def conn():
    """按请求打开数据库连接（调用方负责 close）。"""
    return storage.get_conn()


def safe_msg(e: Exception) -> str:
    """把异常转成可以回给浏览器的短消息（去掉绝对路径等内部信息）。"""
    txt = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
    if _ROOT in txt:
        txt = txt.replace(_ROOT, "<项目目录>")
    return txt[:200]


def mask(key: str, value):
    """敏感配置打码，避免授权码/账号经接口泄露到浏览器。"""
    if any(k in key.upper() for k in SECRET_HINTS):
        s = str(value or "")
        if not s:
            return ""
        if len(s) <= 6:
            return "*" * len(s)
        return s[:3] + "*" * min(len(s) - 3, 8)
    return value


def is_secret(key: str) -> bool:
    return any(k in key.upper() for k in SECRET_HINTS)


def require_auth(request: Request):
    """接口鉴权 + CSRF 防护（依赖注入，按需挂在路由上）。

    * WEB_AUTH_TOKEN 为空 → 不做鉴权（默认；适合只在本机/可信内网使用）；
      但写操作仍要求 X-Requested-With，挡掉跨站伪造请求。
    * WEB_AUTH_TOKEN 非空 → 必须带 X-Auth-Token 头或 ?token= 参数。
    """
    token = config.get_str("WEB_AUTH_TOKEN", "")
    method = request.method.upper()
    if method in ("POST", "PUT", "DELETE", "PATCH"):
        if request.headers.get("x-requested-with", "").lower() != "fetch":
            raise HTTPException(status_code=403,
                                detail="缺少 X-Requested-With 请求头（防护跨站请求伪造）")
    if token:
        got = (request.headers.get("x-auth-token")
               or request.query_params.get("token") or "")
        if got != token:
            raise HTTPException(status_code=401, detail="未授权：请先填写访问令牌")


def years_ago(years: int) -> str:
    """N 年之前的日期字符串（用于序列接口的默认窗口）。"""
    import datetime as _dt
    return (_dt.datetime.now() - _dt.timedelta(days=int(365.25 * years))
            ).strftime("%Y-%m-%d")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


WEIGHT_KEYS = ("pe", "pb", "ps", "pcf", "dividend")


def normalize_weights(d: dict) -> dict:
    """把用户提交的权重归一化到合计 1.0。

    接受任意正数比例（如 30/25/15/10/20 或 0.3/0.25/0.15/0.1/0.2），
    一律除以合计。非法输入（全 0 / 负数 / NaN）抛 ValueError。
    """
    w = {}
    for k in WEIGHT_KEYS:
        v = d.get(k) if isinstance(d, dict) else 0
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0.0
        if v != v or v < 0:                      # NaN / 负数
            raise ValueError(f"权重 {k} 不合法：{v!r}")
        w[k] = v
    s = sum(w.values())
    if s <= 0:
        raise ValueError("权重合计不能为 0（至少给一个指标 >0 的权重）")
    return {k: round(v / s, 4) for k, v in w.items()}
