# -*- coding: utf-8 -*-
"""报告渲染：把库里的评分/估值数据渲染成邮件 HTML（含四张图）。

数据来源
--------
  * valuation_score  评分、5年评分、五指标分位（10年/5年双口径）、状态、动作
  * kline（指数行）   PE/PB/PS/PCF/股息率的绝对值（方案A：成分股聚合后写回）
  * view_show        四张图：评分走势 / 五指标分位走势 / 当前分位柱状 / 估值绝对值

对外接口
--------
  build_report(conn, codes=None, years=None) -> {subject, html, images, items, ...}
  save_preview(conn, path=None, ...)         -> 生成自包含 HTML（图片转 base64，不发邮件）

设计沿用旧版邮件版式（用户已确认过的方案C配色）：
  ① 状态色横幅：左 = 名称+状态胶囊，右 = 综合评分 + 较前日变化，底部 = 建议动作
  ② 元信息行：数据日期 · 权重说明
  ③ 四张图（评分走势在最上）
  ④ 指标表：指标 | 当前 | 较前日 | 10年分位 | 5年分位，末行综合评分
邮件正文里的中文说明放在 HTML 里；图内文字一律英文（树莓派可能没有中文字体）。
"""

import base64
import colorsys
import html
import logging
import os
from datetime import datetime

from src.config import config
from src.show_view import view_show
from src.storage import storage

log = logging.getLogger("report")

# 五指标定义：(库字段, 显示名, 10年分位字段, 5年分位字段, 反向[越高越便宜], 单位, 权重键)
METRIC_SPECS = (
    ("pe_ttm",      "PE-TTM", "pct_pe",       "pct5_pe",       False, "",  "pe"),
    ("pb_mrq",      "PB",     "pct_pb",       "pct5_pb",       False, "",  "pb"),
    ("ps_ttm",      "PS-TTM", "pct_ps",       "pct5_ps",       False, "",  "ps"),
    ("pcf_ncf_ttm", "PCF",    "pct_pcf",      "pct5_pcf",      False, "",  "pcf"),
    ("div_yield",   "股息率", "pct_dividend", "pct5_dividend", True,  "%", "dividend"),
)

# 指标权重键 → 展示名（缺失提示用）
METRIC_LABELS = {"pe": "PE", "pb": "PB", "ps": "PS", "pcf": "PCF",
                 "dividend": "股息率"}

# 图 key → cid 后缀（顺序即邮件里的展示顺序）
# 卡片样式：与网页 .card / .card-title 对齐（val_type 见 style.css）
CARD_STYLE = ("background:#fff;border:1px solid #e6e8ef;border-radius:12px;"
              "padding:14px 16px 16px;margin-bottom:16px;"
              "box-shadow:0 1px 3px rgba(30,40,70,.04);")
CARD_TITLE_STYLE = "margin:0 0 10px;font-size:14px;font-weight:600;color:#2f3542;"


def _panel(title: str, body: str, right: str = "") -> str:
    """卡片**内部**的小面板（浅底 + 小标题），用于基本信息 / 指标与权重。

    外层已经是一张"整只标的"的大卡片，内部再用浅底分组，
    层次清楚但不会像多张独立卡片那样占掉大量纵向间距。
    """
    head = (f'<div style="display:flex;justify-content:space-between;'
            f'align-items:center;margin-bottom:8px;">'
            f'<span style="font-size:13px;font-weight:600;color:#2f3542;">'
            f'{title}</span>'
            f'<span style="font-size:12px;color:#8b93a5;">{right}</span></div>')
    return (f'<div style="background:#fafbfd;border:1px solid #eef1f5;'
            f'border-radius:10px;padding:11px 13px 12px;margin-bottom:12px;">'
            f'{head}{body}</div>')


def _card(title_html: str, body: str) -> str:
    """一张卡片 = 标题 + 内容（网页里每个区块都是这样一张卡）。"""
    head = (f'<div style="{CARD_TITLE_STYLE}">{title_html}</div>'
            if title_html else "")
    return f'<div style="{CARD_STYLE}">{head}{body}</div>'


# =====================================================================
# 图表标题行：标题文字（HTML）+ 网页同款小标签
# =====================================================================
# 图内不再画标题（view_show 里已全部去掉 set_title），标题只在这里渲染一次，
# 与网页「标的信息」页的 h3.card-title + span.ct-vals 结构对齐。
_CHIP_STYLE = ("display:inline-block;border-radius:20px;padding:1px 8px;"
               "font-size:12px;font-weight:700;background:#f2f4f8;"
               "color:#46506a;font-variant-numeric:tabular-nums;")
_CHIP_MUTED_BG = "#f6f7fa"
_CHEAP, _DEAR = "#1e8e5a", "#c94f4f"       # 与前端 --cheap / --dear 一致


def _chip(text, color: str | None = None, muted: bool = False,
          bg: str | None = None) -> str:
    """一个小标签（网页 .ct-chip 的邮件版）。

    color 只改字色、底色仍为浅灰 —— 与网页 valChip(text, '', color) 的观感一致。
    """
    style = _CHIP_STYLE
    if muted:
        style = (style.replace("background:#f2f4f8", f"background:{_CHIP_MUTED_BG}")
                      .replace("font-weight:700", "font-weight:500")
                      .replace("color:#46506a", "color:#8b93a5"))
    if bg:
        style = style.replace("background:#f2f4f8", f"background:{bg}")
    if color:
        style = style.replace("color:#46506a", f"color:{color}")
    return f'<span style="{style}">{_e(text)}</span>'


