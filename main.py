# -*- coding: utf-8 -*-
"""指数估值每日推送主脚本（支持多指数）。

流程：抓数据 → 算分位+趋势 → 出信号 → 生成含图表与行动建议的 HTML 邮件 → 发送。
设计为 cron 拉起、跑完即退，不留常驻进程，适合树莓派 3B+。

手动验证：
    python3 main.py
非交易日会打印日志并退出，不发邮件。
"""

import logging
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import akshare as ak
import pandas as pd

import charts
import config
import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("csival")


# =====================================================================
# 1. 交易日历与判断
# =====================================================================
_CALENDAR = None  # sorted list of 'YYYYMMDD'


def trade_calendar():
    """新浪交易日历，缓存避免一天 4 次重复拉取。"""
    global _CALENDAR
    if _CALENDAR is None:
        cal = ak.tool_trade_date_hist_sina()
        _CALENDAR = sorted(str(d).replace("-", "") for d in cal["trade_date"])
    return _CALENDAR


def is_trading_day(today: str) -> bool:
    """today: 'YYYYMMDD'。"""
    try:
        return today in trade_calendar()
    except Exception as e:
        log.warning("交易日历获取失败，按工作日兜底判断: %s", e)
        return datetime.strptime(today, "%Y%m%d").weekday() < 5


def latest_available_trade_date(now: datetime):
    """当前时刻应当已可拿到的最新交易日(YYYYMMDD)。
    收盘(>=17点)且今日为交易日 → 今日；否则取最近的一个过去交易日。"""
    cal = trade_calendar()
    today = now.strftime("%Y%m%d")
    if now.hour >= 17 and today in cal:
        return today
    past = [d for d in cal if d < today]
    return past[-1] if past else None


# =====================================================================
# 2. 数据抓取 + 增量入库
# =====================================================================
# akshare 1.18 已移除旧接口 index_value_hist_funddb，改用以下替代：
#   PE-TTM(滚动市盈率) -> ak.stock_index_pe_lg() 乐咕乐股, 取「滚动市盈率」列,
#                       月频全历史(2005-今)
#             (乐咕不支持的指数降级用中证官网 stock_zh_index_value_csindex 的市盈率2)
#   市净率 -> ak.stock_index_pb_lg()  乐咕乐股, 月频全历史(2005-今)
#             (乐咕不支持的指数无 PB，由 config.PE_ONLY_INDICES 声明并自适应评分)
#   股息率 -> ak.stock_zh_index_value_csindex() 中证官网, 近20交易日(日频)
# 中证官网股息率只有最近 20 个交易日，随每日入库逐步累积变长；
# 因此"10年分位"初期实际基于已有历史，运行越久越准确（其余指标不受影响）。
# 注：DB 内部以"市盈率"为 key 存储 PE-TTM 序列（历史兼容，勿改）。
_CSINDEX_CODE = {  # 中文指数名 -> 中证官网 6 位代码（股息率/降级PE数据源用）
    "上证50": "000016", "沪深300": "000300", "上证380": "000009",
    "中证500": "000905", "上证180": "000010",
    "深证红利": "399324", "深证100": "399330", "中证1000": "000852",
    "上证红利": "000015", "中证100": "000903", "中证800": "000906",
    "创业板指": "399006", "创业板50": "399673",
    "科创50": "000688",
}


def fetch_from_akshare(symbol: str, indicator: str):
    """按指标从 akshare 拉历史，返回 list[(date 'YYYY-MM-DD', value)] 升序。

    适配 akshare 1.18：旧接口 index_value_hist_funddb 已移除。
    """
    if indicator == "市盈率":
        try:
            # 优先乐咕（历史长、字段全）
            df = ak.stock_index_pe_lg(symbol=symbol)
            df = df[["日期", "滚动市盈率"]].copy()
        except KeyError:
            # 乐咕不支持的指数（如科创50）降级：中证官网市盈率2
            code = _CSINDEX_CODE.get(symbol)
            if not code:
                raise ValueError(f"暂无 {symbol} 的市盈率数据源")
            df = ak.stock_zh_index_value_csindex(symbol=code)
            df = df[["日期", "市盈率2"]].copy()
    elif indicator == "市净率":
        if symbol in config.PE_ONLY_INDICES:
            raise ValueError(f"{symbol} 在乐咕无市净率数据（PE_ONLY_INDICES 声明）")
        df = ak.stock_index_pb_lg(symbol=symbol)
        df = df[["日期", "市净率"]].copy()
    else:  # 股息率
        code = _CSINDEX_CODE.get(symbol)
        if not code:
            raise ValueError(
                f"暂无 {symbol} 的中证官网代码，无法获取股息率；"
                f"请在 _CSINDEX_CODE 中补充"
            )
        df = ak.stock_zh_index_value_csindex(symbol=code)
        df = df[["日期", "股息率2"]].copy()
    df.columns = ["date", "value"]
    # 统一日期为 YYYY-MM-DD，兼容字符串/日期/时间戳
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df["value"] = df["value"].astype(float)
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return [(r["date"], r["value"]) for _, r in df.iterrows()]


