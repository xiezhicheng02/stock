# 指数基金估值定投提醒系统

抓取沪深300 / 中证500 / 科创50 等宽基指数估值，多指标加权生成综合评分，
按估值高低给出**定投动作建议**（大额买入 → 分批卖出），通过 HTML 邮件定时推送，
图表展示近 3 年走势。设计为树莓派 3B+ 上常驻的 cron 任务。

## 功能特性

- **估值数据**：PE-TTM / PB / 股息率（乐咕乐股 + 中证指数官网）
- **综合评分**：按指数独立权重加权（PE-TTM + PB + 股息率），缺失指标自动剔除归一
- **5 档信号**：低估🟢大额买入 / 偏低🔵定投 / 正常⚪小额 / 偏高🟠停止 / 高估🔴分批卖出
- **分级提醒**：低估/高估（重点日）一天 4 次；其余状态一天 1 次
- **HTML 邮件**：状态色横幅 + 综合评分 + 各指标 3 年走势图 + 分位表格
- **本地缓存**：SQLite 增量存储历史估值，避免每次全量拉取、越跑越准

## 目录结构

```
stock/
├── main.py                  # 主脚本（取数 → 评分 → 信号 → HTML 邮件）
├── config.py                # ★ 非敏感配置：指数/权重/阈值/配色/时段
├── local_config.py          # 🔒 私有配置：邮箱/授权码（gitignore，自行创建）
├── local_config.example.py  # 私有配置模板（提交到仓库）
├── charts.py                # matplotlib 图表生成（3年折线）
├── storage.py               # SQLite 数据层
├── run.sh                   # cron 调用入口（自动 cd + venv + 日志）
├── requirements.txt         # Python 依赖（已固定版本）
├── preview.html             # 本地预览输出（gitignore，用 --preview 生成）
└── data.db                  # 历史缓存库（gitignore，首次运行自动创建）
```

## 本地快速预览（调试用，不发邮件）

需要 Python 3.10+，先装依赖再跑：

```bash
cd stock
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

python main.py --preview          # 生成 preview.html，浏览器打开查看
```

> `--preview` 模式**绝不发邮件**，只是生成本地 HTML 便于看效果。

---

## 一、树莓派部署

### 1. 前置环境

- **树莓派 3B+**，Raspberry Pi OS（Bookworm 或更新，32/64 位均可）
- Python 3.11+（Bookworm 自带 3.11）
- 能访问外网（数据源：legulegu.com / csindex.com.cn / 新浪财经）

### 2. 拷贝项目到树莓派

```bash
# 在你的开发机上
scp -r stock/ pi@<树莓派IP>:/home/pi/stock
```

> 若用 git：`git clone <你的仓库> stock` 即可。**注意**：`data.db`、
> `logs/`、`.venv/` 已在 .gitignore，不会也不应提交。

### 3. 创建虚拟环境并安装依赖