def _chart_head(title: str, chips: list) -> str:
    """左标题 + 右小标签（对应网页 .card-title 里的标题与 .ct-vals）。"""
    right = "".join(c for c in chips if c)
    if right:
        right = ('<span style="margin-left:10px;display:inline-flex;gap:6px;'
                 f'align-items:center;flex-wrap:wrap;">{right}</span>')
    return ('<div style="display:flex;justify-content:space-between;'
            'align-items:center;gap:8px;margin-bottom:5px;">'
            '<span style="font-size:13px;font-weight:600;color:#2f3542;">'
            f'{_e(title)}</span>{right}</div>')


def _chart_chips(item: dict, key: str) -> list:
    """图表标题右侧的小标签 —— 与网页标的信息页的图表标题一一对应。

    K线走势     最新价 · 涨跌幅 · 日期
    综合评分走势 最新评分 · 5年 · 偏离(超阈值时) · 状态 · 日期
    XX 估值分位  最新分位
    末尾统一跟一个窗口（近 N 年）；K 线窗口与其它图不同，标它真实画了多少。
    """
    info = item.get("info") or {}
    years = item.get("years") or config.get_int("HISTORY_YEARS_CHART", 5)

    if key == "kline":
        close, pct = info.get("close"), info.get("pct_chg")
        col = None if pct is None else (_DEAR if pct >= 0 else _CHEAP)
        chips = []
        if close is not None:
            chips.append(_chip(f"{close:.2f}", col))
        if pct is not None:
            chips.append(_chip(f"{pct:+.2f}%", col))
        if info.get("kline_last"):
            chips.append(_chip(str(info["kline_last"]), muted=True))
        # 邮件里的 K 线只画最近这些个交易日（图不能缩放，再密就看不清了），
        # 窗口和上面那 6 张走势图不一样，这里如实标出来。
        chips.append(_chip(f"最近 {view_show.KLINE_MAX_CANDLES} 个交易日",
                           muted=True))
        return chips

    if key == "score":
        s, s5 = item.get("score"), item.get("score5")
        chips = []
        if s is not None:
            chips.append(_chip(f"{s:.1f}%", item.get("color")))
        if s5 is not None:
            chips.append(_chip(f"5年 {s5:.1f}%", muted=True))
        div = item.get("divergence")
        thr = config.get_int("DIVERGENCE_THRESHOLD", 15)
        if div is not None and abs(div) >= thr:
            chips.append(_chip(f"偏离 {abs(div):.1f}", "#a85a00", bg="#fdf1de"))
        if item.get("status"):
            chips.append(_chip(item["status"], item.get("color")))
        if item.get("date"):
            chips.append(_chip(str(item["date"]), muted=True))
        return chips + [_chip(f"近 {years} 年", muted=True)]

    if key.startswith("pct_"):
        wkey = CHART_METRIC_KEY.get(key)
        row = next((r for r in (item.get("rows") or []) if r.get("wkey") == wkey),
                   None)
        chips = []
        if row and row.get("pct10") is not None:
            p = row["pct10"]
            chips.append(_chip(f"{p:.1f}%", config.signal_of(p)["color"]))
        return chips + [_chip(f"近 {years} 年", muted=True)]

    return [_chip(f"近 {years} 年", muted=True)]



# 邮件图表顺序：与网页标的信息页一致
#   头部横幅 → 基本信息 → 评分权重 → 五指标卡 → K线 → 综合评分走势 → 5 张分位走势
CHART_ORDER = ("kline", "score", "pct_pe", "pct_pb", "pct_ps", "pct_pcf",
               "pct_dividend")
CHART_TITLES = {
    "kline": "K线走势",
    "score": "综合评分走势",
    "pct_pe": "PE-TTM 估值分位",
    "pct_pb": "PB 估值分位",
    "pct_ps": "PS-TTM 估值分位",
    "pct_pcf": "PCF 估值分位",
    "pct_dividend": "股息率 估值分位",
    # 保留旧的合并图/条形图标题（命令行出图仍会生成）
    "pct": "五指标分位走势",
    "bars": "当前五指标分位",
    "values": "指数估值绝对值",
}

# 图 key → item["rows"] 里的权重键（取该指标的最新分位做标题小标签）
# 与 view_show.build_charts 生成 pct_* 键的规则保持一致
CHART_METRIC_KEY = {
    "pct_pe": "pe", "pct_pb": "pb", "pct_ps": "ps",
    "pct_pcf": "pcf", "pct_dividend": "dividend",
}

# 指标卡（与网页五指标卡一一对应）：(表头, valuation_score 分位字段, 权重键)
CARD_SPECS = (
    ("PE-TTM", "pct_pe", "pe"),
    ("PB", "pct_pb", "pb"),
    ("PS-TTM", "pct_ps", "ps"),
    ("PCF", "pct_pcf", "pcf"),
    ("股息率", "pct_dividend", "dividend"),
)