def ensure_data(conn, symbol: str, indicator: str):
    """带本地缓存的取数：仅当 DB 最新日期 < 应有最新交易日时才联网全量拉取并增量入库。
    返回 list[(date, value)] 升序（从 DB 读）。
    PE_ONLY 指数（config.PE_ONLY_INDICES）的市净率直接返回 []，不尝试联网。
    """
    # PE_ONLY 指数无市净率数据源，直接返回空（不打扰日志）。
    if indicator == "市净率" and symbol in config.PE_ONLY_INDICES:
        return []

    target = latest_available_trade_date(datetime.now())  # 'YYYYMMDD'
    db_latest = storage.latest_db_date(conn, symbol, indicator)  # 'YYYY-MM-DD'
    db_latest_compact = db_latest.replace("-", "") if db_latest else None

    if target is None:
        log.warning("%s %s: 无交易日历，兜底联网全量拉取。", symbol, indicator)
    elif db_latest_compact is not None and db_latest_compact >= target:
        log.info("%s %s: 本地已最新(DB=%s)，读缓存不联网。", symbol, indicator, db_latest)
        return storage.load_series(conn, symbol, indicator)

    log.info("%s %s: 联网全量拉取(target=%s, DB=%s)...", symbol, indicator, target, db_latest)
    series = fetch_from_akshare(symbol, indicator)
    new, changed = storage.upsert_series(conn, symbol, indicator, series)
    storage.set_fetch_date(conn, symbol, indicator, datetime.now().strftime("%Y-%m-%d"))
    log.info("%s %s: 入库完成，总%d行，新增%d，更新%d。",
             symbol, indicator, len(series), new, changed)
    return storage.load_series(conn, symbol, indicator)


def window(series, years: int):
    """取近 N 年的【纯值】列表（升序）—— 供 percentile / 直方图使用。"""
    cutoff = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    return [v for (d, v) in series if d >= cutoff]


def window_pairs(series, years: int):
    """取近 N 年的 (date, value) 元组列表（升序）—— 供折线图使用。"""
    cutoff = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    return [(d, v) for (d, v) in series if d >= cutoff]


def ff_value(series_map: dict, date: str):
    """前向填充：series_map = {date: value}（已按日期排序）。
    返回 <= date 的最近一个 value；找不到返回 None。
    用于不同频率指标对齐（如股息率日频 vs PB 月频）。"""
    if not series_map:
        return None
    best = None
    for d, v in series_map.items():
        if d > date:
            break
        best = v
    return best


# =====================================================================
# 3. 百分位（纯标准库，线性插值，与 numpy 默认一致）
# =====================================================================
def percentile(values, x: float) -> float:
    import bisect
    s = sorted(values)
    n = len(s)
    if n == 0:
        return float("nan")
    if x <= s[0]:
        return 0.0
    if x >= s[-1]:
        return 100.0
    hi = bisect.bisect_left(s, x)
    lo = hi - 1
    if s[lo] == s[hi]:
        return (lo + hi) / (2 * n) * 100
    rank = lo + (x - s[lo]) / (s[hi] - s[lo])
    return rank / n * 100


def trend_arrow(cur: float, prev: float) -> str:
    """与上一交易日对比的趋势箭头（纯文本，带符号）。"""
    if prev is None or cur is None:
        return ""
    delta = cur - prev
    if delta > 1e-9:
        return f"▲ {delta:+.2f}"
    if delta < -1e-9:
        return f"▼ {delta:+.2f}"
    return "─ 0.00"


def score_delta_txt(cur: float, prev: float) -> str:
    """评分较前日变化（标题用紧凑写法）：↑1.2 / ↓0.8 / 空。"""
    if prev is None or cur is None:
        return ""
    d = cur - prev
    if d > 0.05:
        return f"↑{d:.1f}"
    if d < -0.05:
        return f"↓{abs(d):.1f}"
    return "→0"


