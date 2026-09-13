# -*- coding: utf-8 -*-
"""图表生成：matplotlib 出 PNG，供邮件内联嵌入。

图表清单（每个标的）
--------------------
  score  : 综合评分走势（10年评分实线 + 5年评分虚线 + 低估/高估阈值带）
  pct    : 五指标分位走势（PE/PB/PS/PCF/股息率 多线对比）
  bars   : 当前五指标分位柱状图（含配置权重标注）
  values : 指数估值绝对值走势（双轴：PE/PS/PCF 左轴，PB/股息率右轴）

数据来源
--------
  * 评分走势/分位走势 ← valuation_score 表（storage.load_scores）
  * 估值绝对值走势     ← kline 表指数行（storage.load_kline）

约定
----
图内文字一律用英文/符号，避免树莓派缺中文字体导致方框；
中文说明放在邮件 HTML 正文里。
"""

import argparse
import functools
import io
import logging
import os
import sys
import datetime as dt

import matplotlib
matplotlib.use("Agg")          # 无显示环境（树莓派/服务器）必需
import matplotlib.pyplot as plt      # noqa: E402
import matplotlib.dates as mdates    # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
if __package__ in (None, ""):          # 支持 python src/show_view/view_show.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from src.config import config        # noqa: E402
from src.storage import storage      # noqa: E402

log = logging.getLogger("view")

# =====================================================================
# 调色板
# =====================================================================
_C_LINE = "#3a7bd5"
_C_AREA = "#bcd4f5"
_C_GRID = "#e9e9ef"
_C_TXT = "#444"
_CHEAP = "#1e8e5a"
_DEAR = "#c94f4f"
_NEUTRAL = "#7d8ca0"
_WARN = "#a85a00"

# 五指标配色与标签（与 indicators.METRICS 对应）
METRIC_COLORS = {
    "pe_ttm": "#3a7bd5",        # 蓝
    "pb_mrq": "#1e8e5a",        # 绿
    "ps_ttm": "#a85a00",        # 橙
    "pcf_ncf_ttm": "#7d5bbe",   # 紫
    "div_yield": "#17a2b8",     # 青
}
METRIC_LABELS = {
    "pe_ttm": "PE-TTM", "pb_mrq": "PB", "ps_ttm": "PS",
    "pcf_ncf_ttm": "PCF", "div_yield": "Div",
}
# 图内用的中文标签（与网页「五指标卡」的标签保持一致）
METRIC_LABELS_ZH = {
    "pe_ttm": "PE-TTM", "pb_mrq": "PB", "ps_ttm": "PS-TTM",
    "pcf_ncf_ttm": "PCF", "div_yield": "股息率",
}
METRICS = tuple(METRIC_COLORS.keys())

# 评分表分位字段 → 指标名
SCORE_PCT_FIELD = {
    "pe_ttm": "pct_pe", "pb_mrq": "pct_pb", "ps_ttm": "pct_ps",
    "pcf_ncf_ttm": "pct_pcf", "div_yield": "pct_dividend",
}

# 信号阈值（与 config.SIGNAL_BANDS 关键档位对齐，仅用于图中参考线）
LOW_PCT, HIGH_PCT = 20, 85


# =====================================================================
# 通用绘图工具
# =====================================================================
# K 线图最多画多少根蜡烛（邮件图固定宽度，再密就糊成一片了）
KLINE_MAX_CANDLES = 260


def _status_color(pct: float) -> str:
    """分位 → 颜色（低=便宜绿，高=贵红）。"""
    if pct is None:
        return _NEUTRAL
    if pct < 20:
        return _CHEAP
    if pct < 40:
        return "#3a7bd5"
    if pct < 70:
        return _NEUTRAL
    if pct < 85:
        return _WARN
    return _DEAR


# =====================================================================
# 中文字体：图内文字用中文（与网页显示保持一致）
# ---------------------------------------------------------------------
# 树莓派上不一定装了中文字体，所以先探测；探测不到就退回英文，
# 免得整张图变成一排"豆腐块"方框。可用 DSH_CHART_LANG=en 强制英文。
# =====================================================================
_CJK_CANDIDATES = (
    "Noto Sans CJK SC", "Source Han Sans SC", "Noto Sans CJK JP",
    "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Microsoft YaHei",
    "PingFang SC", "Hiragino Sans GB", "Heiti SC", "SimHei",
    "Droid Sans Fallback", "Arial Unicode MS",
)