```bash
ssh pi@<树莓派IP>
cd ~/stock

python3 -m venv .venv
source .venv/bin/activate

# 树莓派 3B+ 编译慢，用清华镜像加速（含 piwheels 可自动命中 ARM 预编译包）
pip config set global.extra-index-url https://www.piwheels.org/simple
pip install -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> akshare 依赖较多，3B+ 上安装约需 5~15 分钟，耐心等待即可。
> 如遇 pip 超时，可重复执行一次安装命令（已装的不重复下载）。

### 4. 配置邮箱与参数

**邮箱/授权码等敏感信息放在 `local_config.py`（不提交 git）：**

```bash
cp local_config.example.py local_config.py
vi local_config.py
```

填写内容：

```python
SMTP_HOST = "smtp.qq.com"        # 或 smtp.163.com
SMTP_PORT = 465
SMTP_USER = "你的邮箱@qq.com"      # 发件邮箱
SMTP_PASS = "你的SMTP授权码"       # 注意：不是邮箱登录密码！
MAIL_TO = ["收件人@qq.com"]       # 可多个
```

**非敏感参数放在 `config.py`：**

```python
INDICES = ["沪深300", "中证500", "科创50"]   # 监控指数，可增删
INDICES_WEIGHTS = {...}                     # 各指数权重
MAIN_RUN_HOUR = 18                          # 主跑时段
```

> 🔒 **安全说明**：
> - `local_config.py` 已在 `.gitignore` 中，**不会被提交**到仓库
> - 仓库里只保留模板 `local_config.example.py`（占位符，无真实凭据）
> - 缺少 `local_config.py` 时：**预览模式正常**，仅在发送邮件时提示"邮件配置不完整"

### 5. （可选）安装中文字体

图表内标签为英文，**HTML 中文由邮箱客户端渲染**，树莓派本机无需中文字体。
若你希望在树莓派上用浏览器打开 preview.html 显示中文，可装：

```bash
sudo apt install -y fonts-noto-cjk
```

### 6. 验证能取数、能出图

```bash
cd ~/stock
./run.sh --preview          # 第一次会联网拉全量历史（约几秒~几十秒）
ls -la preview.html         # 确认生成
```

浏览器打开 `preview.html` 确认图表与排版正常后，再进入定时任务配置。

---

## 二、cron 定时任务配置

### 定时规则说明

程序内置两档逻辑（见 `config.py` / `main.py`）：

- `MAIN_RUN_HOUR = 18`：**主跑时段**，无论信号如何，每天发 1 次
- 命中**告警状态**（`ALERT_STATUSES = ["低估","高估"]`）时，其他整点运行也发

因此安排 cron 在**早 / 中 / 晚 / 深夜各触发一次**（`08 12 18 23`），效果为：

| 当天状态 | 发信情况 |
|---|---|
| 无告警（正常/偏低/偏高） | 只在 **18:30** 发 1 次 |
| 有告警（低估→大额买 / 高估→卖出） | **08:30 / 12:30 / 18:30 / 23:30** 各发 1 次，共 4 次 |

### 安装 cron（两步）

**① 打开 crontab：**

```bash
crontab -e
```

**② 追加这一行：**

```cron
# 工作日 早8:30 / 午12:30 / 晚18:30 / 深夜23:30 各尝试一次
# 脚本判断：主跑时段(18点)必发；其余时刻仅低估/高估(告警)才发
30 8,12,18,23 * * 1-5  /home/pi/stock/run.sh >> /home/pi/stock/cron.log 2>&1
```

> 💡 若项目放在其它路径，把上面两处 `/home/pi/stock` 换成你的实际路径
> （`run.sh` 会自动 cd 到脚本所在目录，路径写对即可）。

> 时间都设为 **:30**（如 8:30）可避开整点高峰，具体时刻随意，
> 脚本只按「整点小时」判断是否发送。四个时刻的含义：

| cron 时刻 | 触发后脚本行为 |
|---|---|
| 08:30 | 有告警 → 发第 1 次；无告警 → 跳过 |
| 12:30 | 有告警 → 发第 2 次；无告警 → 跳过 |
| 18:30 | **主跑时段 → 无论是否告警都发**（平常日就靠这一次） |
| 23:30 | 有告警 → 发第 3~4 次；无告警 → 跳过 |

> ⚠️ **时区**：确保树莓派时区为 `Asia/Shanghai`，否则 cron 时刻会错位：
> `sudo timedatectl set-timezone Asia/Shanghai`
>
> ⚠️ **不要**用「每 X 分钟执行一次」的 cron——程序**没有按天去重**，
> 同一告警时段重复触发会重复发信。请使用上面固定的 4 个时刻。

### cron 常用运维命令

```bash
crontab -l          # 查看已装任务
crontab -e          # 编辑
tail -f ~/stock/cron.log     # 实时看 cron 输出
ls ~/stock/logs/             # 每次运行的详细日志
```

---

## 三、验证与故障排查

### 发一封测试邮件（强制发送模式）

```bash
cd ~/stock && .venv/bin/python main.py --send
```

> `--send` **无视**交易日 / 时段 / 告警规则，立即取数评估并发一封真实邮件，
> 用于验证 SMTP 授权码与邮件渲染。树莓派部署完成后建议先跑一次。

命令行三种模式速查：

| 命令 | 行为 |
|---|---|
| `python main.py` | 正常定时：主跑时段必发，其余时段仅告警发 |
| `python main.py --preview` | 生成本地 HTML，不发邮件（看版面） |
| `python main.py --send` | **强制立即发一封**（测试发送） |

### 常见问题

| 现象 | 排查 |
|---|---|
| 不发邮件 | 看 `logs/run_*.log`：非交易日 / 非主跑时段且无告警属正常 |
| `load config failed` | 从项目根运行（run.sh 已自动 cd）；`config.py` 语法错误时也会报此错 |
| 邮箱报错 535/认证失败 | `SMTP_PASS` 应为**授权码**而非登录密码；确认邮箱已开 SMTP |
| `邮件配置不完整` | 缺少 `local_config.py`：`cp local_config.example.py local_config.py` 并填写 |
| 某指数"无可用数据源" | 乐咕不支持该指数（如科创50 无 PB）属正常，会自动只用 PE+股息率 |
| 数据很久不更新 | 网络断了会读缓存并跳过；检查 `logs/` 与网络连通性 |

### 数据积累说明

- PE-TTM / PB：乐咕返回 **2005 年至今月频**，首次拉取即补满，3 年图立即可用
- 股息率：中证官网只给**最近 20 个交易日**，首次只有 20 个点；
  **每天运行 +1 个点**，运行约一年后补满 3 年曲线（期间自动用已有数据评分，不影响使用）

---

## 四、参数调优速查（config.py）

| 配置 | 位置 | 说明 |
|---|---|---|
| 监控指数 | `INDICES` | 增删指数名 |
| 各指数权重 | `INDICES_WEIGHTS` | PE-TTM/PB/股息率 各自占比（未列指数用 `COMPOSITE_WEIGHTS`） |
| 5 档阈值 | `SIGNAL_BANDS` | (下限, 上限, 状态, emoji, 动作)，改阈值或动作文字 |
| 动作短词/图标 | `ACTION_SHORT` / `ACTION_ICON` | 邮件标题与横幅图标 |
| 状态配色 | `STATUS_STYLE` | (背景色, 文字色)，改主题色 |
| 图表年数 | `HISTORY_YEARS_CHART` | 走势图展示窗口（默认 3） |
| 主跑时段 | `MAIN_RUN_HOUR` | 每天必发一次的整点 |
| 告警状态 | `ALERT_STATUSES` | 触发一天 4 次的状态 |

## 免责声明

本项目仅用于估值跟踪与个人学习，不构成任何投资建议。市场有风险，决策需谨慎。