def delta_html(cur: float, prev: float, invert: bool = False) -> str:
    """表格用：指标值较前日变化。

    颜色按"变便宜/变贵"上色：
      invert=False（PE-TTM/PB）：数值下降=变便宜→绿；上升=变贵→红
      invert=True （股息率）   ：数值上升=变便宜→绿；下降=变贵→红
    """
    if prev is None or cur is None:
        return '<span style="color:#c4ccd8;">-</span>'
    d = cur - prev
    if abs(d) < 1e-9:
        return '<span style="color:#b6bfcc;">─ 0.00</span>'
    cheaper = (d < 0) if not invert else (d > 0)
    col = "#1e8e5a" if cheaper else "#c0392b"
    arrow = "▲" if d > 0 else "▼"
    return f'<span style="color:{col};">{arrow} {abs(d):.2f}</span>'

# =====================================================================
# 4. 信号
# =====================================================================
def signal_for(score: float):
    """返回 (状态, emoji, 动作)。score 为综合估值分位。"""
    for lo, hi, status, emoji, action in config.SIGNAL_BANDS:
        if lo <= score < hi:
            return status, emoji, action
    return "未知", "❓", "无"


def weights_for(symbol: str) -> dict:
    """返回该指数使用的权重；未单独配置时回退全局 COMPOSITE_WEIGHTS。"""
    return config.INDICES_WEIGHTS.get(symbol, config.COMPOSITE_WEIGHTS)


def composite_score(pe_pct, pb_pct=None, div_pct=None, has_pb=True, has_div=True,
                    symbol: str = ""):
    """综合估值分位（低=便宜，与单指标方向一致）。

    = Σ(权重 × 该指标分位) / 可用权重之和
      PE-TTM、PB  直接用分位（低=便宜）
      股息率  用 100−分位（高股息率=便宜，反向）
    某指标缺失（None / has_*=False）时自动从权重里剔除再归一化，
    因此 PE_ONLY 指数（无 PB）也能正确评分。
    symbol 决定使用哪个指数的权重（INDICES_WEIGHTS）。
    """
    w = weights_for(symbol)
    total, acc = 0.0, 0.0
    if pe_pct is not None:
        total += w["pe"]; acc += w["pe"] * pe_pct
    if pb_pct is not None and has_pb and w.get("pb", 0) > 0:
        total += w["pb"]; acc += w["pb"] * pb_pct
    if div_pct is not None and has_div and w.get("dividend", 0) > 0:
        total += w["dividend"]; acc += w["dividend"] * (100 - div_pct)
    if total <= 0:
        return 50.0  # 无任何可用指标时的中性值
    return acc / total


def signal_color(status: str) -> str:
    """状态主色（横幅背景、强调色）。可配于 config.STATUS_STYLE。"""
    return config.STATUS_STYLE.get(status, ("#999", "#fff"))[0]


def on_color(status: str) -> str:
    """状态色上的前景文字色（保证对比度），config.STATUS_STYLE 第二项。"""
    return config.STATUS_STYLE.get(status, ("#999", "#fff"))[1]


def pct_color(pct: float) -> str:
    """百分位数 → 颜色（低分位=便宜绿，高分位=贵红），用于表格分位着色。"""
    if pct < 20:
        return "#2e8b57"
    if pct < 40:
        return "#3a7bd5"
    if pct < 70:
        return "#8a94a6"
    if pct < 85:
        return "#a85a00"
    return "#d9534f"


# =====================================================================
# 5. 渲染
# =====================================================================
def is_alert_result(r: dict) -> bool:
    return r["status"] in config.ALERT_STATUSES


