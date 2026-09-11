# -*- coding: utf-8 -*-
"""本地私有配置模板 —— 复制为 local_config.py 后填写你自己的值。

    cp local_config.example.py local_config.py

⚠️ local_config.py 含授权码，已在 .gitignore 中忽略，**不要**提交到仓库。
"""

# ---------- SMTP 发件服务 ----------
# 以 QQ 邮箱为例：设置 → 账户 → 开启 SMTP 服务 → 生成授权码
# 其他邮箱：
#   163:    smtp.163.com:465    (SSL)
#   QQ:     smtp.qq.com:465     (SSL)
#   Gmail:  smtp.gmail.com:465  (SSL，需应用专用密码)
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465                 # 465 = SSL；587 则走 STARTTLS

# ---------- 账号与授权码 ----------
SMTP_USER = "your_account@qq.com"   # 发件邮箱
SMTP_PASS = "your_smtp_auth_code"   # SMTP 授权码（**不是**邮箱登录密码）

# ---------- 收发件人 ----------
MAIL_FROM = SMTP_USER
MAIL_TO = ["receiver@example.com"]  # 收件人，可多个