# =====================================================================
# 配色（与 view_show 的信号色保持一致）
# =====================================================================
def _e(v) -> str:
    """把库里的字符串转义后放进 HTML。

    name/status/action 等都来自数据库（可由本地配置改动），不转义的话
    一封邮件或一个预览页面就能注入脚本（预览是同源 text/html，属存储型 XSS）。
    """
    return html.escape("" if v is None else str(v), quote=True)


def signal_color(status: str) -> str:
    """状态主色（从配置 STATUS_STYLE 取，未配置兜底灰色）。"""
    style = config.status_style().get(status)
    return style[0] if style else "#999999"




def score_color(score: float) -> str:
    """评分 → 横幅背景色：0 分鲜绿 → 50 分沉暗中性 → 100 分鲜红（方案C）。

    明度限制在 24%~37%，保证白字对比度全程 ≥4.5（AA 标准）。
    """
    s = max(0.0, min(100.0, score)) / 100.0
    edge = abs(s - 0.5) * 2                 # 0=中性(50分)，1=两端
    hue = (145.0 - 145.0 * s) / 360.0       # 145°绿 → 0°红
    light = 0.24 + 0.13 * edge
    sat = 0.55 + 0.25 * edge
    r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def pct_color(pct: float) -> str:
    """分位 → 颜色（低分位=便宜绿，高分位=贵红），表格分位着色用。"""
    if pct is None:
        return "#bbb"
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
# 数据收集
# =====================================================================
def _fmt_val(v, unit: str, nd: int = 2) -> str:
    if v is None:
        return '<span style="color:#bbb;">-</span>'
    return f"<b>{v:.{nd}f}{unit}</b>"


def _delta_html(cur, prev, invert: bool = False) -> str:
    """较前日变化：数值下降=变便宜→绿；上升=变贵→红（股息率反向）。"""
    if prev is None or cur is None:
        return '<span style="color:#c4ccd8;">-</span>'
    d = cur - prev
    if abs(d) < 1e-9:
        return '<span style="color:#b6bfcc;">─ 0.00</span>'
    cheaper = (d > 0) if invert else (d < 0)
    col = "#1e8e5a" if cheaper else "#c0392b"
    arrow = "▲" if d > 0 else "▼"
    return f'<span style="color:{col};">{arrow} {abs(d):.2f}</span>'


def _score_delta(score, prev) -> str:
    """评分较前日变化（涨=变贵→红，跌=变便宜→绿）。"""
    if prev is None or score is None:
        return ""
    d = score - prev
    if d > 0.05:
        return f"↑{d:.1f}"
    if d < -0.05:
        return f"↓{abs(d):.1f}"
    return "→0"


def _delta_label(date: str, prev_date: str | None) -> str:
    """对比基准的措辞：相邻交易日叫"较前日"，间隔较远（历史按月采样）叫"较上次"。

    避免首次建库（评分历史按月采样）时把"较上月"误写成"较前日"。
    """
    if not prev_date:
        return "较前日"
    try:
        gap = (datetime.strptime(date, "%Y-%m-%d")
               - datetime.strptime(prev_date, "%Y-%m-%d")).days
    except ValueError:
        return "较前日"
    return "较前日" if gap <= 4 else f"较{prev_date[5:]}"


def collect(conn, codes=None, years=None) -> list[dict]:
    """收集各标的的渲染数据（含图表）。

    返回 [{code,name,emoji,status,action,alert,score,score5,score_prev,date,
           rows:[...],weights,weights_txt,charts:{key:cid}}]
    无评分记录的标的会被跳过（只在日志里提示）。
    """
    targets = config.targets()            # 只取启用中的标的
    if codes:
        want = set(codes)
        targets = [t for t in targets if t["code"] in want]
    targets.sort(key=lambda t: (t.get("sort_order") or 0, t["code"]))
    # 走势图窗口：调用方没指定就取配置（邮件标题行的小标签要显示这个实际值）
    years = years or config.get_int("HISTORY_YEARS_CHART", 5)

    items = []
    for t in targets:
        code, name = t["code"], t["name"]
        sc = storage.latest_score(conn, code)
        if not sc:
            log.warning("%s（%s）尚无评分记录，跳过（先跑一次评分）", name, code)
            continue
        date = sc["date"]
        prev = storage.score_before(conn, code, date) or {}

        # 指数估值绝对值（方案A写回 kline 的指数行）；取最新两条算"较前日"
        krows = storage.load_kline(conn, code, fields=("date",) + view_show.METRICS)
        if not krows:
            log.warning("%s 无估值K线数据，绝对值列将显示为空", name)
        cur_row = krows[-1] if krows else {}
        prev_row = krows[-2] if len(krows) >= 2 else {}

        # 基本信息（与网页标的信息页的两行网格一一对应）
        basics = storage.load_stock_basic(conn, code=code)
        b = basics[0] if basics else {}
        span = storage.kline_span(conn, code)
        # 最新收盘/涨跌幅取行情K线（含 close/pct_chg，估值字段那份不含）
        q = storage.load_kline(conn, code, fields=("date", "close", "pct_chg"))
        lastq = q[-1] if q else {}
        market = b.get("market") or (code.split(".")[0] if "." in code else "")
        info = {
            "code": code,
            "ktype": t["ktype"],
            "market": {"sh": "上交所", "sz": "深交所"}.get(market, market or ""),
            "industry": b.get("industry"),
            "listed_date": b.get("listed_date"),
            "close": lastq.get("close"),
            "pct_chg": lastq.get("pct_chg"),
            "kline_first": span["first"],
            "kline_last": span["last"] or lastq.get("date"),
            "kline_rows": span["rows"],
            "constituent_count": (len(storage.load_constituents(conn, code))
                                  if t["ktype"] in ("index", "portfolio") else None),
            "is_target": True,
        }

        rows = []
        for field, label, pct_f, pct5_f, invert, unit, wkey in METRIC_SPECS:
            rows.append({
                "label": label, "unit": unit, "invert": invert, "wkey": wkey,
                "cur": cur_row.get(field), "prev": prev_row.get(field),
                "pct10": sc.get(pct_f), "pct5": sc.get(pct5_f),
            })

        charts = view_show.build_charts(conn, code, ktype=t["ktype"], years=years,
                                       with_values=(t["ktype"] == "index"))
        _s, _s5 = sc.get("score"), sc.get("score5")
        divergence = (_s5 - _s) if (_s is not None and _s5 is not None) else None
        # 信号一律按**当前配置**实时推导：库里的 status/action 是算分位那刻的快照，
        # 改了 SIGNAL_BANDS 之后就不准了（见 config.signal_of 的说明）。
        sig = config.signal_of(_s)
        items.append({
            "code": code, "name": name, "ktype": t["ktype"], "date": date,
            "years": years,
            "score": _s, "score5": _s5,
            "score_prev": prev.get("score"),
            "prev_date": prev.get("date"),
            "dlabel": _delta_label(date, prev.get("date")),
            "status": sig["status"],
            "action": sig["action"],
            "action_short": sig["short"],
            "emoji": sig["icon"] or sig["emoji"] or "👉",
            "color": sig["color"],
            "text": sig["text"],
            "alert": sig["alert"],
            "rows": rows, "weights": t["weights"], "charts": charts,
            "info": info,
            "n_used": sc.get("n_used"),
            "divergence": divergence,
            "n_total": (len(storage.load_constituents(conn, code))
                        if t["ktype"] == "index" else None),
            "missing_metrics": [wkey for (_f, _l, pct_f, _p5, _i, _u, wkey)
                                in METRIC_SPECS if sc.get(pct_f) is None],
        })
    return items