def index_section(ctx: dict) -> str:
    """单个指数的邮件区块。

    布局：
      [状态色渐变横幅] 指数名+emoji+状态(+重点提醒徽章) | 建议动作胶囊 | 综合评分
      [数据日期 · 权重说明]（浅灰小字）
      [综合评分走势图]（最上）
      [各指标近1年折线图]
      [指标表格：当前值|10年分位|5年分位，分位数着色]
      [小字说明]
    """
    color = signal_color(ctx["status"])
    alert = is_alert_result(ctx)

    # ---------- 指标行数据（PE-TTM 恒有；PB/股息率按可用性）----------
    # (名称, 当前值, 前值, 10年分位, 5年分位, 是否反向[股息率], 单位)
    rows = [("PE-TTM", ctx["pe_cur"], ctx.get("pe_prev"),
             ctx["pe_pct10"], ctx["pe_pct5"], False, "")]
    if ctx.get("has_pb", True):
        rows.append(("PB", ctx["pb_cur"], ctx.get("pb_prev"),
                     ctx["pb_pct10"], ctx["pb_pct5"], False, ""))
    if ctx.get("has_div", True):
        rows.append(("股息率", ctx["div_cur"], ctx.get("div_prev"),
                     ctx["div_pct10"], ctx["div_pct5"], True, "%"))

    # ---------- 表格行：指标 | 当前 | 较前日 | 10年分位 | 5年分位 ----------
    def tr(name, cur, prev, p10, p5, invert, unit):
        b = "padding:7px 10px;"
        cur_txt = (f'<b>{cur:.2f}{unit}</b>' if cur is not None
                   else '<span style="color:#bbb;">-</span>')

        def cell(p, strong=False):
            if p is None:
                return f'<td style="text-align:right;{b}color:#bbb;">-</td>'
            style = f'color:{pct_color(p)};font-weight:{700 if strong else 400};'
            return f'<td style="text-align:right;{b}{style}">{p:.0f}%</td>'

        return (
            f'<tr><td style="{b}color:#55606e;">{name}</td>'
            f'<td style="text-align:right;{b}">{cur_txt}</td>'
            f'<td style="text-align:right;{b}font-size:12px;">'
            f'{delta_html(cur, prev, invert)}</td>'
            f'{cell(p10)}{cell(p5)}</tr>'
        )

    # 综合评分行：当前评分 + 较前日变化（涨幅=变贵→红，跌幅=变便宜→绿）
    score5 = ctx.get("score5")
    score_prev = ctx.get("score_prev")
    if score_prev is None:
        score_delta_cell = '<span style="color:#c4ccd8;">-</span>'
    else:
        d = ctx["score"] - score_prev
        dcol = "#c0392b" if d > 0.05 else ("#1e8e5a" if d < -0.05 else "#b6bfcc")
        arrow = "▲" if d > 0.05 else ("▼" if d < -0.05 else "─")
        score_delta_cell = (f'<span style="color:{dcol};font-weight:700;">'
                            f'{arrow} {abs(d):.1f}</span>')
    comp_row = (
        '<tr style="background:#f4f7fc;border-top:2px solid #e2e8f0;">'
        f'<td style="padding:9px 10px;font-weight:700;color:#243340;">综合评分</td>'
        f'<td style="text-align:right;padding:9px 10px;"><b style="color:{color};'
        f'font-size:16px;">{ctx["score"]:.1f}%</b></td>'
        f'<td style="text-align:right;padding:9px 10px;font-size:12px;">'
        f'{score_delta_cell}</td>'
        # 10年分位列：综合评分本身即 10 年口径，此处显示构成说明
        f'<td style="text-align:right;padding:9px 10px;color:#9aa3b2;'
        f'font-size:11px;">(加权)</td>'
        # 5年分位列
        + (f'<td style="text-align:right;padding:9px 10px;color:{pct_color(score5)};'
           f'font-weight:700;">{score5:.1f}%</td>' if score5 is not None else
           '<td style="text-align:right;padding:9px 10px;color:#bbb;">-</td>')
        + '</tr>'
    )

    table = (
        '<table cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;font-size:13px;width:100%;'
        'margin-top:12px;border-radius:10px;overflow:hidden;'
        'border:1px solid #edf0f4;">'
        '<thead><tr style="background:#f7f9fc;color:#9aa3b2;font-size:12px;">'
        '<th style="text-align:left;padding:8px 10px;">指标</th>'
        '<th style="text-align:right;padding:8px 10px;">当前</th>'
        '<th style="text-align:right;padding:8px 10px;">较前日</th>'
        '<th style="text-align:right;padding:8px 10px;">10年分位</th>'
        '<th style="text-align:right;padding:8px 10px;">5年分位</th>'
        '</tr></thead><tbody>'
        + "".join(tr(*r) for r in rows)
        + comp_row
        + '</tbody></table>'
    )

    # ---------- 权重说明（按指数实际权重）----------
    _w = ctx.get("weights") or config.COMPOSITE_WEIGHTS
    has_pb, has_div = ctx.get("has_pb", True), ctx.get("has_div", True)
    w_parts = [f"PE-TTM {_w['pe']*100:.0f}%"]
    if has_pb and _w.get("pb", 0) > 0:
        w_parts.append(f"PB {_w['pb']*100:.0f}%")
    if has_div and _w.get("dividend", 0) > 0:
        w_parts.append(f"股息率 {_w['dividend']*100:.0f}%")
    _weights_txt = " · ".join(w_parts) + ("（股息率反向：高=便宜）" if has_div else "")
    _formula_note = ("分位说明：0%=历史最低便宜　100%=历史最高贵；"
                     "综合评分 = 加权分位，缺失指标自动剔除归一。")

    # ---------- 图表（综合评分走势放最上 → 各指标折线）----------
    charts = ctx.get("charts", {})
    _img_style = ("display:block;width:100%;border-radius:8px;"
                  "margin-top:10px;border:1px solid #eef1f5;")
    _img = lambda key: (
        f'<img src="cid:{charts[key]}" style="{_img_style}" alt="{key}" />'
        if charts.get(key) else "")
    img_score = _img("score")
    img_line = _img("line")
    img_pb = _img("pb")
    img_div = _img("div")

    # ---------- 头部状态色横幅：指数名/状态 + 评分；底部一条行动建议 ----------
    icon = config.ACTION_ICON.get(ctx["status"], "👉")
    fg = on_color(ctx["status"])                     # 横幅前景文字色（config 配）
    shade = "" if fg != "#ffffff" else "text-shadow:0 1px 2px rgba(0,0,0,.18);"
    # 半透明胶囊/叠加层统一用白色透明（前景为白时）
    badge = ('<span style="background:rgba(255,255,255,.24);color:#fff;'
             'font-size:10px;padding:1px 8px;border-radius:9px;'
             'margin-left:6px;vertical-align:middle;">🔔 重点提醒</span>'
             if alert else "")
    # 评分较前日变化：涨=变贵(红) / 跌=变便宜(绿)，白底胶囊保证在彩色横幅上清晰
    _sp = ctx.get("score_prev")
    if _sp is None:
        score_delta_badge = ""
    else:
        _d = ctx["score"] - _sp
        if _d > 0.05:
            _arrow, _txt, _dcol = "▲", f"+{_d:.1f}", "#c0392b"   # 评分上升=估值变贵
        elif _d < -0.05:
            _arrow, _txt, _dcol = "▼", f"{_d:.1f}", "#1e8e5a"    # 评分下降=估值变便宜
        else:
            _arrow, _txt, _dcol = "─", "0.0", "#7a8699"
        score_delta_badge = (
            f'<div style="margin-top:3px;font-size:11px;font-weight:800;'
            f'background:#ffffff;color:{_dcol};display:inline-block;'
            f'padding:1px 8px;border-radius:9px;'
            f'box-shadow:0 1px 3px rgba(0,0,0,.12);">'
            f'{_arrow} {_txt} 较前日</div>'
        )
    header = (
        f'<div style="background:linear-gradient(135deg,{color} 0%,{color}dd 100%);'
        f'color:{fg};border-radius:10px;overflow:hidden;{shade}">'
        # 上段：指数名+状态（左）｜ 评分（右）
        f'<div style="display:flex;align-items:center;justify-content:space-between;'
        f'padding:12px 16px 10px;">'
        f'<div style="display:flex;align-items:center;gap:8px;min-width:0;">'
        f'<span style="font-size:15px;">{ctx["emoji"]}</span>'
        f'<span style="font-size:16px;font-weight:800;letter-spacing:.5px;">'
        f'{ctx["symbol"]}</span>'
        f'<span style="font-size:12px;font-weight:700;'
        f'background:rgba(255,255,255,.22);padding:1px 9px;border-radius:10px;">'
        f'{ctx["status"]}</span>{badge}</div>'
        f'<div style="text-align:right;flex-shrink:0;">'
        f'<div style="font-size:10px;opacity:.85;letter-spacing:1px;">综合评分</div>'
        f'<div style="font-size:22px;font-weight:800;line-height:1.15;">'
        f'{ctx["score"]:.1f}%</div>{score_delta_badge}</div></div>'
        # 下段：行动建议（整行通栏，动态图标）
        f'<div style="background:rgba(255,255,255,.14);padding:7px 16px;'
        f'display:flex;align-items:center;gap:8px;">'
        f'<span style="font-size:15px;">{icon}</span>'
        f'<span style="font-size:14px;font-weight:700;letter-spacing:.3px;">'
        f'{ctx["action"]}</span></div></div>'
    )

    # 元信息行（日期 · 权重），紧贴横幅下方
    meta_line = (
        f'<div style="display:flex;align-items:center;justify-content:space-between;'
        f'padding:8px 8px 2px;font-size:11px;color:#9aa3b2;">'
        f'<span>数据日期 {ctx["date"]}</span>'
        f'<span>{_weights_txt}</span></div>'
    )

    return (
        f'<div style="margin-bottom:16px;background:#fff;border-radius:14px;'
        f'padding:12px;box-shadow:0 3px 12px rgba(0,0,0,.06);'
        f'border:1px solid {color}33;">'
        f'{header}'
        f'{meta_line}'
        f'{img_score}{img_line}{img_pb}{img_div}'
        f'<div style="padding:0 6px;">{table}'
        f'<p style="color:#aab3c2;font-size:11px;margin:8px 2px 2px;line-height:1.5;">'
        f'{_formula_note}</p></div>'
        f'</div>'
    )