def _setup_cjk_font() -> bool:
    """挑一个可用的中文字体并设为默认；返回是否有中文字体。"""
    try:
        from matplotlib import font_manager as fm
        have = {f.name for f in fm.fontManager.ttflist}
    except Exception:                      # noqa: BLE001
        have = set()
    picked = next((n for n in _CJK_CANDIDATES if n in have), None)
    if picked:
        plt.rcParams["font.sans-serif"] = [picked, "DejaVu Sans"]
        plt.rcParams["font.family"] = "sans-serif"
    else:
        plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    # 负号用 ASCII，避免部分中文字体缺 U+2212 显示成方框
    plt.rcParams["axes.unicode_minus"] = False
    return bool(picked)


CJK_FONT = None
if os.environ.get("DSH_CHART_LANG", "").lower().startswith("en"):
    CJK_OK = False
else:
    CJK_OK = _setup_cjk_font()
log.info("图表中文字体：%s", "可用" if CJK_OK else "不可用，退回英文")


def _t(zh: str, en: str) -> str:
    """有中文字体就用中文，否则退回英文。"""
    return zh if CJK_OK else en


# =====================================================================
# 估值区间：与网页图表逐项对齐
# ---------------------------------------------------------------------
# 网页的走势图用 ECharts 的 visualMap.pieces 把**折线按区间分段上色**，
# 再在区间分界处画虚线（markLine），末端一个区间色徽标（endLabel）。
# 这里用 matplotlib 复刻同一套视觉效果：配色同样取自 SIGNAL_BANDS +
# STATUS_STYLE，所以设置页改了区间/配色，邮件图也跟着变。
# =====================================================================
def signal_bands() -> list:
    """当前估值区间 [(lo, hi, 状态, 颜色)]（颜色取自 STATUS_STYLE）。"""
    style = config.status_style()
    out = []
    for b in config.signal_bands():
        if not isinstance(b, (list, tuple)) or len(b) < 3:
            continue
        lo, hi, name = b[0], b[1], b[2]
        colors = style.get(name) or []
        out.append((lo, hi, name, colors[0] if colors else _NEUTRAL))
    return out


def band_color(v) -> str:
    """数值落在哪个区间 → 该区间配色（越界兜底到最近一档）。"""
    bands = signal_bands()
    if not bands or v is None:
        return _NEUTRAL
    for lo, hi, _n, c in bands:
        if lo <= v < hi:
            return c
    return bands[0][3] if v < bands[0][0] else bands[-1][3]


def _band_line(ax, dates, vals, lw: float = 1.8):
    """按估值区间给折线**分段上色**（对应网页的 visualMap.pieces）。

    matplotlib 的 LineCollection 只吃数字坐标，所以 x 要先转成日期数字，
    并把 x 轴显式设成日期轴（否则 DateFormatter 无从判断单位）。
    """
    from matplotlib.collections import LineCollection
    pts = [(d, v) for d, v in zip(dates, vals) if v is not None]
    if not pts:
        return
    ax.xaxis_date()
    xs = [mdates.date2num(d) for d, _ in pts]
    ys = [v for _, v in pts]
    if len(pts) == 1:
        ax.plot([xs[0]], [ys[0]], marker="o", ms=3.5,
                color=band_color(ys[0]), lw=lw)
    else:
        segs = [[(xs[i], ys[i]), (xs[i + 1], ys[i + 1])]
                for i in range(len(pts) - 1)]
        cols = [band_color((ys[i] + ys[i + 1]) / 2.0)
                for i in range(len(pts) - 1)]
        ax.add_collection(LineCollection(segs, colors=cols, linewidths=lw,
                                         capstyle="round", zorder=4))
    # LineCollection 不参与自动缩放，手动把数据范围交给坐标轴（已是日期数字）
    ax.update_datalim(list(zip(xs, ys)))
    ax.autoscale_view()