# =====================================================================
# 渲染：单个标的卡片
# =====================================================================
def _email_pill(status: str, color: str, icon: str = "",
                action: str = "", alert: bool = False) -> str:
    """状态胶囊：与网页横幅一致——白底 + 状态色文字 + 图标 + 短动作 + 告警标记。

    邮件里没有 CSS 变量，直接内联样式；用 white 底是因为横幅底色是评分色阶，
    状态色直接做底会撞色（见 README 里那段说明）。
    """
    bits = []
    if icon:
        bits.append(f'<span style="margin-right:3px;">{_e(icon)}</span>')
    bits.append(f'<span style="font-weight:800;">{_e(status)}</span>')
    if action:
        bits.append(f'<span style="font-weight:600;opacity:.8;"> · {_e(action)}</span>')
    if alert:
        bits.append(f'<span style="margin-left:2px;">{_e(config.get_str("ALERT_MARK", "⚡"))}</span>')
    return (f'<span style="display:inline-block;background:rgba(255,255,255,.93);'
            f'color:{color};font-size:12px;line-height:1.6;padding:2px 10px;'
            f'border-radius:20px;white-space:nowrap;'
            f'box-shadow:0 1px 3px rgba(0,0,0,.16);">{"".join(bits)}</span>')


def _info_rows_html(item: dict) -> str:
    """基本信息：与网页标的信息页的 2 行 × 5 列网格一一对应。

    第一行＝标的属性（代码/类型/市场/行业/上市日期）
    第二行＝最新数据（最新收盘+涨跌/综合评分+5年/评分权重/分位偏离/K线区间）
    缺值填「—」，每格都是「标签 / 值 / 补充说明」三层，保证两行等高对齐。
    """
    info = item.get("info") or {}
    score, score5 = item.get("score"), item.get("score5")
    color = item.get("color") or signal_color(item["status"])
    w = item.get("weights") or {}
    sum_w = round(sum(w.get(k, 0) or 0 for k in ("pe", "pb", "ps", "pcf", "dividend")) * 100)
    div = item.get("divergence")
    thr = config.get_int("DIVERGENCE_THRESHOLD", 15)
    kt = {"index": "指数", "portfolio": "组合"}.get(info.get("ktype"), "个股")

    def cell(label, value, sub="", tone=""):
        bg = {"ok": "#f4fbf7", "warn": "#fffaf0"}.get(tone, "#ffffff")
        vcol = "#a85a00" if tone == "warn" else "#2f3542"
        if value in (None, ""):
            value, vcol = "—", "#c3c9d6"
        return (
            f'<td style="background:{bg};border:1px solid #e6e8ef;padding:9px 12px;'
            f'vertical-align:top;width:20%;">'
            f'<div style="font-size:11px;color:#8b93a5;line-height:1.5;">{_e(label)}</div>'
            f'<div style="font-size:14px;font-weight:700;color:{vcol};line-height:1.45;'
            f'font-variant-numeric:tabular-nums;">{value}</div>'
            f'<div style="font-size:11px;line-height:1.5;color:#8b93a5;">{sub or "&nbsp;"}</div>'
            '</td>')

    # 涨跌幅单独着色
    pct = info.get("pct_chg")
    if pct is None:
        chg_sub = ""
    else:
        c = "#c94f4f" if pct >= 0 else "#1e8e5a"
        chg_sub = (f'<span style="color:{c};font-weight:600;">'
                   f'{"+" if pct >= 0 else ""}{pct:.2f}%</span>')
    score_val = (f'<span style="color:{color};">{score:.1f}%</span>'
                 if score is not None else None)
    score_sub = f'5年 {score5:.1f}%' if score5 is not None else ""
    weight_tone = "ok" if sum_w > 0 else "warn"
    w_val = "已配置" if sum_w > 0 else "未配置"
    w_sub = f'合计 {sum_w}%' if sum_w > 0 else "未设置比例"
    if div is None:
        div_val, div_tone, div_sub = None, "", f'阈值 {thr}'
    else:
        over = abs(div) >= thr
        div_val = (f'<span style="color:{"#a85a00" if over else "#2f3542"};">'
                   f'{"+" if div > 0 else ""}{div:.1f}</span>')
        div_tone, div_sub = ("warn" if over else ""), f'阈值 {thr}'
    first, last = info.get("kline_first"), info.get("kline_last")
    kline_val = (f'{(first or "—")[:7]} ~ {(last or "—")[:7]}'
                 if (first or last) else None)
    rows = info.get("kline_rows")

    row1 = "".join([
        cell("代码", _e(info.get("code"))),
        cell("类型", kt, f'{info["constituent_count"]} 只成分股'
             if info.get("constituent_count") is not None else ""),
        cell("市场", _e(info.get("market"))),
        cell("行业", _e(info.get("industry"))),
        cell("上市日期", _e(info.get("listed_date"))),
    ])
    row2 = "".join([
        cell("最新收盘", f'{info["close"]:.2f}' if info.get("close") is not None else None,
             chg_sub),
        cell("综合评分", score_val, score_sub),
        cell("评分权重", w_val, w_sub, weight_tone),
        cell("分位偏离", div_val, div_sub, div_tone),
        cell("K线区间", kline_val, f'{rows} 行' if rows else ""),
    ])
    return (
        '<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;'
        'width:100%;table-layout:fixed;">'
        f'<tr>{row1}</tr><tr>{row2}</tr></table>')