def build_html(sections: list, results: list, subject: str = "") -> str:
    """组装完整 HTML。

    首行放置与邮件标题一致的文本（subject），这样微信/邮件列表的预览摘要
    （通常截取正文开头文本）无需点开即可看到核心信号。
    """
    if not subject:
        subject = build_subject(results)
    has_alert = any(is_alert_result(r) for r in results)

    # 首行：标题（与邮件主题一致），状态色圆点/前缀配色点缀
    if has_alert:
        head_style = ("background:#c94f4f;color:#fff;")
    else:
        head_style = ("background:#fff;color:#243340;border:1px solid #e3e6ea;")
    head = (
        f'<div style="{head_style}border-radius:10px;padding:12px 16px;'
        f'margin-bottom:12px;box-shadow:0 2px 8px rgba(0,0,0,.05);">'
        f'<div style="font-size:15px;font-weight:800;line-height:1.5;">{subject}</div>'
        f'<div style="font-size:11px;opacity:.7;margin-top:2px;">指数估值信号 · 详见下方卡片</div>'
        f'</div>'
    )
    return f"""
    <html><head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;color:#243340;margin:0;padding:24px 12px;background:#eef1f4;">
      <div style="max-width:600px;margin:0 auto;">
        {head}
        {''.join(sections)}
        <p style="color:#aabbcc;font-size:11px;text-align:center;margin:20px 0 0;line-height:1.6;">
          本邮件由树莓派定时任务自动生成，仅作估值参考，不构成投资建议。
        </p>
      </div>
    </body></html>
    """