def _band_marklines(ax):
    """区间分界虚线 + 「状态 阈值」文字（对应网页的 markLine）。"""
    bands = signal_bands()
    if not bands:
        return
    x1 = ax.get_xlim()[1]
    for _lo, hi, name, c in bands[:-1]:
        ax.axhline(hi, color=c, ls="--", lw=0.9, alpha=0.55, zorder=2)
        ax.text(x1, hi, f"{name} {hi}", color=c, fontsize=7,
                va="bottom", ha="right", zorder=6)


def _end_badge(ax, dates, vals, fmt: str = "%.1f%%"):
    """末端值徽标：区间色圆角底 + 白字（对应网页的 endLabel）。"""
    last = next(((d, v) for d, v in reversed(list(zip(dates, vals)))
                 if v is not None), None)
    if not last:
        return
    ax.annotate(fmt % last[1], (last[0], last[1]), textcoords="offset points",
                xytext=(8, -2), fontsize=9, color="white", fontweight="bold",
                zorder=7, bbox=dict(boxstyle="round,pad=0.32",
                                    fc=band_color(last[1]), ec="none"))


def _style(ax):
    ax.set_facecolor("#fff")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#ccccd0")
    ax.spines["bottom"].set_color("#ccccd0")
    ax.tick_params(colors=_C_TXT, labelsize=8)
    ax.grid(True, color=_C_GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _fmt_xdate(ax, fig):
    """日期轴：按跨度自动选刻度格式（跨年显示年-月）。"""
    loc = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(loc)
    x0, x1 = ax.get_xlim()
    span = x1 - x0
    if span > 700:
        fmt = "%Y-%m"
    elif span > 366:
        fmt = "%y-%m"
    else:
        fmt = "%m-%d"
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    fig.autofmt_xdate(rotation=30)


def _to_png(fig) -> bytes:
    """把 figure 渲成 PNG 并**关闭它**（无论成功失败，避免 figure 泄漏）。

    matplotlib 的 Gcf.figs 是强引用，不 close 就不会释放（约 0.9MB/张）。
    出图函数在异常路径上（脏数据、字体缺失、savefig 失败）以前会永久泄漏 figure，
    长时间运行的 web 进程里这是持续增长的。
    """
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                    facecolor="#fff")
        buf.seek(0)
        return buf.read()
    finally:
        plt.close(fig)