def _weights_html(item: dict) -> str:
    """评分权重：一条堆叠条 + 图例（与网页「评分权重」卡片一致）。"""
    w = item.get("weights") or {}
    parts = [(label, w.get(key, 0) or 0, view_show.METRIC_COLORS.get(field, "#75839a"))
             for field, label, _p, _p5, _i, _u, key in METRIC_SPECS]
    parts = [(l, v, c) for l, v, c in parts if v > 0]
    if not parts:
        return ('<div style="margin-top:12px;font-size:12px;color:#8b93a5;">'
                '未配置评分权重：不计算综合评分，只展示估值分位</div>')
    segs = "".join(
        f'<td style="width:{v * 100:.1f}%;background:{c};height:12px;'
        f'font-size:0;line-height:0;">&nbsp;</td>' for _l, v, c in parts)
    legend = "".join(
        f'<span style="display:inline-block;margin:6px 14px 0 0;font-size:12px;'
        f'color:#55606e;"><i style="display:inline-block;width:9px;height:9px;'
        f'border-radius:2px;background:{c};margin-right:5px;"></i>'
        f'{_e(l)} {v * 100:.0f}%</span>' for l, v, c in parts)
    total = round(sum(v for _l, v, _c in parts) * 100)
    return (
        '<table cellspacing="0" cellpadding="0" style="border-collapse:separate;'
        'border-spacing:0;width:100%;background:#eef1f7;border-radius:6px;'
        'overflow:hidden;table-layout:fixed;">'
        f'<tr>{segs}</tr></table>'
        f'<div>{legend}</div>'), total


def _metric_cards_html(item: dict) -> str:
    """五指标卡：与网页「指标与权重」卡片里的指标卡一致。

    每张卡：彩色标签 + 权重占比 / 当前值 / 10年分位（+ 5年分位）。
    """
    info_rows = {r["wkey"]: r for r in item.get("rows") or []}
    w = item.get("weights") or {}
    cards = []
    for label, pct_field, key in CARD_SPECS:
        r = info_rows.get(key) or {}
        field = next((f for f, l, _p, _p5, _i, _u, k in METRIC_SPECS if k == key), None)
        c = view_show.METRIC_COLORS.get(field, "#75839a")
        val = _fmt_val(r.get("cur"), r.get("unit"))
        wv = w.get(key) or 0
        # 权重占比小标（与网页 .metric-w 一致）
        wtag = (f'<span style="font-size:11px;font-weight:600;color:#8b93a5;'
                f'background:#eef1f7;border-radius:20px;padding:1px 7px;">'
                f'{wv * 100:.0f}%</span>') if wv > 0 else ""
        p10, p5 = r.get("pct10"), r.get("pct5")
        pcts = []
        if p10 is not None:
            pcts.append(f'10年分位 <b style="color:{pct_color(p10)};">{p10:.0f}%</b>')
        if p5 is not None:
            pcts.append(f'5年 <b style="color:{pct_color(p5)};">{p5:.0f}%</b>')
        cards.append(
            f'<td style="width:20%;border:1px solid #e6e8ef;border-radius:10px;'
            f'padding:9px 12px 10px;vertical-align:top;background:#fafbfd;">'
            f'<div style="display:flex;justify-content:space-between;'
            f'align-items:baseline;gap:6px;">'
            f'<span style="font-size:12px;font-weight:700;color:{c};">{_e(label)}</span>'
            f'{wtag}</div>'
            f'<div style="font-size:19px;font-weight:700;color:#2f3542;line-height:1.35;'
            f'margin-top:2px;font-variant-numeric:tabular-nums;">{val}</div>'
            f'<div style="font-size:11px;color:#8b93a5;line-height:1.5;">'
            f'{"　".join(pcts) or "&nbsp;"}</div>'
            '</td>')
    # 用 border-spacing 造出卡片间距（邮件里 table 最稳）
    return (
        '<table cellspacing="0" cellpadding="0" style="border-collapse:separate;'
        'border-spacing:8px 0;width:100%;table-layout:fixed;margin:0 -8px;">'
        f'<tr>{"".join(cards)}</tr></table>')


