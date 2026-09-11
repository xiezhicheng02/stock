# -*- coding: utf-8 -*-
"""图表生成：matplotlib 出 PNG，供邮件内联嵌入。

为避免树莓派缺中文字体导致中文变方框，图内标签一律用英文/符号；
中文说明放在邮件 HTML 正文里。
"""

import io
import datetime as dt

import matplotlib
matplotlib.use("Agg")  # 无显示环境（树莓派/服务器）必需
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402

# 统一调色板
_C_LINE = "#3a7bd5"        # 折线蓝
_C_AREA = "#bcd4f5"       # 折线下方填充浅蓝
_C_GRID = "#e9e9ef"
_C_TXT = "#444"
_CHEAP = "#2e8b57"
_DEAR = "#d9534f"
_NEUTRAL = "#808080"


def _status_color(pct: float) -> str:
    if pct < 20:
        return _CHEAP
    if pct < 40:
        return "#3a7bd5"
    if pct < 70:
        return _NEUTRAL
    if pct < 85:
        return "#a85a00"
    return _DEAR


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
    """统一日期坐标轴：按数据跨度自动选择刻度格式，确保年份正确显示。
    matplotlib 日期轴数值以天为单位（1.0 = 1 天），可从 xlim 推出跨度。"""
    loc = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(loc)

    x0, x1 = ax.get_xlim()
    span_days = x1 - x0
    if span_days > 700:      # 跨 > ~2 年 → "2023-09"
        fmt = "%Y-%m"
    elif span_days > 366:    # 跨 1~2 年 → "23-09"（紧凑）
        fmt = "%y-%m"
    else:                    # 1 年内 → "09-15" 月-日
        fmt = "%m-%d"
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    fig.autofmt_xdate(rotation=30)


def _to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                facecolor="#fff")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _pct_of(sorted_vals, x: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if x <= sorted_vals[0]:
        return 0.0
    if x >= sorted_vals[-1]:
        return 100.0
    import bisect
    hi = bisect.bisect_left(sorted_vals, x)
    lo = hi - 1
    if sorted_vals[lo] == sorted_vals[hi]:
        return (lo + hi) / (2 * n) * 100
    rank = lo + (x - sorted_vals[lo]) / (sorted_vals[hi] - sorted_vals[lo])
    return rank / n * 100


def metric_line_chart(series, cur_val: float, all_vals_for_pct,
                      title: str, fmt: str = "%.2f") -> bytes:
    """指标走势折线：渐变填充 + 低估/高估阈值线 + 当前点标注。
    供 PE-TTM / PB / 股息率等各指标复用。
    series: list[(date_str, value)]（近 N 年窗口）。all_vals_for_pct: 算当前百分位着色。
    """
    if not series:
        return b""
    dates = [dt.datetime.strptime(d, "%Y-%m-%d") for d, _ in series]
    vals = [v for _, v in series]
    sv = sorted(all_vals_for_pct) if all_vals_for_pct else sorted(vals)
    low_val = sv[int(0.2 * (len(sv) - 1))]
    high_val = sv[int(0.8 * (len(sv) - 1))]
    cur_color = _status_color(_pct_of(sv, cur_val))

    fig, ax = plt.subplots(figsize=(6.6, 2.9))
    _style(ax)
    # 低估/高估横线
    ax.axhline(low_val, color=_CHEAP, ls="--", lw=0.9, alpha=0.7)
    ax.axhline(high_val, color=_DEAR, ls="--", lw=0.9, alpha=0.7)
    ax.text(dates[-1], low_val, "  low 20%", color=_CHEAP, fontsize=7,
            va="bottom", ha="right")
    ax.text(dates[-1], high_val, "  high 80%", color=_DEAR, fontsize=7,
            va="bottom", ha="right")
    # 折线 + 填充
    ax.fill_between(dates, vals, min(vals) - (max(vals) - min(vals)) * 0.1,
                    color=_C_AREA, alpha=0.55)
    ax.plot(dates, vals, color=_C_LINE, lw=1.6)
    # 当前点
    ax.scatter([dates[-1]], [cur_val], color=cur_color, zorder=5, s=42,
               edgecolor="white", linewidth=1.2)
    ax.annotate(fmt % cur_val, (dates[-1], cur_val),
                textcoords="offset points", xytext=(8, -2), fontsize=9,
                color="white", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc=cur_color, ec="none"))
    ax.set_title(title, fontsize=11, color=_C_TXT, loc="left", pad=6)
    ax.margins(x=0.01, y=0.12)
    _fmt_xdate(ax, fig)
    return _to_png(fig)