def build_subject(results: list) -> str:
    """标题体现关键信息：状态+分数+较前日变化+动作短词；告警加前缀与标记。"""
    has_alert = any(is_alert_result(r) for r in results)
    prefix = config.ALERT_PREFIX if has_alert else ""
    parts = []
    for r in results:
        mark = config.ALERT_MARK if is_alert_result(r) else ""
        action_short = config.ACTION_SHORT.get(r["status"], "")
        delta = score_delta_txt(r["score"], r.get("score_prev"))
        parts.append(
            f'{r["symbol"]}{r["emoji"]}{r["status"]}'
            f'{r["score"]:.0f}%{delta}({action_short}){mark}'
        )
    return prefix + "｜".join(parts)


# =====================================================================
# 6. 邮件
# =====================================================================
def send_mail(subject: str, html: str, images: list = None):
    # 校验邮件配置（敏感信息在 local_config.py，缺失时给出明确指引）
    missing = [k for k, v in (("SMTP_USER", config.SMTP_USER),
                              ("SMTP_PASS", config.SMTP_PASS)) if not v]
    if not config.MAIL_TO:
        missing.append("MAIL_TO")
    if missing:
        raise RuntimeError(
            f"邮件配置不完整（缺少 {', '.join(missing)}）："
            f"请复制 local_config.example.py 为 local_config.py 并填写邮箱与授权码"
        )

    # related：HTML 与内联图片(图表)同属一组
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = config.MAIL_FROM
    msg["To"] = ", ".join(config.MAIL_TO)
    msg.attach(MIMEText(html, "html", "utf-8"))

    # 内联图片：Content-ID 与 HTML 里 cid: 对应
    for cid, png in (images or []):
        sub = MIMEImage(png, "png")
        sub.add_header("Content-ID", f"<{cid}>")
        sub.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        msg.attach(sub)

    if config.SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        server.starttls()
    try:
        server.login(config.SMTP_USER, config.SMTP_PASS)
        server.sendmail(config.MAIL_FROM, config.MAIL_TO, msg.as_string())
        log.info("邮件已发送 → %s  标题: %s", config.MAIL_TO, subject)
    finally:
        server.quit()