def index_section(item: dict, idx: int) -> tuple[str, list]:
    """渲染单个标的的卡片，返回 (html, [(cid, png), ...])。

    区块顺序与网页「标的信息」页严格对齐：
      头部横幅 → 基本信息 → 评分权重 → 五指标卡 → K线 → 综合评分走势 → 5 张分位走势
    """
    status = item["status"]
    color = item.get("color") or signal_color(status)
    alert = item["alert"]
    score = item["score"] or 0.0
    score_prev = item["score_prev"]
    w = item.get("weights") or config.get_json("COMPOSITE_WEIGHTS", {}) or {}
    parts = [f"{label} {w.get(wkey, 0) * 100:.0f}%"
             for _f, label, _p, _p5, _i, _u, wkey in METRIC_SPECS
             if (w.get(wkey) or 0) > 0]
    weights_txt = " · ".join(parts) or "未配置权重"
    note = ("分位说明：0%=历史最低（最便宜）　100%=历史最高（最贵）；"
            "综合评分 = 各指标分位按权重加权（缺失指标自动剔除并归一）；"
            "股息率已反向（股息率越高越便宜）。")

    # ---------- 图表：PNG 里**不画标题**，标题由这里的 HTML 渲染 ----------
    # 与网页「标的信息」页的图表标题完全一致：左边标题文字，右边一排小标签
    # （.ct-chip 的邮件版），把最新值这类信息放在标题行上，而不是印进图片里。
    imgs, images = [], []
    img_style = ("display:block;width:100%;border-radius:8px;"
                 "border:1px solid #eef1f5;")
    for key in CHART_ORDER:
        png = item["charts"].get(key)
        if not png:
            continue
        cid = f"img{idx}_{key}"
        images.append((cid, png))
        title = CHART_TITLES.get(key, key)
        # 图与图之间只留 10px（以前每张图各自一张卡，间距接近 46px，太空）
        imgs.append(
            '<div style="margin-top:10px;">'
            + _chart_head(title, _chart_chips(item, key))
            + f'<img src="cid:{cid}" style="{img_style}" '
              f'alt="{_e(title)}" /></div>')

    # ---------- 横幅：名称 + 状态胶囊（与网页横幅一致）----------
    bg = score_color(score)
    if score_prev is None:
        delta_box = ('<span style="font-size:11px;opacity:.85;white-space:nowrap;">'
                     f'{_e(item.get("dlabel", "较前日"))} —</span>')
    else:
        d = score - score_prev
        if d > 0.05:
            a, t, c = "▲", f"+{d:.1f}", "#c0392b"
        elif d < -0.05:
            a, t, c = "▼", f"{d:.1f}", "#1e8e5a"
        else:
            a, t, c = "─", "0.0", "#6b7684"
        # 与网页一致：白底 + 方向色文字（评分越高越贵 → 涨红跌绿）
        dcol = "#c94f4f" if d > 0 else "#1e8e5a"
        delta_box = (
            f'<span style="background:rgba(255,255,255,.93);color:{dcol};'
            f'font-size:12px;font-weight:800;padding:2px 9px;border-radius:8px;'
            f'box-shadow:0 1px 3px rgba(0,0,0,.16);white-space:nowrap;">{a} {t}</span>'
            '<span style="font-size:11px;opacity:.9;margin-left:5px;'
            f'white-space:nowrap;">{_e(item.get("dlabel", "较前日"))}</span>')

    # ---------- 横幅：与网页「标的信息」页头部**完全一致** ----------
    #   左侧：名称 + 紧跟其后的状态胶囊（图标 + 状态 + 短动作 + 告警标记），
    #         下一行是副信息：代码 · 类型 · 成分股 · 行业 · 上市日期
    #   右侧：综合评分 + 较前日变化
    # 网页横幅没有底部动作条，所以这里也去掉（动作建议已经在胶囊里了）。
    info = item.get("info") or {}
    kt = {"index": "指数", "portfolio": "组合"}.get(info.get("ktype"), "个股")
    sub_bits = [_e(info.get("code")), kt]
    if info.get("constituent_count") is not None:
        sub_bits.append(f'{info["constituent_count"]} 只成分股')
    if info.get("industry"):
        sub_bits.append(_e(info["industry"]))
    if info.get("listed_date"):
        sub_bits.append('上市 ' + _e(info["listed_date"]))

    header = (
        f'<div style="background:{bg};color:#fff;'
        f'padding:16px 18px;text-shadow:0 1px 2px rgba(0,0,0,.22);">'
        '<div style="display:flex;justify-content:space-between;'
        'align-items:center;gap:14px;">'
        '<div style="min-width:0;">'
        f'<div style="font-size:19px;font-weight:800;letter-spacing:.3px;'
        f'line-height:1.45;">{_e(item["name"])}&nbsp;'
        + _email_pill(status, color, item.get("emoji") or "",
                      item.get("action_short") or "", alert)
        + '</div>'
        '<div style="font-size:12px;opacity:.9;margin-top:3px;">'
        + " · ".join(sub_bits) + '</div></div>'
        '<div style="text-align:right;flex-shrink:0;">'
        f'<div style="font-size:28px;font-weight:800;line-height:1.1;">'
        f'{score:.1f}%</div>'
        f'<div style="margin-top:4px;white-space:nowrap;">{delta_box}</div></div>'
        '</div></div>'
    )

    # 数据完整度提示：指标缺失/成分股覆盖不足时必须显式告诉用户
    warn_bits = []
    if item.get("n_used") and item.get("n_total") and item["ktype"] == "index":
        cov = item["n_used"] / max(1, item["n_total"])
        if cov < 0.9:
            warn_bits.append(f'成分股有效 {item["n_used"]}/{item["n_total"]}'
                             f'（{cov * 100:.0f}%）')
    if item.get("missing_metrics"):
        warn_bits.append("缺失指标：" + "/".join(
            METRIC_LABELS.get(k, k) for k in item["missing_metrics"]))
    warn = ('<div style="padding:6px 2px 0;font-size:11px;color:#a85a00;">'
            f'⚠ {" · ".join(warn_bits)}</div>') if warn_bits else ""

    meta_line = (
        '<div style="display:flex;align-items:center;justify-content:space-between;'
        'padding:0 2px 10px;font-size:11px;color:#8b93a5;">'
        f'<span>数据日期 {_e(item["date"])}</span><span>{_e(weights_txt)}</span></div>'
        f'{warn}'
    )

    weights_body, weights_total = _weights_html(item)
    # 整只标的 = **一张完整的大卡片**：横幅 + 基本信息 + 指标与权重 + 全部图表。
    # 这样一封邮件里有多个标的时，一眼就能看出"这一块是一只标的"，
    # 而不是一堆各自独立、间距又很大的卡片混在一起。
    inner = (
        meta_line
        + _panel('基本信息', _info_rows_html(item))
        + _panel('指标与权重',
                 weights_body
                 + '<div style="border-top:1px dashed #e6e8ef;margin-top:12px;'
                   'padding-top:12px;">'
                 + _metric_cards_html(item) + '</div>',
                 f'合计 {weights_total}%' if weights_total else '')
        + "".join(imgs)
        + f'<p style="color:#8b93a5;font-size:11px;line-height:1.6;'
          f'margin:12px 2px 0;">{note}</p>'
    )
    html = (
        '<div style="border:1px solid #dfe3ea;border-radius:14px;background:#fff;'
        'overflow:hidden;margin-bottom:20px;'
        'box-shadow:0 1px 4px rgba(30,40,70,.06);">'
        + header
        + f'<div style="padding:12px 16px 16px;">{inner}</div>'
        '</div>'
    )
    return html, images


