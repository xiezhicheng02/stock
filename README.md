# 股票数据采集系统

基于 baostock 的 A 股/ETF/指数数据采集与展示系统。
FastAPI 提供 Web 界面，APScheduler 定时拉取日 K、季频财务、宏观经济等数据，本地 SQLite 落库。

---

## 目录

- [环境要求](#环境要求)
- [安装](#安装)
- [启动](#启动)
- [停止](#停止)
- [定时任务](#定时任务)
- [目录结构](#目录结构)
- [手动触发数据拉取](#手动触发数据拉取)

---

## 环境要求

- Python 3.10+
- Windows / Linux
- 能访问外网（数据源 baostock）

## 安装

```bash
cd D:\project\PycharmProjects\stock

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate    # Linux

# 安装依赖
pip install -r requirements.txt
```

首次启动时会自动建表（`data/stock.db`），无需手动初始化。

## 启动

```bash
# 方式一：双击脚本
script\start_web.bat

# 方式二：指定端口
script\start_web.bat 8080

# 方式三：命令行
.venv\Scripts\python -m src.web.app
```

启动后访问 <http://localhost:8000/>。

启动时自动：
1. 建表（`data/stock.db`）
2. 启动定时任务（日频 / 周频 / kline 补缺口）
3. 首次启动自动写入三个指数的基本信息（上证指数 / 沪深300 / 中证红利）

## 停止

```bash
# 方式一：停止脚本
script\stop_web.bat            # 默认停 8000 端口
script\stop_web.bat 8080       # 指定端口

# 方式二：前台运行时按 Ctrl+C
```

服务停止时，定时任务会自动关闭。

---

## 定时任务

| 任务 | 时间 | 说明 |
|---|---|---|
| **日频任务** `daily_task` | 周一~周五 18:00 | 拉全市场 A 股 + ETF + 三个指数日 K、复权因子；补证券基本信息与行业分类 |
| **kline 补缺口** `kline_backfill_task` | 周六 20:00 | 逐只检查历史 K 线完整性，补缺口（前复权） |
| **周频任务** `week_task` | 周日 10:00 | 拉季频财务（盈利能力/营运/成长/偿债/现金流/杜邦/业绩快报/业绩预告）+ 宏观经济（存贷款利率/准备金率/货币供应量）+ 交易日历 |

三个指数：`sh.000001`（上证综合指数）、`sh.000300`（沪深300）、`sh.000922`（中证红利）。

---

## 目录结构

```
stock/
├── src/
│   ├── config/config.py            # 系统配置（DB/SMTP/Web 端口/管理员）
│   ├── model/                       # pydantic 数据对象（Kline/StockBasic/...）
│   ├── fetch_data/data_fetcher.py   # baostock 会话（每个 model 一个方法）
│   ├── storage/
│   │   ├── schema.py               # 建表 SQL
│   │   └── storage.py               # Storage 类（save/load/ensure_schema）
│   ├── schedule/
│   │   ├── daily_task.py            # 日频任务
│   │   ├── week_task.py             # 周频任务
│   │   ├── kline_backfill_task.py   # kline 补缺口
│   │   └── APScheduler.py          # 调度入口
│   └── web/
│       ├── app.py                   # FastAPI 入口（生命周期 + 路由挂载）
│       └── routers/dashboard.py     # 首页接口（行情/统计/手动触发）
├── script/
│   ├── start_web.bat                # 启动
│   └── stop_web.bat                 # 停止
├── data/stock.db                    # SQLite（自动创建）
└── requirements.txt
```

---

## 手动触发数据拉取

### 命令行

```bash
# 日频任务（拉当天全市场 K 线）
.venv\Scripts\python -m src.schedule.daily_task

# 周频任务（季频 + 宏观）
.venv\Scripts\python -m src.schedule.week_task

# kline 补缺口
.venv\Scripts\python -m src.schedule.kline_backfill_task

# 调度器常驻（含定时任务）
.venv\Scripts\python -m src.schedule.APScheduler
```

### Web 页面

首页右上角有三个按钮，分别手动触发上述三个任务（后台异步执行）。

---

## 免责声明

本项目仅用于数据采集与个人学习，不构成任何投资建议。市场有风险，决策需谨慎。