def score_line_chart(series, cur_score: float, period_txt: str = "last 1y") -> bytes:
    """综合估值分位走势折线：底部带日期坐标轴，直观看时间与综合评分关系。
    series: list[(date_str, score)]。cur_score: 当前综合分位（着色+标注）。
    """
    if not series:
        return b""
    dates = [dt.datetime.strptime(d, "%Y-%m-%d") for d, _ in series]
    vals = [v for _, v in series]
    cur_color = _status_color(cur_score)

    fig, ax = plt.subplots(figsize=(6.6, 2.9))
    _style(ax)
    # 低估/高估阈值横线
    ax.axhline(20, color=_CHEAP, ls="--", lw=0.9, alpha=0.7)
    ax.axhline(85, color=_DEAR, ls="--", lw=0.9, alpha=0.7)
    ax.text(dates[-1], 20, "  low 20%", color=_CHEAP, fontsize=7,
            va="bottom", ha="right")
    ax.text(dates[-1], 85, "  high 80%", color=_DEAR, fontsize=7,
            va="bottom", ha="right")
    # 折线 + 填充
    ax.fill_between(dates, vals, 0, color=_C_AREA, alpha=0.45)
    ax.plot(dates, vals, color=_C_LINE, lw=1.6)
    # 当前点
    ax.scatter([dates[-1]], [cur_score], color=cur_color, zorder=5, s=42,
               edgecolor="white", linewidth=1.2)
    ax.annotate(f"{cur_score:.1f}%", (dates[-1], cur_score),
                textcoords="offset points", xytext=(8, -2), fontsize=9,
                color="white", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc=cur_color, ec="none"))
    ax.set_ylim(0, 100)
    ax.set_ylabel("score %", fontsize=9, color=_C_TXT)
    ax.set_title(f"Composite score ({period_txt})", fontsize=11,
                 color=_C_TXT, loc="left", pad=6)
    ax.margins(x=0.01, y=0.05)
    _fmt_xdate(ax, fig)
    return _to_png(fig)


def histogram(values, cur_val: float) -> bytes:
    """PE-TTM 10年分布直方图 + 当前值竖线标注。"""
    if not values:
        return b""
    cur_color = _status_color(_pct_of(sorted(values), cur_val))
    fig, ax = plt.subplots(figsize=(6.6, 2.5))
    _style(ax)
    ax.hist(values, bins=30, color=_C_AREA, edgecolor="white", linewidth=0.8)
    ax.axvline(cur_val, color=cur_color, lw=2)
    ax.annotate(f"now {cur_val:.2f}", (cur_val, ax.get_ylim()[1] * 0.9),
                textcoords="offset points", xytext=(8, 0), fontsize=9,
                color="white", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.3", fc=cur_color, ec="none"))
    ax.set_title("PE-TTM distribution (10y)", fontsize=11, color=_C_TXT,
                 loc="left", pad=6)
    ax.margins(y=0.15)
    return _to_png(fig)


def percentile_bars(pe_pct, pb_pct, div_pct, score) -> bytes:
    """指标分位 + 综合分位柱状图（低=便宜，股息率已做 100− 反向）。
    pb_pct / div_pct 可为 None（该指数无此数据，自动跳过）。"""
    labels, vals = ["PE"], [pe_pct]
    if pb_pct is not None:
        labels.append("PB"); vals.append(pb_pct)
    if div_pct is not None:
        labels.append("Div*"); vals.append(100 - div_pct)
    labels.append("Score"); vals.append(score)
    colors = [_status_color(v) for v in vals]
    fig, ax = plt.subplots(figsize=(6.6, 2.5))
    _style(ax)
    ax.axhline(20, color=_CHEAP, ls="--", lw=0.9, alpha=0.6)
    ax.axhline(85, color=_DEAR, ls="--", lw=0.9, alpha=0.6)
    bars = ax.bar(labels, vals, color=colors, width=0.55,
                  edgecolor="white", linewidth=1.5)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 2, f"{v:.0f}",
                ha="center", fontsize=9, color=_C_TXT, fontweight="bold")
    ax.set_ylim(0, 100)
    ax.set_ylabel("percentile %", fontsize=9, color=_C_TXT)
    ax.set_title("Valuation percentile (low=cheap)", fontsize=11,
                 color=_C_TXT, loc="left", pad=6)
    return _to_png(fig)