def build_subject(items: list) -> str:
    """标题：状态+分数+较前日变化+动作短词；有告警则加前缀与标记。"""
    has_alert = any(i["alert"] for i in items)
    prefix = config.get_str("ALERT_PREFIX", "") if has_alert else ""
    mark = config.get_str("ALERT_MARK", "⚡")
    parts = []
    for i in items:
        action_short = i.get("action_short") or ""
        delta = _score_delta(i["score"], i["score_prev"])
        parts.append(
            f'{_e(i["name"])}{_e(i["emoji"])}{_e(i["status"])}'
            f'{(i["score"] or 0):.0f}%{delta}({_e(action_short)})'
            f'{mark if i["alert"] else ""}')
    return prefix + "｜".join(parts)


def divergence_html(items: list) -> str:
    """「短期显著偏离长期」提示块：5 年口径分位与 10 年口径分位差异过大时展示。

    差异 = score5 − score（正 = 短期比长期贵）。阈值由 DIVERGENCE_THRESHOLD 配置。
    没有偏离的标的返回空串（邮件里不显示该块）。
    """
    thr = config.get_int("DIVERGENCE_THRESHOLD", 15)
    divs = [i for i in items
            if i.get("divergence") is not None and abs(i["divergence"]) >= thr]
    if not divs:
        return ""
    divs.sort(key=lambda i: -abs(i["divergence"]))
    rows = []
    for i in divs:
        d = i["divergence"]
        if d > 0:
            direction, col = "短期估值高于长期（近期中枢上移）", "#c0392b"
        else:
            direction, col = "短期估值低于长期（近期中枢下移）", "#1e8e5a"
        rows.append(
            f'<li style="margin:3px 0;"><b>{_e(i["name"])}</b>：'
            f'10年 {i["score"]:.0f}% / 5年 {i["score5"]:.0f}%'
            f'（差 {abs(d):.0f}）<span style="color:{col};font-weight:600;">'
            f'{direction}</span></li>')
    return (
        '<div style="background:#fff8e6;border:1px solid #f0d79a;border-radius:10px;'
        'padding:10px 14px;margin-bottom:12px;font-size:12px;color:#7a5a12;">'
        '<div style="font-weight:800;font-size:13px;margin-bottom:4px;">'
        '⚠️ 短期显著偏离长期</div>'
        f'<ul style="margin:0;padding-left:18px;">{"".join(rows)}</ul>'
        '<div style="color:#9a8a5a;margin-top:5px;line-height:1.5;">'
        '说明：5 年口径分位明显偏离 10 年口径，说明近期估值中枢相对长期发生了变化，'
        '看分位时建议结合绝对估值一起判断。</div></div>'
    )