# =====================================================================
# 7. 单指数计算
# =====================================================================
def evaluate(conn, symbol: str) -> dict:
    pe = ensure_data(conn, symbol, "市盈率")
    pb = ensure_data(conn, symbol, "市净率")   # PE_ONLY 指数为 []
    div = ensure_data(conn, symbol, "股息率")
    has_pb = len(pb) > 0
    has_div = len(div) > 0
    log.info("%s: 本地 PE %d / PB %d / 股息率 %d 条", symbol, len(pe), len(pb), len(div))
    if not pe:
        raise ValueError(f"{symbol} 无任何市盈率数据")

    pe_cur, pe_prev = pe[-1][1], (pe[-2][1] if len(pe) >= 2 else None)
    pb_cur, pb_prev = (pb[-1][1], (pb[-2][1] if len(pb) >= 2 else None)) if has_pb else (None, None)
    div_cur, div_prev = (div[-1][1], (div[-2][1] if len(div) >= 2 else None)) if has_div else (None, None)
    data_date = pe[-1][0]

    pe10, pe5 = window(pe, 10), window(pe, 5)
    pe3y = window_pairs(pe, config.HISTORY_YEARS_CHART)  # (date,value)，供折线图
    pb10 = pb5 = []
    div10 = div5 = []
    if has_pb:
        pb10, pb5 = window(pb, 10), window(pb, 5)
    if has_div:
        div10, div5 = window(div, 10), window(div, 5)

    pe_pct10 = percentile(pe10, pe_cur)
    pe_pct5 = percentile(pe5, pe_cur)
    pb_pct10 = percentile(pb10, pb_cur) if has_pb else None
    pb_pct5 = percentile(pb5, pb_cur) if has_pb else None
    div_pct10 = percentile(div10, div_cur) if has_div else None
    div_pct5 = percentile(div5, div_cur) if has_div else None

    # 综合估值分位（缺指标自动归一化权重）
    score = composite_score(pe_pct10, pb_pct10, div_pct10, has_pb, has_div, symbol)
    score5 = composite_score(pe_pct5, pb_pct5, div_pct5, has_pb, has_div, symbol)

    # 上一交易日各分位（用于趋势）
    prev_ok = pe_prev is not None and (pb_prev is not None or not has_pb) \
        and (div_prev is not None or not has_div)
    if prev_ok:
        pe_pct10_prev = percentile(pe10, pe_prev)
        pb_pct10_prev = percentile(pb10, pb_prev) if has_pb else None
        div_pct10_prev = percentile(div10, div_prev) if has_div else None
        score_prev = composite_score(pe_pct10_prev, pb_pct10_prev, div_pct10_prev,
                                     has_pb, has_div, symbol)
    else:
        pe_pct10_prev = pb_pct10_prev = div_pct10_prev = score_prev = None

    status, emoji, action = signal_for(score)

    # 综合分位 N年走势：以 PE 日期为主轴，PB/股息率用前向填充对齐（
    # 不同指标频率不同：PE/PB 月频、股息率日频），供 score_line_chart。
    pb_map = dict(pb)
    div_map = dict(div)
    scoreN = []
    for d, pe_v in pe3y:
        pb_v = ff_value(pb_map, d) if has_pb else None
        div_v = ff_value(div_map, d) if has_div else None
        scoreN.append((d, composite_score(
            percentile(pe10, pe_v),
            percentile(pb10, pb_v) if pb_v is not None else None,
            percentile(div10, div_v) if div_v is not None else None,
            pb_v is not None,
            div_v is not None,
            symbol,
        )))

    pb_txt = f"PB=%.2f(%.0f%%)" % (pb_cur, pb_pct10) if has_pb else "PB=无数据"
    div_txt = f"股息=%.2f%%(%.0f%%)" % (div_cur, div_pct10) if has_div else "股息=无数据"
    log.info("%s %s 数据日=%s PE-TTM=%.2f(%.0f%%) %s %s 综合=%.1f%% → %s/%s",
             symbol, emoji, data_date, pe_cur, pe_pct10, pb_txt, div_txt,
             score, status, action)

    return {
        "symbol": symbol, "date": data_date,
        "pe_cur": pe_cur, "pe_prev": pe_prev,
        "pb_cur": pb_cur, "pb_prev": pb_prev,
        "div_cur": div_cur, "div_prev": div_prev,
        "has_pb": has_pb, "has_div": has_div,
        "pe_pct10": pe_pct10, "pe_pct10_prev": pe_pct10_prev, "pe_pct5": pe_pct5,
        "pb_pct10": pb_pct10, "pb_pct10_prev": pb_pct10_prev, "pb_pct5": pb_pct5,
        "div_pct10": div_pct10, "div_pct10_prev": div_pct10_prev, "div_pct5": div_pct5,
        "score": score, "score_prev": score_prev, "score5": score5,
        "status": status, "emoji": emoji, "action": action,
        "weights": weights_for(symbol),
        # 各指标近 N年 (date,value)，供每指标一张折线图
        "pe3y": pe3y,
        "pb3y": window_pairs(pb, config.HISTORY_YEARS_CHART) if has_pb else [],
        "div3y": window_pairs(div, config.HISTORY_YEARS_CHART) if has_div else [],
        "score3y": scoreN,  # 供综合分位 N年折线图（放最上）
        # 各指标全量纯值（折线图 low/high 分位线与当前点着色用）
        "pe_all": [v for _, v in pe],
        "pb_all": [v for _, v in pb] if has_pb else [],
        "div_all": [v for _, v in div] if has_div else [],
    }


