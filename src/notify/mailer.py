# -*- coding: utf-8 -*-
"""邮件发送（SMTP + cid 内联图片）。

只负责"把已经渲染好的 HTML 发出去"，不关心内容怎么来的（渲染在 report.py）。
SMTP 参数一律从配置表读（config.smtp()），代码里不出现任何主机/账号硬编码。

用法
----
    from src.notify import mailer
    mailer.check_config()          # → 缺失项列表，空列表表示可发
    mailer.send_mail(subject, html, images=[("img0_score", png_bytes)])
"""

import logging
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

from src.config import config

log = logging.getLogger("mailer")


def check_config() -> list[str]:
    """检查邮件配置完整性，返回缺失项名称列表（空=可用）。"""
    smtp = config.smtp()
    missing = []
    for key, label in (("host", "SMTP_HOST"), ("user", "SMTP_USER"),
                       ("password", "SMTP_PASS"), ("sender", "MAIL_FROM")):
        if not smtp.get(key):
            missing.append(label)
    if not smtp.get("receivers"):
        missing.append("MAIL_TO")
    return missing


def send_mail(subject: str, html: str, images: list | None = None,
              receivers: list | None = None) -> dict:
    """发送 HTML 邮件（图片以 cid 内联）。

    images: [(cid, png_bytes), ...]，HTML 里用 ``<img src="cid:xxx">`` 引用。
    receivers: 覆盖收件人（默认用配置里的 MAIL_TO）。
    返回 {sent, subject, receivers, images}；配置不全或发送失败时抛异常。
    """
    missing = check_config()
    if missing:
        raise RuntimeError(
            f"邮件配置不完整（缺少 {', '.join(missing)}）："
            f"请在数据库中补齐 setting 表对应项（或检查 src/config/local_config.py）")

    smtp = config.smtp()
    to_list = list(receivers or smtp["receivers"])

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = formataddr(("指数估值提醒", smtp["sender"]))
    msg["To"] = ", ".join(to_list)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="stock.local")
    msg.attach(MIMEText(html, "html", "utf-8"))

    for cid, png in (images or []):
        sub = MIMEImage(png, "png")
        sub.add_header("Content-ID", f"<{cid}>")
        sub.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(sub)

    # 建连到发送整体包在 try 里：starttls()/login() 半途失败时也要关掉 socket
    # （否则连接只能等 GC 回收，SMTP 服务器侧还会占着会话）
    server = None
    try:
        if smtp["port"] == 465:
            server = smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=30)
        else:
            server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=30)
            server.starttls()
        server.login(smtp["user"], smtp["password"])
        server.sendmail(smtp["sender"], to_list, msg.as_string())
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:                   # noqa: BLE001
                try:
                    server.close()
                except Exception:               # noqa: BLE001
                    pass

    log.info("邮件已发送 → %s | 标题: %s | 内联图 %d 张",
             to_list, subject, len(images or []))
    return {"sent": True, "subject": subject, "receivers": to_list,
            "images": len(images or [])}