def build_html(sections: list, subject: str = "", summary: str = "",
               notice: str = "") -> str:
    """组装完整 HTML。

    整体风格与网页「标的信息」页保持一致：
      * 页面底色 `#f4f5f8`（网页 --bg），容器 760px
      * 顶部一条白色标题条（对应网页顶栏），告警时变红
      * 每张卡片白底 + 1px 边框 + 12px 圆角 + 极淡阴影（网页 .card）
      * 底部一句免责声明
    首行放标题文本便于手机通知栏直接看到信号。
    """
    alert_head = (summary == "alert")
    head_style = ("background:#c94f4f;color:#fff;border:1px solid #c94f4f;"
                  if alert_head else
                  "background:#ffffff;color:#243340;border:1px solid #e6e8ef;")
    head = (
        f'<div style="{head_style}border-radius:12px;padding:12px 16px;'
        'margin-bottom:16px;box-shadow:0 1px 3px rgba(30,40,70,.04);">'
        f'<div style="font-size:15px;font-weight:700;line-height:1.5;">'
        f'{_e(subject)}</div>'
        '<div style="font-size:11px;opacity:.75;margin-top:3px;">'
        '指数估值信号</div></div>'
    )
    return f"""<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,Segoe UI,PingFang SC,Microsoft YaHei,sans-serif;
color:#2f3542;margin:0;padding:20px 12px;background:#f4f5f8;
-webkit-text-size-adjust:100%;">
<div style="max-width:760px;margin:0 auto;">
{head}
{notice}
{''.join(sections)}
<p style="color:#8b93a5;font-size:11px;text-align:center;margin:18px 0 0;line-height:1.6;">
本邮件由树莓派定时任务自动生成，仅作估值参考，不构成投资建议。</p>
</div></body></html>"""

def build_report(conn, codes=None, years=None) -> dict:
    """渲染完整报告。返回 {subject, html, images, items, has_alert, alert_names}。"""
    items = collect(conn, codes=codes, years=years)
    if not items:
        return {"subject": "", "html": "", "images": [], "items": [],
                "has_alert": False, "alert_names": [], "empty": True}

    sections, images = [], []
    for i, item in enumerate(items):
        html, imgs = index_section(item, i)
        sections.append(html)
        images.extend(imgs)

    subject = build_subject(items)
    has_alert = any(i["alert"] for i in items)
    notice = divergence_html(items)
    html = build_html(sections, subject, "alert" if has_alert else "", notice)
    return {
        "subject": subject, "html": html, "images": images, "items": items,
        "has_alert": has_alert,
        "alert_names": [i["name"] for i in items if i["alert"]],
        "divergence_names": [i["name"] for i in items
                             if i.get("divergence") is not None
                             and abs(i["divergence"])
                             >= config.get_int("DIVERGENCE_THRESHOLD", 15)],
        "empty": False,
    }


def inline_images(html: str, images: list) -> str:
    """把 cid: 引用替换成 base64 data URI（预览 HTML 单文件可离线打开）。"""
    for cid, png in images:
        html = html.replace(f"cid:{cid}",
                            "data:image/png;base64,"
                            + base64.b64encode(png).decode())
    return html


def save_preview(conn, path: str | None = None, codes=None, years=None) -> dict:
    """生成自包含预览 HTML（不做任何发送）。返回 {path, subject, items, bytes}。"""
    rep = build_report(conn, codes=codes, years=years)
    if rep.get("empty"):
        raise RuntimeError("没有可渲染的标的（库中没有评分记录，请先跑一次评分）")
    html = inline_images(rep["html"], rep["images"])
    path = path or os.path.join(config.web()["preview_dir"], "preview.html")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # 先写临时文件再原子改名：避免浏览器读到写了一半的文件
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, path)
    log.info("预览 HTML 已生成：%s（%d 个标的，%d 张图，未发送邮件）",
             path, len(rep["items"]), len(rep["images"]))
    return {"path": os.path.abspath(path), "subject": rep["subject"],
            "items": len(rep["items"]), "images": len(rep["images"]),
            "bytes": len(html.encode("utf-8"))}


def preview_filename(prefix: str = "preview") -> str:
    """带时间戳的预览文件名（web 端每次生成一份，便于回看）。

    精确到微秒：秒级分辨率下同一秒内的两个并发请求会写同一个文件，
    互相覆盖、并且可能读到写了一半的 HTML。
    """
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.html"