def gen_charts(result: dict, idx: int) -> list:
    """为单个指数生成图表：综合评分走势(最上) + 每个可用指标各一张折线。
    注入 cid 到 result['charts']，返回 [(cid, png)]。"""
    images = []
    cids = {}
    prefix = f"img{idx}"
    y_txt = f"last {config.HISTORY_YEARS_CHART}y"

    specs = [
        ("score", lambda: charts.score_line_chart(
            result.get("score3y", []), result["score"], y_txt)),
        ("line", lambda: charts.metric_line_chart(
            result.get("pe3y", []), result["pe_cur"], result.get("pe_all", []),
            f"PE-TTM ({y_txt})", "%.2f")),
    ]
    if result.get("has_pb"):
        specs.append(("pb", lambda: charts.metric_line_chart(
            result.get("pb3y", []), result["pb_cur"], result.get("pb_all", []),
            f"PB ({y_txt})", "%.2f")))
    if result.get("has_div"):
        specs.append(("div", lambda: charts.metric_line_chart(
            result.get("div3y", []), result["div_cur"], result.get("div_all", []),
            f"Div yield ({y_txt})", "%.2f%%")))

    try:
        for key, fn in specs:
            png = fn()
            if png:
                cid = f"{prefix}_{key}"
                cids[key] = cid
                images.append((cid, png))
    except Exception as e:
        log.warning("%s 图表生成失败(降级为无图): %s", result["symbol"], e)
    result["charts"] = cids
    return images


# =====================================================================
# 主流程
# =====================================================================
def main(argv=None):
    """运行主流程。

    参数：
      --preview [path]  预览模式：生成含内嵌 base64 图片的 HTML 文件到本地，
                        保存到 path（默认 ./preview.html），绝不发邮件。
                        用于随时查看版面效果。
      --send            强制发送模式：忽略交易日/时段/告警规则，立即评估并
                        发一封邮件。用于测试 SMTP 与邮件渲染（树莓派部署后验证）。
      （无参数）        正常模式：按时段(MAIN_RUN_HOUR)+告警规则决定是否发邮件。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    preview_path = None
    force_send = "--send" in argv
    if "--preview" in argv:
        i = argv.index("--preview")
        preview_path = argv[i + 1] if i + 1 < len(argv) else "preview.html"
        if preview_path.startswith("--"):
            preview_path = "preview.html"

    today = datetime.now().strftime("%Y%m%d")
    now_hour = datetime.now().hour

    # 非交易日跳过（仅正常模式生效；--send 测试模式不拦，方便随时验证发送）
    if (not preview_path and not force_send
            and config.SKIP_NON_TRADING_DAY and not is_trading_day(today)):
        log.info("今日(%s)非交易日，跳过。", today)
        return

    conn = storage.init_db(config.DB_PATH)
    results = []
    try:
        for sym in config.INDICES:
            try:
                results.append(evaluate(conn, sym))
            except Exception as e:
                log.exception("%s 评估失败，跳过该指数: %s", sym, e)
    finally:
        conn.close()

    if not results:
        log.warning("所有指数评估均失败，本次不发邮件。")
        return

    # 先生成图表 → 注入 cid 到 result['charts']，再渲染 HTML（cid 必须先就位）
    images = []
    for i, r in enumerate(results):
        images.extend(gen_charts(r, i))
    sections = [index_section(r) for r in results]
    subject = build_subject(results)
    html = build_html(sections, results, subject)

    if preview_path:
        # 预览模式：把 cid 图片转 base64 内嵌，单文件即可浏览器打开
        import base64
        for cid, png in images:
            html = html.replace(f'cid:{cid}',
                                f'data:image/png;base64,{base64.b64encode(png).decode()}')
        with open(preview_path, "w", encoding="utf-8") as f:
            f.write(html)
        log.info("已生成预览 HTML（未发送邮件）: %s", preview_path)
        log.info("标题预览: %s", subject)
        return

    if force_send:
        # 强制发送模式：跳过时段/告警判断，立即发送（用于测试发送链路）。
        send_mail(subject, html, images)
        return

    has_alert = any(is_alert_result(r) for r in results)
    is_main = (now_hour == config.MAIN_RUN_HOUR)

    # 主跑时段无条件发；其余时段仅告警才发
    if not is_main and not has_alert:
        log.info("非主跑时段(%d点)且无告警信号，本次不发邮件。", now_hour)
        return

    send_mail(subject, html, images)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("脚本执行失败: %s", e)
        sys.exit(1)