def _chart_safe(fn):
    """装饰器：出图函数抛异常时，关掉它创建的所有 figure（防止泄漏）。

    matplotlib 的 Gcf.figs 持强引用，只有 plt.close() 才释放（约 0.9MB/张）。
    正常路径由 _to_png 关闭；这里兜住"绘图途中抛异常"的路径——脏数据（列里混进字符串）、
    字体缺失、savefig 失败都会走那条路，长期运行的 web 进程里会持续涨内存。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        before = set(plt.get_fignums())
        try:
            return fn(*args, **kwargs)
        except Exception:
            for num in set(plt.get_fignums()) - before:
                try:
                    plt.close(num)
                except Exception:               # noqa: BLE001
                    pass
            raise
    return wrapper


def _parse_dates(date_strs):
    return [dt.datetime.strptime(d, "%Y-%m-%d") for d in date_strs]


# =====================================================================
# 数据准备（从数据库读取）
# =====================================================================
def _years_ago(years: int) -> str:
    return (dt.datetime.now() - dt.timedelta(days=int(365.25 * years))
            ).strftime("%Y-%m-%d")


def load_score_rows(conn, code: str, years: int | None = None) -> list[dict]:
    """读取近 N 年的评分历史（走势图数据源）。"""
    years = years or config.get_int("HISTORY_YEARS_CHART", 5)
    return storage.load_scores(
        conn, code, start=_years_ago(years),
        fields=("date", "score", "score5", "pct_pe", "pct_pb", "pct_ps",
                "pct_pcf", "pct_dividend", "status"))


def load_value_rows(conn, code: str, years: int | None = None) -> list[dict]:
    """读取近 N 年的估值绝对值序列（指数行，来自方案A聚合）。"""
    years = years or config.get_int("HISTORY_YEARS_CHART", 5)
    return storage.load_kline(conn, code, start=_years_ago(years),
                             fields=("date",) + METRICS)


# =====================================================================
# 图 1：综合评分走势
# =====================================================================
@_chart_safe
def score_trend_chart(rows, period_txt: str = "last 3y") -> bytes:
    """综合评分走势：10年评分（实线）+ 5年评分（虚线）+ 阈值带 + 当前点。

    rows: [{date, score, score5, status}]
    """
    rows = [r for r in rows if r.get("score") is not None]
    if not rows:
        return b""
    dates = _parse_dates([r["date"] for r in rows])
    vals = [r["score"] for r in rows]
    vals5 = [r.get("score5") for r in rows]
    cur = vals[-1]
    cur_color = _status_color(cur)

    fig, ax = plt.subplots(figsize=(7.1, 2.9))
    _style(ax)

    ax.set_ylim(0, 100)
    # 10 年评分：按估值区间**分段上色**（对应网页 visualMap.pieces）
    _band_line(ax, dates, vals, lw=2.0)
    # 区间分界虚线：必须在折线之后画，它要用最终的 xlim 决定文字位置
    _band_marklines(ax)
    # 5 年评分：蓝色虚线（网页里也是 #3a7bd5 虚线）
    if any(v is not None for v in vals5):
        xs = [d for d, v in zip(dates, vals5) if v is not None]
        ys = [v for v in vals5 if v is not None]
        ax.plot(xs, ys, color="#3a7bd5", lw=1.3, ls="--", alpha=0.95,
                label=_t("评分5年", "score 5y"), zorder=3)
    # 图例：用一条区间色的线代表 10 年主序列（分段线本身不进图例）
    ax.plot([], [], color=band_color(cur), lw=2.0,
            label=_t("评分10年", "score 10y"))

    _end_badge(ax, dates, vals)
    ax.set_ylabel(_t("评分 %", "score %"), fontsize=9, color=_C_TXT)
    # 图内不画标题：标题由邮件 HTML / 网页渲染（见 report.py 的图标题行）
    ax.legend(loc="upper left", fontsize=7, frameon=False, ncol=2)
    ax.margins(x=0.01, y=0.06)
    _fmt_xdate(ax, fig)
    return _to_png(fig)


# =====================================================================
# 图 2：五指标分位走势
# =====================================================================
@_chart_safe
def metrics_percentile_chart(rows, period_txt: str = "last 3y") -> bytes:
    """五指标分位走势（多线）：直观对比各指标相对自身历史的贵/便宜。

    rows: [{date, pct_pe, pct_pb, pct_ps, pct_pcf, pct_dividend}]
    分位已统一为"越高越贵"（股息率已反向）。
    """
    if not rows:
        return b""
    dates = _parse_dates([r["date"] for r in rows])

    fig, ax = plt.subplots(figsize=(7.1, 2.9))
    _style(ax)
    ax.axhspan(0, LOW_PCT, color=_CHEAP, alpha=0.07)
    ax.axhspan(HIGH_PCT, 100, color=_DEAR, alpha=0.07)
    ax.axhline(LOW_PCT, color=_CHEAP, ls="--", lw=0.9, alpha=0.6)
    ax.axhline(HIGH_PCT, color=_DEAR, ls="--", lw=0.9, alpha=0.6)

    plotted = 0
    for m in METRICS:
        field = SCORE_PCT_FIELD[m]
        ys = [r.get(field) for r in rows]
        if not any(v is not None for v in ys):
            continue
        xs = [d for d, v in zip(dates, ys) if v is not None]
        vs = [v for v in ys if v is not None]
        ax.plot(xs, vs, color=METRIC_COLORS[m], lw=1.5,
                label=METRIC_LABELS[m], alpha=0.9)
        # 末端点标注
        ax.scatter([xs[-1]], [vs[-1]], color=METRIC_COLORS[m], s=22,
                   edgecolor="white", linewidth=0.8, zorder=5)
        ax.annotate(f"{vs[-1]:.0f}", (xs[-1], vs[-1]), textcoords="offset points",
                    xytext=(5, -1), fontsize=8, color=METRIC_COLORS[m],
                    fontweight="bold")
        plotted += 1

    if plotted == 0:
        plt.close(fig)
        return b""

    ax.set_ylim(0, 100)
    ax.set_ylabel(_t("分位 %", "percentile %"), fontsize=9, color=_C_TXT)
    # 图内不画标题：标题由邮件 HTML / 网页渲染（见 report.py 的图标题行）
    ax.legend(loc="upper left", fontsize=7, frameon=False,
              ncol=min(plotted, 5))
    ax.margins(x=0.02, y=0.06)
    _fmt_xdate(ax, fig)
    return _to_png(fig)


# =====================================================================
# 图 3：当前五指标分位柱状图（含权重）
# =====================================================================
@_chart_safe
def percentile_bar_chart(pcts: dict, weights: dict, score: float | None = None,
                         title: str = "Current percentile (low = cheap)") -> bytes:
    """当前五指标分位柱状图 + 综合评分参考线。

    pcts:    {"pe": 分位, "pb":..., "ps":..., "pcf":..., "dividend":...}
    weights: {"pe": 0.3, ...}（用于柱上标注权重）
    """
    key_order = ["pe", "pb", "ps", "pcf", "dividend"]
    labels, vals, colors, notes = [], [], [], []
    for k in key_order:
        v = pcts.get(k)
        if v is None:
            continue
        labels.append(k.upper())
        vals.append(v)
        colors.append(_status_color(v))
        w = weights.get(k)
        notes.append(f"w{w*100:.0f}%" if w else "")
    if not labels:
        return b""
    labels.append("SCORE")
    vals.append(score if score is not None else 0)
    colors.append(_status_color(score))
    notes.append("")

    fig, ax = plt.subplots(figsize=(7.1, 2.5))
    _style(ax)
    ax.axhline(LOW_PCT, color=_CHEAP, ls="--", lw=0.9, alpha=0.6)
    ax.axhline(HIGH_PCT, color=_DEAR, ls="--", lw=0.9, alpha=0.6)
    bars = ax.bar(labels, vals, color=colors, width=0.56,
                  edgecolor="white", linewidth=1.5)
    for b, v, note in zip(bars, vals, notes):
        x = b.get_x() + b.get_width() / 2
        ax.text(x, v + 2, f"{v:.0f}", ha="center", fontsize=9,
                color=_C_TXT, fontweight="bold")
        if note:
            ax.text(x, 3, note, ha="center", fontsize=7, color="#ffffff",
                    fontweight="bold")
    ax.set_ylim(0, 110)
    ax.set_ylabel(_t("分位 %", "percentile %"), fontsize=9, color=_C_TXT)
    # 图内不画标题：标题由邮件 HTML / 网页渲染（见 report.py 的图标题行）
    return _to_png(fig)


# =====================================================================
# 图 4：指数估值绝对值走势（双轴）
# =====================================================================
@_chart_safe
def index_value_chart(rows, period_txt: str = "last 3y") -> bytes:
    """指数估值绝对值走势（方案A聚合结果）。

    左轴：PE-TTM / PS-TTM / PCF（量级较大）
    右轴：PB / 股息率（量级较小）
    rows: [{date, pe_ttm, pb_mrq, ps_ttm, pcf_ncf_ttm, div_yield}]
    """
    if not rows:
        return b""
    dates = _parse_dates([r["date"] for r in rows])

    fig, ax = plt.subplots(figsize=(7.1, 2.9))
    _style(ax)
    left_metrics = ["pe_ttm", "ps_ttm", "pcf_ncf_ttm"]
    right_metrics = ["pb_mrq", "div_yield"]

    drawn = 0
    for m in left_metrics:
        ys = [r.get(m) for r in rows]
        if not any(v is not None for v in ys):
            continue
        xs = [d for d, v in zip(dates, ys) if v is not None]
        vs = [v for v in ys if v is not None]
        ax.plot(xs, vs, color=METRIC_COLORS[m], lw=1.5,
                label=METRIC_LABELS[m])
        ax.annotate(f"{vs[-1]:.1f}", (xs[-1], vs[-1]), textcoords="offset points",
                    xytext=(5, 0), fontsize=8, color=METRIC_COLORS[m],
                    fontweight="bold")
        drawn += 1
    ax.set_ylabel("ratio (x)", fontsize=9, color=_C_TXT)

    ax2 = ax.twinx()
    ax2.spines["top"].set_visible(False)
    ax2.tick_params(colors=_C_TXT, labelsize=8)
    for m in right_metrics:
        ys = [r.get(m) for r in rows]
        if not any(v is not None for v in ys):
            continue
        xs = [d for d, v in zip(dates, ys) if v is not None]
        vs = [v for v in ys if v is not None]
        ax2.plot(xs, vs, color=METRIC_COLORS[m], lw=1.3, ls="--",
                 label=METRIC_LABELS[m], alpha=0.85)
        ax2.annotate(f"{vs[-1]:.2f}", (xs[-1], vs[-1]),
                     textcoords="offset points", xytext=(5, 0), fontsize=8,
                     color=METRIC_COLORS[m], fontweight="bold")
        drawn += 1
    ax2.set_ylabel("PB / Div%", fontsize=9, color=_C_TXT)

    if drawn == 0:
        plt.close(fig)
        return b""

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=7,
              frameon=False, ncol=min(len(l1 + l2), 5))
    # 图内不画标题：标题由邮件 HTML / 网页渲染（见 report.py 的图标题行）
    ax.margins(x=0.02, y=0.12)
    _fmt_xdate(ax, fig)
    return _to_png(fig)


# =====================================================================
# 图 5：单指标走势（保留，用于个股或单指标放大）
# =====================================================================
@_chart_safe
def metric_line_chart(rows, title: str, field: str = "pe_ttm",
                      fmt: str = "%.2f") -> bytes:
    """单指标绝对值走势折线（含均值参考线与当前点标注）。

    rows: [{date, <field>: value, ...}]
    """
    pts = [(r["date"], r.get(field)) for r in rows if r.get(field) is not None]
    if not pts:
        return b""
    dates = _parse_dates([d for d, _ in pts])
    vals = [v for _, v in pts]
    cur = vals[-1]
    avg = sum(vals) / len(vals)
    cur_color = _status_color(_pct_of(sorted(vals), cur))

    fig, ax = plt.subplots(figsize=(7.1, 2.6))
    _style(ax)
    ax.axhline(avg, color=_NEUTRAL, ls="--", lw=0.9, alpha=0.7)
    ax.text(dates[-1], avg, _t("  均值", "  mean"), color=_NEUTRAL, fontsize=7,
            va="bottom", ha="right")
    ax.fill_between(dates, vals, min(vals) - (max(vals) - min(vals)) * 0.1,
                    color=_C_AREA, alpha=0.5)
    ax.plot(dates, vals, color=METRIC_COLORS.get(field, _C_LINE), lw=1.7)
    ax.scatter([dates[-1]], [cur], color=cur_color, zorder=5, s=42,
               edgecolor="white", linewidth=1.2)
    ax.annotate(fmt % cur, (dates[-1], cur), textcoords="offset points",
                xytext=(8, -2), fontsize=9, color="white", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc=cur_color, ec="none"))
    # 图内不画标题：标题由邮件 HTML / 网页渲染（见 report.py 的图标题行）
    ax.margins(x=0.01, y=0.14)
    _fmt_xdate(ax, fig)
    return _to_png(fig)


# =====================================================================
# 图 5：K 线走势（收盘价折线 + 区间高低点，与网页「K线走势」对应）
# =====================================================================
@_chart_safe
def kline_chart(rows, period_txt: str = "last 3y") -> bytes:
    """K 线（蜡烛图）+ 成交量，与网页「K线走势」图一一对应。

    rows: [{date, open, high, low, close, volume}]
    涨红跌绿（A股习惯），下方 20% 高度放成交量柱。
    """
    pts = [r for r in rows if r.get("close") is not None]
    if not pts:
        return b""
    # 邮件里的图是固定宽度、不能缩放，蜡烛太多会细成一条线。
    # 只画最近 KLINE_MAX_CANDLES 根（约一年）—— 这个上限与其它走势图不同步，
    # 所以邮件标题行会写清"最近 N 个交易日"，避免被误读成 x 轴没跟配置走。
    if len(pts) > KLINE_MAX_CANDLES:
        pts = pts[-KLINE_MAX_CANDLES:]
    dates = _parse_dates([r["date"] for r in pts])
    # matplotlib 的日线宽度是按天算的，用 0.62 天留出间隙
    width = 0.62
    up, down = _DEAR, _CHEAP          # 红涨绿跌

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(7.4, 3.4), sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1], "hspace": 0.12})
    _style(ax)
    _style(axv)

    for d, r in zip(dates, pts):
        o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
        o = c if o is None else o
        h = max(h, o, c) if h is not None else max(o, c)
        l = min(l, o, c) if l is not None else min(o, c)
        col = up if c >= o else down
        ax.vlines(d, l, h, color=col, lw=0.7, alpha=0.9)
        ax.add_patch(plt.Rectangle(
            (mdates.date2num(d) - width / 2, min(o, c)), width, abs(c - o) or 1e-6,
            facecolor=col, edgecolor=col, linewidth=0.4))

    # 最新收盘价横线 + 在**左侧** y 轴标出具体数值（与网页的 markLine 一致）。
    # 最后一个蜡烛右侧原本还有一个同样的数值气泡，和这条标线重复，
    # 而且会挤到图右边缘 —— 已去掉，只保留标线 + 左侧数值。
    last_row = pts[-1]
    lc = last_row.get("close")
    if lc is not None:
        lc_col = up if (last_row.get("open") is None
                        or lc >= last_row["open"]) else down
        ax.axhline(lc, color=lc_col, ls="--", lw=1.0, alpha=0.9, zorder=5)
        ax.annotate("%.2f" % lc, (0, lc), xycoords=("axes fraction", "data"),
                    textcoords="offset points", xytext=(3, -1),
                    fontsize=8, color="white", fontweight="bold", zorder=8,
                    va="center", ha="left",
                    bbox=dict(boxstyle="round,pad=0.25", fc=lc_col, ec="none"))

    # 成交量柱（颜色跟随当根 K 线）
    vols = [r.get("volume") for r in pts]
    if any(v is not None for v in vols):
        axv.bar(dates, [v or 0 for v in vols], width=width,
                color=[up if (r.get("close") or 0) >= (r.get("open") or 0) else down
                       for r in pts], alpha=0.75)

    # 图内**不画标题**：标题统一由邮件 HTML 渲染（与网页标的信息页一致）
    axv.set_ylabel(_t("成交量", "vol"), fontsize=8, color=_C_TXT)
    ax.margins(x=0.01, y=0.12)
    _fmt_xdate(axv, fig)
    return _to_png(fig)


# =====================================================================
# 图 6：单指标分位走势（与网页每张「XX 估值分位」图对应）
# =====================================================================
@_chart_safe
def metric_pct_trend_chart(rows, metric: str, period_txt: str = "last 3y") -> bytes:
    """单个指标的历史分位走势（带低估/高估区间底纹 + 末值标注）。

    rows: [{date, pct_pe, pct_pb, ...}]；分位已统一为"越高越贵"。
    """
    field = SCORE_PCT_FIELD[metric]
    pts = [(r["date"], r.get(field)) for r in rows if r.get(field) is not None]
    if not pts:
        return b""
    dates = _parse_dates([d for d, _ in pts])
    vals = [v for _, v in pts]
    cur = vals[-1]
    color = METRIC_COLORS.get(metric, _C_LINE)
    band = _status_color(cur)

    fig, ax = plt.subplots(figsize=(7.1, 2.4))
    _style(ax)
    ax.set_ylim(0, 100)
    # 与网页「XX 估值分位」图完全一致：折线按区间分段上色 + 分界虚线 + 末端徽标
    _band_line(ax, dates, vals, lw=1.6)
    _band_marklines(ax)
    _end_badge(ax, dates, vals)
    # 图内不画标题：标题由邮件 HTML / 网页渲染（见 report.py 的图标题行）
    ax.margins(x=0.01)
    _fmt_xdate(ax, fig)
    return _to_png(fig)


def _pct_of(sorted_vals, x: float) -> float:
    """x 在已排序列表中的分位（0~100，与 indicators.percentile 口径一致）。"""
    import bisect
    n = len(sorted_vals)
    if n == 0 or x is None:
        return 50.0
    if n == 1:
        return 50.0
    if x <= sorted_vals[0]:
        return 0.0
    if x >= sorted_vals[-1]:
        return 100.0
    hi = bisect.bisect_left(sorted_vals, x)
    lo = hi - 1
    if sorted_vals[lo] == sorted_vals[hi]:
        rank = (lo + hi) / 2.0
    else:
        rank = lo + (x - sorted_vals[lo]) / (sorted_vals[hi] - sorted_vals[lo])
    return rank / (n - 1) * 100


# =====================================================================
# 便捷入口：一次生成某标的的全部图表
# =====================================================================
def build_charts(conn, code: str, ktype: str = "index",
                 years: int | None = None,
                 with_values: bool = True) -> dict:
    """读取数据库并生成图表，返回 {图名: PNG bytes}。

    图名（与网页标的信息页的图表一一对应）：
      kline / score / pct_pe / pct_pb / pct_ps / pct_pcf / pct_dividend
      以及旧的 pct（五线合并）/ bars / values（保留给命令行出图用）
    数据缺失时对应项为 b""（调用方按需跳过）。
    """
    # 邮件走势图的窗口 = HISTORY_YEARS_CHART（"走势图展示窗口"，默认 5 年）。
    # 网页 K 线图也用同一个配置，改一处两边同步。
    years = years or config.get_int("HISTORY_YEARS_CHART", 5)
    period = _t(f"近 {years} 年", f"last {years}y")
    out = {}

    # K 线（蜡烛图 + 成交量）——与网页「K线走势」对应
    krows = storage.load_kline(conn, code, start=_years_ago(years),
                               fields=("date", "open", "high", "low", "close",
                                       "volume", "pct_chg"))
    out["kline"] = kline_chart(krows, period)

    score_rows = load_score_rows(conn, code, years)
    out["score"] = score_trend_chart(score_rows, period)
    out["pct"] = metrics_percentile_chart(score_rows, period)
    # 每个指标单独一张分位走势——与网页那 5 张「XX 估值分位」图对应
    for m in METRICS:
        out[f"pct_{m.replace('_ttm', '').replace('_mrq', '')
                     .replace('_ncf', '').replace('div_yield', 'dividend')}"] = \
            metric_pct_trend_chart(score_rows, m, period)

    # 当前分位柱状图：取最新一条评分记录
    latest = storage.latest_score(conn, code)
    if latest:
        pcts = {k: latest.get(SCORE_PCT_FIELD[m]) for m, k in zip(
            METRICS, ["pe", "pb", "ps", "pcf", "dividend"])}
        weights = (config.target(code) or {}).get("weights") or \
            config.get_json("COMPOSITE_WEIGHTS", {}) or {}
        out["bars"] = percentile_bar_chart(pcts, weights, latest.get("score"))

    if with_values:
        out["values"] = index_value_chart(load_value_rows(conn, code, years), period)

    return out


def save_charts(conn, code: str, outdir: str, ktype: str = "index",
                years: int | None = None, with_values: bool = True) -> list[str]:
    """生成图表并落盘为 PNG 文件，返回写出的文件路径列表（调试/预览用）。"""
    os.makedirs(outdir, exist_ok=True)
    charts = build_charts(conn, code, ktype=ktype, years=years,
                          with_values=with_values)
    paths = []
    for name, png in charts.items():
        if not png:
            log.warning("%s 的 %s 图无数据，跳过", code, name)
            continue
        path = os.path.join(outdir, f"{code.replace('.', '_')}_{name}.png")
        with open(path, "wb") as f:
            f.write(png)
        paths.append(path)
    return paths


# =====================================================================
# 命令行：手动出图预览（不发邮件）
# =====================================================================
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="生成估值评分图表（PNG 预览）")
    p.add_argument("--code", default=None, help="标的代码，如 sh.000300；省略则用配置里全部标的")
    p.add_argument("--outdir", default="logs/charts", help="PNG 输出目录")
    p.add_argument("--years", type=int, default=None, help="展示窗口（年），默认取 HISTORY_YEARS_CHART")
    p.add_argument("--db", default=None, help="数据库路径，默认 config.DB_PATH")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    targets = ([t for t in config.targets(only_enabled=False) if t["code"] == args.code]
               if args.code else config.targets(only_enabled=False))
    if not targets:
        log.error("没有匹配的标的：%s", args.code)
        return 1

    config.use_db(args.db)
    conn = storage.get_conn()
    try:
        total = 0
        for t in targets:
            for path in save_charts(conn, t["code"], args.outdir,
                                    ktype=t.get("ktype", "index"),
                                    years=args.years,
                                    with_values=(t.get("ktype") == "index")):
                log.info("已生成 %s", path)
                total += 1
        log.info("共生成 %d 个图（目录 %s）", total, args.outdir)
    finally:
        conn.close()
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
