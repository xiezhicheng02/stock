# -*- coding: utf-8 -*-
"""估值指标计算：成分股 → 指数估值聚合，以及五指标综合评分。

两条算法线（A + B 并行）
------------------------
方案 A（指数估值绝对值，供图表展示）
    每日按【流通市值权重】聚合成分股估值，得到指数 PE/PB/PS/PCF/股息率序列，
    写回 kline 表的指数行。
    加权口径：
        PE / PB / PS / PCF → 调和加权  1/Σ(w_i/x_i)（等价整体法 Σ市值/Σ财务指标）
        股息率              → 算术加权  Σ(w_i×x_i)
    异常值：剔除 x<=0（亏损股负 PE）与过大极值。

方案 B（评分，落库供走势图）
    先算每只成分股各指标相对【自身历史】的分位（0~100，已统一"越高越贵"），
    再按流通市值权重加权成指数指标分位 → 按配置权重合成综合评分。
    可消除个股估值水平的天然差异（银行 PE 5 与科技 PE 80 不互相扭曲）。

权重计算
--------
    流通股本 = volume / (turn/100)        # 均为实际值，不受复权影响
    流通市值 = close(前复权) × 流通股本     # 前复权价带来轻微偏差，作近似可接受

评分落库
--------
    valuation_score 表：code + date 一行，含综合评分、5年参考、各指标分位、信号
    * index_score() / stock_score()   算某标的当日评分（默认落库）
    * rebuild_score_history()         重建历史评分序列（供评分走势图）

用法
----
    from src.indicators import indicators as ind

    ind.rebuild_index_valuation(conn, "sh.000300")          # A：指数估值序列
    r = ind.index_score(conn, "sh.000300")                  # B：当日评分（落库）
    ind.rebuild_score_history(conn, "sh.000300", freq="M")  # 历史评分（月频）
"""

import bisect
import logging
import math
import os
from collections import deque
from datetime import date, datetime, timedelta

from src.config import config
from src.storage import storage

log = logging.getLogger("indicators")

# 参与聚合与评分的指标（与 kline 字段同名）
METRICS = ("pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm", "div_yield")

# 指标 → config 权重键名
METRIC_TO_WEIGHT_KEY = {
    "pe_ttm": "pe", "pb_mrq": "pb", "ps_ttm": "ps",
    "pcf_ncf_ttm": "pcf", "div_yield": "dividend",
}

# 反向指标：股息率高 = 便宜（分位需取 100-p）
INVERTED = {"div_yield"}

# 分位计算的最小样本数：样本过少时分位没有统计意义
# （例如新股只有 2 个观测，分位会给出 0/100 的极端值，污染指数加权结果）
MIN_OBS = 20

# 指数聚合的最小覆盖率：有效成分股占比低于该值 → 认为该指标当日不可用，
# 避免"只剩 1~2 只股票有数据"时把它当成整个指数的估值
MIN_COVERAGE = 0.30

# 指标合理区间（剔除亏损/极值）
# 注意：div_yield 下限设为负值——股息率 0 表示"无分红"，是有效观测值，
# 若下限为 0 且用 <= 判定，会把无分红股票整体剔除，
# 导致"全部成分股当年无分红"的日期指数股息率聚合为 None。
VALID_RANGE = {
    "pe_ttm": (0.01, 1000.0),
    "pb_mrq": (0.01, 100.0),
    "ps_ttm": (0.01, 1000.0),
    "pcf_ncf_ttm": (0.01, 1000.0),
    "div_yield": (-0.001, 30.0),
}


# =====================================================================
# 通用工具
# =====================================================================
def percentile(sorted_vals, x) -> float:
    """x 在**已排序**列表中的分位（0~100，线性名次插值）。

    名次定义：最小值 → 0，中位数 → 50，最大值 → 100。
      例：[1,2,3] 中 x=2 → 50；x=1.5 → 25
    重复值取**整段的中点名次**：`[1,2,2,2,3]` 中 x=2 → 50（而不是首个 2 的 25）。
    这一点对股息率很重要——库里存在大量 0 值（无分红），若取首个名次，
    这些 0 会被算成 0 分位（反向成"最贵"），与"分了很少钱"的样本之间形成断崖。
    空列表或单值返回 50（中性）。
    """
    n = len(sorted_vals)
    if n == 0 or x is None:
        return 50.0
    if n == 1:
        return 50.0
    if x < sorted_vals[0]:
        return 0.0
    if x > sorted_vals[-1]:
        return 100.0
    lo_i = bisect.bisect_left(sorted_vals, x)
    hi_i = bisect.bisect_right(sorted_vals, x) - 1
    if lo_i <= hi_i:
        rank = (lo_i + hi_i) / 2.0             # x 命中已有值：取重复段中点
    else:
        rank = hi_i + (x - sorted_vals[hi_i]) / (sorted_vals[lo_i] - sorted_vals[hi_i])
    return rank / (n - 1) * 100


def _valid(metric: str, v) -> bool:
    """指标值是否在合理范围内（NaN/Inf 视为无效）。

    NaN 必须显式拦掉：NaN 与任何数比较都是 False，会一路穿过范围检查、
    分位计算与加权合成，最后让 signal_for 兜底到"最贵"档。
    （注：SQLite 会把 NaN 存成 NULL，所以这是加固而非当前可触发的缺陷。）
    """
    if v is None:
        return False
    if not math.isfinite(v):
        return False
    lo, hi = VALID_RANGE.get(metric, (None, None))
    if lo is not None and v <= lo:
        return False
    if hi is not None and v >= hi:
        return False
    return True


def _shift_days(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d")
            + timedelta(days=days)).strftime("%Y-%m-%d")


def _window_start(years: int, as_of: str | None = None) -> str:
    """N 年窗口的起始日期（以 as_of 或今天为基准）。"""
    base = datetime.strptime(as_of, "%Y-%m-%d") if as_of else datetime.now()
    return (base - timedelta(days=int(365.25 * years))).strftime("%Y-%m-%d")


# =====================================================================
# 权重：流通市值
# =====================================================================
def calc_market_weights(rows) -> dict:
    """按流通市值计算归一化权重。

    rows: [{code, close, volume, turn}]（同一交易日）
    返回 {code: 权重}，和为 1；数据缺失/停牌的标的被剔除。
    """
    caps = {}
    for r in rows:
        v, t, c = r.get("volume"), r.get("turn"), r.get("close")
        if not v or not t or not c or t <= 0 or c <= 0:
            continue
        float_shares = v / (t / 100.0)        # 流通股本（股）
        caps[r["code"]] = c * float_shares    # 流通市值（近似）
    total = sum(caps.values())
    if total <= 0:
        return {}
    return {code: cap / total for code, cap in caps.items()}


def equal_weights(codes) -> dict:
    """等权：{code: 1/N}（和为 1）。"""
    codes = list(codes)
    if not codes:
        return {}
    w = 1.0 / len(codes)
    return {c: w for c in codes}


def constituent_weights(ktype: str, codes, rows) -> dict:
    """成分股权重口径：**指数按流通市值，组合等权**。

    指数是"市场的缩影"，按市值加权才有意义；
    组合是用户自选的一篮子标的，按预期就是每只平均（不支持自定义权重）。
    指数算不出市值时（缺 volume/turn）也退化为等权，避免整条聚合没有结果。
    """
    if ktype == "portfolio":
        return equal_weights(codes)
    w = calc_market_weights(rows)
    return w or equal_weights(codes)


# =====================================================================
# 方案 A：聚合成分股估值 → 指数估值序列
# =====================================================================
def aggregate_metrics(rows, weights, min_coverage: float = MIN_COVERAGE) -> dict:
    """把成分股估值加权聚合成指数估值。

    rows:    [{code, pe_ttm, ...}]
    weights: {code: 权重}
    返回 {指标: 指数值}（无有效样本 / 覆盖不足为 None）

    覆盖率门槛：有效样本数必须 ≥ max(3, 有权重的成分股数 × min_coverage)。
    否则"当天只有 1~2 只股票有数据"时，单只股票的 PE 会被当成整个指数的 PE，
    且完全看不出异常。覆盖不足时返回 None 并记 debug 日志。
    """
    members = [r for r in rows if r["code"] in weights]
    total = len(members)
    out = {}
    for m in METRICS:
        samples = [(weights[r["code"]], r.get(m)) for r in members
                   if _valid(m, r.get(m))]
        need = max(3, math.ceil(total * min_coverage))
        if not samples or total == 0 or len(samples) < need:
            if samples:
                log.debug("指标 %s 覆盖不足（%d/%d，需 %d），按不可用处理",
                          m, len(samples), total, need)
            out[m] = None
            continue
        wsum = sum(w for w, _ in samples)
        if wsum <= 0:
            out[m] = None
            continue
        if m in INVERTED:
            out[m] = sum(w * v for w, v in samples) / wsum        # 算术加权
        else:
            denom = sum(w / v for w, v in samples)                # 调和加权
            out[m] = (wsum / denom) if denom > 0 else None
    return out


def rebuild_index_valuation(conn, index_code: str, start: str | None = None,
                            end: str | None = None, batch: int = 500,
                            only_missing: bool = False,
                            overlap: int = 5) -> int:
    """重建指数估值序列（方案A）并写回 kline 指数行。返回更新的交易日数。

    only_missing=True 时只重算「估值缺失的日期 + 最近 overlap 个交易日」，
    日常增量跑批用这个模式（全天全量重建只用于首次建库）。
    只重算最近几日的原因：盘中跑批会把当天的盘中快照算进去，收盘后需要覆盖修正。

    写回时**跳过值为 None 的指标**，避免"某指标当天无有效样本"把上一轮
    已经算好的正确值抹成 NULL。
    """
    codes = storage.load_constituents(conn, index_code)
    if not codes:
        log.warning("%s 无成分股，跳过估值聚合", index_code)
        return 0

    axis = storage.load_kline(conn, index_code, fields=("date", "pe_ttm", "div_yield"))
    if not axis:
        log.warning("%s 无指数K线，无法确定聚合日期轴", index_code)
        return 0
    if only_missing:
        # 缺失判据**只看 pe_ttm**，不看 div_yield。
        #
        # 以前是 `pe_ttm is None or div_yield is None`，结果每轮都要重算上千天：
        # 个股的 div_yield 要先有 close_raw（不复权收盘价）才能算，而全库只有约
        # 6% 的 K 线行有 close_raw —— 那些历史日期的 div_yield **永远算不出来**，
        # 于是被当成"缺失"反复重算（实测 sh.000300 1209 天 / sh.000905 1104 天，
        # 每 30 分钟白跑 ~23s）。
        #
        # pe_ttm 是聚合的"主指标"，新交易日的数据拉进来时它就是 NULL，
        # 所以这个判据本身就能自然覆盖新增日期，不需要额外水位。
        # 历史 div_yield 的补齐要靠全量重建（only_missing=False），
        # 前提是先补上成分股的 close_raw（那是取数侧的事）。
        want = {r["date"] for r in axis if r["pe_ttm"] is None}
        # 最近 overlap 个交易日无论缺不缺都要重算：盘中跑批会把当天的盘中快照
        # 算进去，收盘后需要覆盖修正。
        want.update(r["date"] for r in axis[-max(1, overlap):])
        dates = sorted(want)
    else:
        dates = [r["date"] for r in axis
                 if (not start or r["date"] >= start) and (not end or r["date"] <= end)]
    if not dates:
        log.info("%s 指数估值无需更新（无缺失日期）", index_code)
        return 0

    ph = ",".join("?" * len(codes))
    sql = (f"SELECT code,date,close,volume,turn,pe_ttm,pb_mrq,ps_ttm,"
           f"pcf_ncf_ttm,div_yield FROM kline WHERE date=? AND code IN ({ph})")

    out_rows, done = [], 0
    for d in dates:
        cons = [dict(r) for r in conn.execute(sql, [d] + codes)]
        if not cons:
            continue
        weights = calc_market_weights(cons)
        if not weights:
            continue
        # 只保留有效指标，避免用 None 覆盖已有值
        agg = {k: v for k, v in aggregate_metrics(cons, weights).items()
               if v is not None}
        if not agg:
            continue
        agg["date"] = d
        out_rows.append(agg)
        done += 1
        if len(out_rows) >= batch:
            storage.update_valuation_fields(conn, index_code, out_rows)
            out_rows.clear()
    if out_rows:
        storage.update_valuation_fields(conn, index_code, out_rows)

    log.info("%s 指数估值聚合：%d 个交易日（成分股 %d 只）",
             index_code, done, len(codes))
    return done


# =====================================================================
# 组合 K 线合成（组合没有 baostock 指数K线，由成分股综合计算）
# =====================================================================
def rebuild_portfolio_kline(conn, portfolio_code: str, start: str | None = None,
                            end: str | None = None, batch: int = 500,
                            base: float = 1000.0) -> int:
    """把组合成分股合成一条组合K线（OHLCV + 五指标）写回 kline(ktype='portfolio')。

    日期轴：优先用交易日历（完整交易日序列）；日历为空则用成分股K线日期的并集。

    价格用**等权收益指数**（基准 1000 点），而不是"成分股价格的等权平均"：
    后者会被高价股主导（例如组合里同时有 1500 元的茅台和 9 元的浦发，
    涨跌几乎只反映茅台），等于权重名不副实。等权收益指数的做法是
    「当日收益 = 各成分股涨跌幅的等权平均」，再逐日累乘成点位，
    这样每只成分股对曲线的影响力才真正相等。

    每个交易日：
      * 权重 = 等权（当日停牌/无数据的成分股剔除后重新归一）
      * 点位 = 上一日点位 × 等权平均涨跌幅；OHLC 用同一缩放系数换算
      * volume/amount = 各成分股求和
      * 五指标 = aggregate_metrics（PE/PB/PS/PCF 调和加权、股息率算术加权）

    返回写入的交易日数。
    """
    codes = storage.load_constituents(conn, portfolio_code)
    if not codes:
        log.warning("%s 无成分股，无法合成组合K线", portfolio_code)
        return 0

    # 日期轴：交易日历优先；否则成分股K线日期并集
    axis = [r["date"] for r in storage.load_trade_dates(
        conn, start=start, end=end, open_only=True)]
    if not axis:
        ph = ",".join("?" * len(codes))
        axis = [r[0] for r in conn.execute(
            f"SELECT DISTINCT date FROM kline WHERE code IN ({ph}) ORDER BY date",
            codes)]
    if not axis:
        log.warning("%s 成分股无K线、日历为空，无法合成组合K线", portfolio_code)
        return 0

    ph = ",".join("?" * len(codes))
    sql = (f"SELECT code,date,open,high,low,close,volume,amount,turn,"
           f"pe_ttm,pb_mrq,ps_ttm,pcf_ncf_ttm,div_yield "
           f"FROM kline WHERE date=? AND code IN ({ph})")

    out_rows, done = [], 0
    level = base                       # 组合点位（等权收益指数）
    prev_close = None                  # 上一交易日的组合收盘（点位）
    last_seen = {}                     # {code: 该成分股最近一次收盘价}
    for d in axis:
        cons = [dict(r) for r in conn.execute(sql, [d] + codes)]
        if not cons:
            continue
        weights = equal_weights([r["code"] for r in cons])
        if not weights:
            continue

        # ---- 当日等权收益：只统计"今天有价、且昨天也有价"的成分股 ----
        pairs = [(c, r["close"], last_seen.get(c))
                 for c, r in _pairs(cons, weights)
                 if r.get("close") is not None]
        num = sum(weights[c] * (cl / pc) for c, cl, pc in pairs if pc)
        den = sum(weights[c] for c, cl, pc in pairs if pc)
        ratio = (num / den) if den > 0 else 1.0
        level = level * ratio

        # ---- OHLC：先算当日等权均价，再用系数缩放到点位 ----
        def _avg(field):
            vals = [(weights[c], r.get(field)) for c, r in _pairs(cons, weights)
                    if r.get(field) is not None]
            tot = sum(w for w, _v in vals)
            return (sum(w * v for w, v in vals) / tot) if tot else None

        raw_close = _avg("close")
        scale = (level / raw_close) if raw_close else 1.0
        close = level if raw_close is not None else prev_close or level
        op = (_avg("open") or raw_close or 0.0) * scale
        hi = (_avg("high") or raw_close or 0.0) * scale
        lo = (_avg("low") or raw_close or 0.0) * scale
        volume = sum(r.get("volume") or 0.0 for r in cons)
        amount = sum(r.get("amount") or 0.0 for r in cons)

        agg = {k: v for k, v in aggregate_metrics(cons, weights).items()
               if v is not None}
        row = {
            "date": d, "open": op, "high": hi, "low": lo, "close": close,
            "volume": volume, "amount": amount,
            "preclose": prev_close,
            "pct_chg": ((close / prev_close - 1) * 100
                        if prev_close else None),
        }
        row.update(agg)
        out_rows.append(row)
        prev_close = close
        # 只有今天有价的成分股才更新"最近收盘"（停牌时保留上次价格）
        for c, r in _pairs(cons, weights):
            if r.get("close") is not None:
                last_seen[c] = r["close"]
        done += 1
        if len(out_rows) >= batch:
            storage.upsert_kline(conn, portfolio_code, "portfolio", out_rows)
            out_rows.clear()
    if out_rows:
        storage.upsert_kline(conn, portfolio_code, "portfolio", out_rows)

    log.info("%s 组合K线合成：%d 个交易日（等权成分股 %d 只，基准 %.0f 点）",
             portfolio_code, done, len(codes), base)
    return done


def _pairs(rows, weights):
    """[(code, row)]，只含有权重的行。"""
    return [(r["code"], r) for r in rows if r["code"] in weights]


# =====================================================================
# 个股指标分位（相对自身历史）
# =====================================================================
def metric_percentile_series(conn, code: str, years: int | None = None) -> dict:
    """某标的各指标在**自身历史**内的滚动分位序列（分位已统一为"越高越贵"）。

    供前端"5 指标估值分位走势图"使用——个股即使没有评分历史（未配置权重）
    也能直接算出分位，不依赖 valuation_score 落库。
    返回 {指标: [{date, pct}, ...]}（升序，无有效分位的日期已剔除）。
    """
    win = config.windows()
    years = years or win["main"]
    rows = storage.load_kline(conn, code, fields=("date",) + METRICS)
    if not rows:
        return {m: [] for m in METRICS}
    out = {}
    for m in METRICS:
        pairs = [(r["date"], r.get(m)) for r in rows]
        pcts = _rolling_percentiles(pairs, m, years)
        out[m] = [{"date": d, "pct": p} for d, p in pcts.items() if p is not None]
    return out


def _rolling_percentiles(pairs, metric: str, years: int) -> dict:
    """单只股票、单个指标的**滚动窗口分位**序列。

    pairs: [(date, value)]（按日期升序）
    窗口: 每个日期往前 years 年
    返回 {date: 分位}，分位已按指标方向统一为"越高越贵"（股息率取 100-p）。
    无有效值/样本不足的日期为 None。全程无未来数据。

    实现：有序列表 + bisect（C 实现，插入/查询都很快），全程无未来数据。
    性能要点：窗口起点**预计算**——早期每个日期都 `strptime`+`strftime` 算一次
    cutoff，占了 70% 时间；改成一次性把日期转 date 对象并预先减好 delta 后，
    8000 行的个股从 2.5s 降到 0.15s（约 16 倍）。
    """
    delta = timedelta(days=int(365.25 * years))
    dobs = [date.fromisoformat(d) for d, _ in pairs]
    cutoffs = [x - delta for x in dobs]
    win, queue, out = [], deque(), {}
    for i, (d, v) in enumerate(pairs):
        if not _valid(metric, v):
            out[d] = None
            continue
        bisect.insort(win, v)
        queue.append((dobs[i], v))
        cutoff = cutoffs[i]
        while queue and queue[0][0] < cutoff:          # 移除窗口外的旧值
            _, ov = queue.popleft()
            j = bisect.bisect_left(win, ov)
            if j < len(win) and win[j] == ov:
                win.pop(j)
        if len(win) < MIN_OBS:                  # 样本太少，分位无意义
            out[d] = None
            continue
        p = percentile(win, v)
        out[d] = (100.0 - p) if metric in INVERTED else p
    return out


def stock_metric_percentiles(conn, code: str, years: int = 10,
                             as_of: str | None = None) -> dict:
    """个股各指标在【as_of 往前 years 年】窗口内的分位。"""
    start = _window_start(years, as_of=as_of)
    rows = storage.load_kline(conn, code, start=start, end=as_of, fields=METRICS)
    if not rows:
        return {m: None for m in METRICS}
    out = {}
    for m in METRICS:
        series = [r.get(m) for r in rows]
        cur = series[-1]
        if not _valid(m, cur):
            out[m] = None
            continue
        vals = sorted(v for v in series if _valid(m, v))
        if len(vals) < MIN_OBS:
            out[m] = None
            continue
        p = percentile(vals, cur)
        out[m] = (100.0 - p) if m in INVERTED else p
    return out


# =====================================================================
# 评分合成
# =====================================================================
def composite_score(metric_pcts: dict, weights: dict) -> dict:
    """按配置权重合成综合评分；缺失指标自动剔除并按可用权重归一。"""
    acc = total = 0.0
    parts, used = {}, {}
    for m in METRICS:
        wk = METRIC_TO_WEIGHT_KEY[m]
        w = weights.get(wk, 0.0) or 0.0
        p = metric_pcts.get(m)
        if w <= 0 or p is None:
            continue
        acc += w * p
        total += w
        parts[wk] = p
        used[wk] = w
    score = (acc / total) if total > 0 else 50.0
    return {"score": score, "parts": parts, "used_weights": used}


def signal_for(score: float) -> dict:
    """按 config.SIGNAL_BANDS 判定状态与动作。

    委托给 config.signal_of()（全系统唯一实现），只保留指标计算需要的三个字段。
    注意：这里返回的是"按当前配置"的结果，写进 valuation_score 后即为**快照**；
    展示层（网页/邮件）仍应按当前配置实时推导，见 config.signal_of() 的说明。
    """
    s = config.signal_of(score)
    return {"status": s["status"], "emoji": s["emoji"], "action": s["action"]}


def _score_row(code, ktype, date, score, score5, pcts, sig, n_used,
               pcts5: dict | None = None) -> dict:
    """组装一行 valuation_score 记录（10年分位 + 5年分位）。"""
    pcts5 = pcts5 or {}
    return {
        "code": code, "date": date, "ktype": ktype,
        "score": score, "score5": score5,
        "pct_pe": pcts.get("pe_ttm"), "pct_pb": pcts.get("pb_mrq"),
        "pct_ps": pcts.get("ps_ttm"), "pct_pcf": pcts.get("pcf_ncf_ttm"),
        "pct_dividend": pcts.get("div_yield"),
        "pct5_pe": pcts5.get("pe_ttm"), "pct5_pb": pcts5.get("pb_mrq"),
        "pct5_ps": pcts5.get("ps_ttm"), "pct5_pcf": pcts5.get("pcf_ncf_ttm"),
        "pct5_dividend": pcts5.get("div_yield"),
        "status": sig["status"], "action": sig["action"], "n_used": n_used,
    }


def _metrics_result(pcts10, pcts5, weights) -> dict:
    """把指标分位整理成对外结果结构。"""
    return {METRIC_TO_WEIGHT_KEY[m]: {
        "pct": pcts10.get(m), "pct5": pcts5.get(m),
        "weight": weights.get(METRIC_TO_WEIGHT_KEY[m], 0.0)} for m in METRICS}


# 指标 → 已落库的分位列（权重变更后"按已存分位重算评分"用，避免重读 K 线）
_PCT_COL = {"pe_ttm": "pct_pe", "pb_mrq": "pct_pb", "ps_ttm": "pct_ps",
            "pcf_ncf_ttm": "pct_pcf", "div_yield": "pct_dividend"}
_PCT5_COL = {"pe_ttm": "pct5_pe", "pb_mrq": "pct5_pb", "ps_ttm": "pct5_ps",
             "pcf_ncf_ttm": "pct5_pcf", "div_yield": "pct5_dividend"}

# 未配置权重时写入 valuation_score 的"无评分"信号：只落分位，不合成评分
_NO_SIGNAL = {"status": None, "action": None, "emoji": "❓"}


def has_weights(weights) -> bool:
    """是否配置了有效权重（至少一项 > 0）。没有权重就算不出综合评分，只存分位。"""
    return bool(weights) and sum(v or 0 for v in weights.values()) > 0


# =====================================================================
# 指数评分（方案 B）—— 当日
# =====================================================================
def index_score(conn, index_code: str, years: int | None = None,
                years_ref: int | None = None, save: bool = True,
                ktype: str = "index", ref_date: str | None = None) -> dict | None:
    """计算指数/组合的当日综合评分并落库（方案B）。

    ① 用估值日当天成分股行情算权重（指数=流通市值，组合=等权）
    ② 每只成分股算各指标自身历史分位（主窗口/参考窗口），按权重加权 → 指数指标分位
    ③ 按该指数配置权重合成评分 → 判定信号 → 写入 valuation_score

    ktype: "index" 或 "portfolio"（组合没有自己的K线，估值日由调用方传入或
           回退到最近一个交易日）；ref_date 用于指定估值日。
    """
    win = config.windows()
    years = years or win["main"]
    years_ref = years_ref or win["ref"]
    target = config.target(index_code)
    weights_cfg = target["weights"] if target else None
    name = target["name"] if target else storage.stock_name(conn, index_code)
    codes = storage.load_constituents(conn, index_code)
    if not codes:
        log.warning("%s 无成分股，跳过评分", index_code)
        return None

    if ref_date is None:
        ref_date = (storage.latest_kline_date(conn, index_code)
                    or datetime.now().strftime("%Y-%m-%d"))
    ph = ",".join("?" * len(codes))
    wrows = [dict(r) for r in conn.execute(
        f"SELECT code,close,volume,turn FROM kline WHERE date=? AND code IN ({ph})",
        [ref_date] + codes)]
    # 指数按流通市值加权，组合固定等权（见 constituent_weights 的说明）
    weights = constituent_weights(ktype, [r["code"] for r in wrows], wrows)
    if not weights:
        weights = equal_weights(codes)

    sum10 = {m: 0.0 for m in METRICS}
    sum5 = {m: 0.0 for m in METRICS}
    wsum = {m: 0.0 for m in METRICS}
    wsum5 = {m: 0.0 for m in METRICS}
    used = 0
    for code in codes:
        w = weights.get(code)
        if not w:
            continue
        # 优先复用成分股**已落库**的分位（个股先算完 → 这里直接读，省掉重算）；
        # 该日没有存档时才回退到从 kline 现算（例如库还没建过这只的历史分位）。
        stored = _stored_pcts_asof(conn, code, ref_date)
        if stored and any(v is not None for v in stored[0].values()):
            p10, p5 = stored
        else:
            p10 = stock_metric_percentiles(conn, code, years, as_of=ref_date)
            p5 = stock_metric_percentiles(conn, code, years_ref, as_of=ref_date)
        hit = False
        for m in METRICS:
            if p10.get(m) is not None:
                sum10[m] += w * p10[m]
                wsum[m] += w
                hit = True
            if p5.get(m) is not None:
                sum5[m] += w * p5[m]
                wsum5[m] += w

        used += 1 if hit else 0

    pcts = {m: (sum10[m] / wsum[m]) if wsum[m] > 0 else None for m in METRICS}
    pcts5 = {m: (sum5[m] / wsum5[m]) if wsum5[m] > 0 else None for m in METRICS}

    # 数据充分性守卫：所有指标都算不出来时，绝不能合成一个"中性 50 分"——
    # 那会变成一封"评分 50 · 正常 · 小额定投"的"一切正常"邮件。
    if used == 0 or all(v is None for v in pcts.values()):
        log.warning("%s 成分股无可用的估值数据（%d 只成分股，0 只可用），"
                    "跳过评分（不落库、不发信）", index_code, len(codes))
        return None

    if has_weights(weights_cfg):
        avail = sum(weights_cfg.get(METRIC_TO_WEIGHT_KEY[m], 0.0)
                    for m in METRICS if pcts.get(m) is not None)
        if avail < 0.5:
            # 有效指标覆盖的权重不足一半：仍出分数，但明确告警（邮件里也会提示）
            missing = [METRIC_TO_WEIGHT_KEY[m] for m in METRICS
                       if pcts.get(m) is None]
            log.warning("%s 有效指标权重覆盖仅 %.0f%%（缺失：%s），评分可信度偏低",
                        index_code, avail * 100, ", ".join(missing) or "无")
        comp = composite_score(pcts, weights_cfg)
        comp5 = composite_score(pcts5, weights_cfg)
        sig = signal_for(comp["score"])
        score, score5 = comp["score"], comp5["score"]
    else:
        # 未配置权重：只落分位，不合成综合评分（score 留空）
        avail, score, score5, sig = 0.0, None, None, _NO_SIGNAL

    result = {
        "code": index_code, "name": name, "ktype": ktype,
        "date": ref_date, "score": score, "score5": score5,
        "status": sig["status"], "emoji": sig["emoji"], "action": sig["action"],
        "metrics": _metrics_result(pcts, pcts5, weights_cfg or {}),
        "constituents_used": used, "constituents_total": len(codes),
        "weight_coverage": round(avail, 4),
        "missing_metrics": [METRIC_TO_WEIGHT_KEY[m] for m in METRICS
                            if pcts.get(m) is None],
        "scored": has_weights(weights_cfg),
    }
    if save:
        storage.upsert_scores(conn, [_score_row(
            index_code, ktype, ref_date, score, score5,
            pcts, sig, used, pcts5)])
        result["saved"] = True
    return result


def rebuild_portfolio(conn, code: str, freq: str = "M",
                      history: bool = True, full: bool = True) -> dict:
    """重算组合的**全部派生数据**：K线 → 指标 → 指标分位 → 综合评分。

    full=True（默认）时**先删掉旧数据再全量重算**：
    成分股变了，旧的组合 K 线和分位/评分都已失效，留着只会在走势图上
    混进旧成分股的结果（尤其是成分股减少、日期轴变短时）。

      ① 删除旧的组合 K 线 + 分位/评分
      ② 合成组合 K 线（OHLCV + 五指标，成分股等权聚合）写回 kline
      ③ 重建历史分位 + 评分序列（走势图用）
      ④ 重算当日评分并落库

    返回 {kline_rows, score_rows, score, constituents, deleted_kline,
          deleted_score}。
    """
    codes = storage.load_constituents(conn, code)
    if not codes:
        log.warning("%s 无成分股，跳过组合重算", code)
        return {"kline_rows": 0, "score_rows": 0, "score": None,
                "constituents": 0, "deleted_kline": 0, "deleted_score": 0}
    d_kline = d_score = 0
    if full:
        d_kline = storage.delete_kline(conn, code)
        d_score = storage.delete_scores(conn, code)
    n_kline = rebuild_portfolio_kline(conn, code)
    n_hist = rebuild_score_history(conn, code, "portfolio",
                                   freq=freq) if history else 0
    r = portfolio_score(conn, code, save=True)
    log.info("%s 组合重算完成（删除旧数据 K线 %d / 评分 %d）："
             "K线 %d 个交易日，历史评分 %d 条，当日评分 %s",
             code, d_kline, d_score, n_kline, n_hist, (r or {}).get("score"))
    return {"kline_rows": n_kline, "score_rows": n_hist,
            "score": (r or {}).get("score"), "constituents": len(codes),
            "deleted_kline": d_kline, "deleted_score": d_score}


def portfolio_score(conn, code: str, years: int | None = None,
                    years_ref: int | None = None, save: bool = True) -> dict | None:
    """自定义组合评分：算法与指数完全一致，只是成分股清单由用户自定。

    组合没有自己的 K 线，估值日取"最近一个交易日"：
      * 有交易日历 → 用最近交易日；
      * 否则回退到成分股里最新的 K 线日期。
    """
    ref_date = storage.latest_trade_date(
        conn, datetime.now().strftime("%Y-%m-%d"))
    if not ref_date:
        codes = storage.load_constituents(conn, code)
        dates = storage.latest_kline_dates(conn)
        ref_date = max((dates.get(c, "") for c in codes), default="")
    return index_score(conn, code, years=years, years_ref=years_ref,
                       save=save, ktype="portfolio",
                       ref_date=ref_date or datetime.now().strftime("%Y-%m-%d"))


# =====================================================================
# 个股评分
# =====================================================================
def stock_score(conn, code: str, years: int | None = None,
                years_ref: int | None = None, save: bool = True) -> dict | None:
    """个股当日评分/分位：自身各指标历史分位 × 配置权重。

    * 配置了权重（在 valuation_target 里）→ 分位 + 综合评分都落库；
    * 未配置权重 → 只落分位（score/score5 留空），供分位走势图使用。
    """
    win = config.windows()
    years = years or win["main"]
    years_ref = years_ref or win["ref"]
    target = config.target(code)
    weights = target["weights"] if target else None
    name = target["name"] if target else storage.stock_name(conn, code)

    ref_date = (storage.latest_kline_date(conn, code)
                or datetime.now().strftime("%Y-%m-%d"))
    p10 = stock_metric_percentiles(conn, code, years, as_of=ref_date)
    p5 = stock_metric_percentiles(conn, code, years_ref, as_of=ref_date)
    if all(v is None for v in p10.values()):
        log.warning("%s 无可用指标，跳过评分", code)
        return None

    if has_weights(weights):
        comp = composite_score(p10, weights)
        comp5 = composite_score(p5, weights)
        sig = signal_for(comp["score"])
        score, score5 = comp["score"], comp5["score"]
    else:
        score, score5, sig = None, None, _NO_SIGNAL

    result = {
        "code": code, "name": name, "ktype": "stock", "date": ref_date,
        "score": score, "score5": score5,
        "status": sig["status"], "emoji": sig["emoji"], "action": sig["action"],
        "metrics": _metrics_result(p10, p5, weights or {}),
        "scored": has_weights(weights),
    }
    if save:
        storage.upsert_scores(conn, [_score_row(
            code, "stock", ref_date, score, score5, p10, sig, 1, p5)])
        result["saved"] = True
    return result


# =====================================================================
# 历史评分重建（供评分走势图）
# =====================================================================
def _sample_dates(dates: list, freq: str) -> list:
    """按频率采样交易日序列（保留每个周期最后一个交易日）。"""
    f = freq.upper()
    if f == "D":
        return list(dates)
    keyfn = (lambda d: d[:7]) if f == "M" else _week_key
    keep, last = [], None
    for d in reversed(dates):
        k = keyfn(d)
        if k != last:
            keep.append(d)
            last = k
    return list(reversed(keep))


def _week_key(date_str: str) -> str:
    iso = datetime.strptime(date_str, "%Y-%m-%d").isocalendar()
    return f"{iso[0]}-{iso[1]:02d}"


def _weights_by_date(conn, codes, dates, chunk: int = 200) -> dict:
    """批量计算若干交易日的成分股权重 {date: {code: weight}}。

    一次 SQL 取回后内存分组计算；日期**分批**查询，
    避免 SQL 参数数量超过 SQLite 限制（默认 999 个绑定变量）。
    """
    if not dates or not codes:
        return {}
    ph_c = ",".join("?" * len(codes))
    out = {}
    for i in range(0, len(dates), chunk):
        batch = dates[i:i + chunk]
        ph_d = ",".join("?" * len(batch))
        sql = (f"SELECT date,code,close,volume,turn FROM kline "
               f"WHERE code IN ({ph_c}) AND date IN ({ph_d})")
        # 每个 chunk 查回后立即算权重并丢弃明细行：
        # 若把全部明细累积到内存，freq='D' 时 500 只 × 2500 日 ≈ 520MB，树莓派会 OOM
        buckets = {}
        for r in conn.execute(sql, list(codes) + list(batch)):
            buckets.setdefault(r["date"], []).append(dict(r))
        for d, rows in buckets.items():
            out[d] = calc_market_weights(rows)
    return out


def _stored_pcts_asof(conn, code: str, ref_date: str):
    """读某标的**在 ref_date 当天已落库**的分位（10 年/5 年），没有则返回 None。

    指数/组合聚合时用：成分股的分位刚才已经算好落库了，没必要再从 kline 原始
    数据重算一遍（10 年窗口 + 排序，上千只成分股就是几十秒）。
    """
    fields = (("date",) + tuple(_PCT_COL.values()) + tuple(_PCT5_COL.values()))
    rows = storage.load_scores(conn, code, start=ref_date, end=ref_date,
                              fields=fields)
    if not rows:
        return None
    r = rows[-1]
    p10 = {m: r.get(_PCT_COL[m]) for m in METRICS}
    p5 = {m: r.get(_PCT5_COL[m]) for m in METRICS}
    return p10, p5


def _stored_percentile_series(conn, code: str):
    """从 valuation_score 读某标的分位，整理成 ({指标:{date:pct}}, {指标:{date:pct5}})。

    供指数聚合复用个股分位——全量重建时个股先算完，指数直接读已存分位加权，
    省掉对每只成分股的重算（成分股合计上千只，省下的是大头）。
    """
    fields = (("date",) + tuple(_PCT_COL.values()) + tuple(_PCT5_COL.values()))
    p10 = {m: {} for m in METRICS}
    p5 = {m: {} for m in METRICS}
    for r in storage.load_scores(conn, code, fields=fields):
        d = r["date"]
        for m in METRICS:
            p10[m][d] = r.get(_PCT_COL[m])
            p5[m][d] = r.get(_PCT5_COL[m])
    return p10, p5


def rebuild_score_history(conn, code: str, ktype: str = "index",
                          freq: str = "M", years: int | None = None,
                          years_ref: int | None = None,
                          clean: bool = False,
                          reuse_stock_pcts: bool = False) -> int:
    """重建历史【分位 + 评分】序列并落库（供分位/评分走势图）。

    freq: 'D' 每交易日 / 'W' 每周 / 'M' 每月（默认，走势图足够且更快）

    * 分位（pct_* / pct5_*）：**所有**个股/指数都算——指数用当日成分股分位按
      流通市值加权聚合，个股直接用自身各指标的滚动窗口分位；
    * 综合评分（score / score5）：**只有配置了权重**的标的才算，未配置的留空。

    全程无未来数据（每个日期只用该日及之前的窗口）。

    clean=True 时先删除该标的已有记录再重建——**改变 freq 时务必打开**，
    否则新旧采样点混在一起（例如月频 + 日频），走势图会出现锯齿。

    返回写入的记录数。
    """
    win = config.windows()
    years = years or win["main"]
    years_ref = years_ref or win["ref"]
    is_index = (ktype == "index")
    # 组合与指数同样"由成分股聚合"：指数按流通市值、组合按等权
    is_agg = ktype in ("index", "portfolio")
    target = config.target(code)
    weights_cfg = target["weights"] if target else None
    scored = has_weights(weights_cfg)          # 有权重才合成综合评分
    if is_agg:
        codes = storage.load_constituents(conn, code)
        if not codes:
            log.warning("%s 无成分股，跳过历史分位重建", code)
            return 0
    else:
        codes = [code]

    # 日期轴：指数用指数K线；个股用自身K线
    axis = [r["date"] for r in storage.load_kline(conn, code, fields=("date",))]
    if not axis and ktype == "portfolio":
        # 组合没有自己的 K 线：用交易日历做评分日期轴
        axis = [r["date"] for r in storage.load_trade_dates(conn, open_only=True)
                if r["date"] >= (config.baostock().get("start_date", "1990-01-01"))]
    if not axis:
        log.warning("%s 无K线（组合亦无交易日历），无法重建历史评分", code)
        return 0
    sampled = _sample_dates(axis, freq)
    if not sampled:
        return 0
    if clean:
        n_del = storage.delete_scores(conn, code)
        log.info("%s 重建前清理旧评分：删除 %d 条", code, n_del)

    # 预取采样日的权重（指数按当日市值；组合等权，循环里用常数 1.0 即可，
    # 累加器会按"当日有值的成分股"自行归一）
    weight_by_date = _weights_by_date(conn, codes, sampled) if is_index else {}

    # 累加器：{date: {metric: [加权和, 权重和]}}，10 年与 5 年各一套
    acc10 = {d: {m: [0.0, 0.0] for m in METRICS} for d in sampled}
    acc5 = {d: {m: [0.0, 0.0] for m in METRICS} for d in sampled}
    used_by_date = {d: 0 for d in sampled}     # 每个采样日实际用上的成分股数

    for c in codes:
        if reuse_stock_pcts:
            # 复用个股已落库的分位（全量重建时个股刚算完，指数不必再算一遍）
            p10, p5 = _stored_percentile_series(conn, c)
        else:
            rows = storage.load_kline(conn, c, fields=("date",) + METRICS)
            if not rows:
                continue
            series = {m: [(r["date"], r.get(m)) for r in rows] for m in METRICS}
            # 该股票各指标的滚动窗口分位（10 年 / 5 年）
            p10 = {m: _rolling_percentiles(series[m], m, years) for m in METRICS}
            p5 = ({m: _rolling_percentiles(series[m], m, years_ref)
                   for m in METRICS}
                  if years_ref and years_ref != years else p10)
        for d in sampled:
            w = weight_by_date.get(d, {}).get(c) if is_index else 1.0
            if not w:
                continue
            hit = False
            for m in METRICS:
                v10 = p10[m].get(d)
                if v10 is not None:
                    slot = acc10[d][m]
                    slot[0] += w * v10
                    slot[1] += w
                    hit = True
                v5 = p5[m].get(d)
                if v5 is not None:
                    slot5 = acc5[d][m]
                    slot5[0] += w * v5
                    slot5[1] += w
            if hit:
                used_by_date[d] += 1

    # 逐采样日：合成分位（10 年 + 5 年）；有权重时再合成综合评分
    out = []
    for d in sampled:
        pcts_d, pcts5_d = {}, {}
        for m in METRICS:
            sw, wsum = acc10[d][m]
            pcts_d[m] = (sw / wsum) if wsum > 0 else None
            sw5, wsum5 = acc5[d][m]
            pcts5_d[m] = (sw5 / wsum5) if wsum5 > 0 else None
        if all(v is None for v in pcts_d.values()):
            continue
        n_used = (len(weight_by_date.get(d, {})) if is_index
                  else used_by_date.get(d, 0)) or 1
        if scored:
            comp = composite_score(pcts_d, weights_cfg)
            comp5 = composite_score(pcts5_d, weights_cfg)
            out.append(_score_row(code, ktype, d, comp["score"], comp5["score"],
                                  pcts_d, signal_for(comp["score"]), n_used,
                                  pcts5_d))
        else:
            out.append(_score_row(code, ktype, d, None, None, pcts_d,
                                  _NO_SIGNAL, n_used, pcts5_d))
    if not out:
        return 0
    n = storage.upsert_scores(conn, out)
    log.info("%s 历史重建：%d 条（频率 %s，区间 %s ~ %s，%s）",
             code, n, freq, out[0]["date"], out[-1]["date"],
             "分位+评分" if scored else "仅分位（未配置权重）")
    return n


# =====================================================================
# 分位全量重建 / 增量维护 / 权重变更后重算
# =====================================================================
def recompute_scores(conn, code: str) -> int:
    """按【已落库的分位】重算某标的的 score/score5（权重变更后调用）。

    不重读 K 线，只用 valuation_score 里已存的 pct_* 列重算，秒级完成。
    未配置权重时把 score/score5 清空（只保留分位）。
    """
    target = config.target(code)
    weights_cfg = target["weights"] if target else None
    scored = has_weights(weights_cfg)
    fields = (("date", "ktype", "n_used")
              + tuple(_PCT_COL.values()) + tuple(_PCT5_COL.values()))
    rows = storage.load_scores(conn, code, fields=fields)
    if not rows:
        return 0
    out = []
    for r in rows:
        pcts = {m: r.get(_PCT_COL[m]) for m in METRICS}
        pcts5 = {m: r.get(_PCT5_COL[m]) for m in METRICS}
        if scored:
            comp = composite_score(pcts, weights_cfg)
            comp5 = composite_score(pcts5, weights_cfg)
            sig = signal_for(comp["score"])
            score, score5 = comp["score"], comp5["score"]
        else:
            score, score5, sig = None, None, _NO_SIGNAL
        out.append(_score_row(code, r.get("ktype") or "stock", r["date"],
                              score, score5, pcts, sig, r.get("n_used"), pcts5))
    n = storage.upsert_scores(conn, out)
    log.info("%s 评分重算（按已存分位）：%d 行", code, n)
    return n


def all_percentile_codes(conn) -> list[tuple]:
    """需要落分位的全部标的 [(code, ktype)]：所有个股 + 有成分股的指数 + 组合。"""
    kts = storage.code_ktypes(conn)
    out = []
    for code, kt in sorted(kts.items()):
        if kt in ("stock", "portfolio"):
            out.append((code, kt))
        elif kt == "index" and storage.load_constituents(conn, code):
            out.append((code, kt))
    # 组合没有自己的 K 线（kline 里可能没有行），从配置里补上
    have = {c for c, _ in out}
    for t in config.targets():
        if t["ktype"] == "portfolio" and t["code"] not in have:
            out.append((t["code"], "portfolio"))
    return out


def _rebuild_worker(args) -> dict:
    """多进程工作单元：每个进程自建数据库连接，处理一个标的。

    做两件事：①（可选）全量重算该股动态股息率；② 重建其分位历史。
    顶层函数（可 pickle），供 ProcessPoolExecutor 调用。

    spawn 启动的子进程不会继承父进程的 config.use_db()，所以 db_path 必须显式传入。
    """
    code, ktype, freq, clean, dividend_full, db_path = args
    if db_path and db_path != config.DB_PATH:
        config.use_db(db_path)
    conn = storage.get_conn()
    try:
        n_dy = (fill_dividend_yield(conn, code, full=True)["rows"]
                if dividend_full else 0)
        n = rebuild_score_history(conn, code, ktype=ktype, freq=freq, clean=clean)
        return {"code": code, "ok": True, "rows": n, "div_yield": n_dy}
    except Exception as e:                          # noqa: BLE001
        log.warning("%s 重建失败：%s", code, e)
        return {"code": code, "ok": False, "rows": 0, "div_yield": 0,
                "error": str(e)}
    finally:
        conn.close()


def rebuild_all_percentiles(conn, freq: str = "D", clean: bool = True,
                            codes=None, progress=None, workers=None,
                            dividend_full: bool = True) -> dict:
    """全量重建所有个股 + 指数的分位历史（10 年 / 5 年双窗口）。

    执行顺序（有依赖）：
      ① 个股/组合：**多进程并行**（每进程自建连接），各自重算动态股息率 + 分位；
      ② 指数：等个股分位落库后，读成分股**已存分位**加权聚合（不重算）。

    * dividend_full=True 先全量重算所有个股动态股息率（无分红置 0）；
    * 综合评分只在配置了权重时计算（其余只落分位）；
    * 这是**手动**全量任务（命令行 / 网页按钮），日常由
      update_latest_percentiles 增量维护最新一天。

    workers=None 时取配置 REBUILD_WORKERS（0=按 CPU 自动）。
    """
    todo = all_percentile_codes(conn)
    if codes:
        want = set(codes)
        todo = [(c, k) for c, k in todo if c in want]
    if not todo:
        return {"codes": 0, "ok": 0, "rows": 0, "freq": freq, "workers": 0}
    stocks = [(c, k) for c, k in todo if k != "index"]
    indexes = [(c, k) for c, k in todo if k == "index"]

    if workers is None:
        workers = config.get_int("REBUILD_WORKERS", 0) or 0
    if workers <= 0:
        workers = max(1, min(4, (os.cpu_count() or 2) - 1))
    workers = max(1, min(workers, len(stocks) or 1))

    total = ok = done = 0
    t0 = datetime.now()
    db_path = config.DB_PATH
    tasks = [(c, k, freq, clean, dividend_full, db_path) for c, k in stocks]

    if workers > 1 and len(tasks) > 1:
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing as mp
        import threading as _threading
        # 单线程进程（命令行）用 fork：最快，且不用重新导入模块；
        # 多线程进程（web 后台线程）用 spawn：避免 fork 继承到别的线程持有的锁而死锁。
        try:
            ctx = mp.get_context("fork" if _threading.active_count() == 1
                                 else "spawn")
        except ValueError:                          # 平台不支持 fork
            ctx = mp.get_context()
        log.info("分位重建：%d 个个股/组合，%d 进程并行（%s）",
                 len(tasks), workers, ctx.get_start_method())
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            for res in ex.map(_rebuild_worker, tasks, chunksize=2):
                done += 1
                total += res["rows"]
                ok += 1 if res["ok"] else 0
                if progress and (done % 20 == 0 or done == len(tasks)):
                    progress(done, len(todo))
    else:
        log.info("分位重建：%d 个个股/组合，单进程", len(tasks))
        for t in tasks:
            res = _rebuild_worker(t)
            done += 1
            total += res["rows"]
            ok += 1 if res["ok"] else 0
            if progress and (done % 20 == 0 or done == len(tasks)):
                progress(done, len(todo))

    # ② 指数：读成分股已存分位做加权聚合（个股分位已在上一步写好）
    log.info("指数分位聚合：%d 个", len(indexes))
    for c, k in indexes:
        try:
            total += rebuild_score_history(conn, c, ktype=k, freq=freq,
                                           clean=clean, reuse_stock_pcts=True)
            ok += 1
        except Exception as e:                      # noqa: BLE001
            log.warning("%s 指数分位聚合失败：%s", c, e)
        done += 1
        if progress:
            progress(done, len(todo))

    log.info("全量分位重建完成：%d/%d 个标的，%d 行，%d 进程，耗时 %.1fs（频率 %s）",
             ok, len(todo), total, workers,
             (datetime.now() - t0).total_seconds(), freq)
    return {"codes": len(todo), "ok": ok, "rows": total, "freq": freq,
            "workers": workers}


def update_latest_percentiles(conn) -> int:
    """为所有个股/指数补最新一天的分位/评分（缺才补，幂等）。

    供「计算指标」任务日常维护：分位历史由 rebuild_all_percentiles 全量建，
    这里只保证最新交易日有数据。已经是最新一天的标的直接跳过（重复跑很快）。
    组合没有自己的 K 线，由 run() 里的 portfolio_score 负责。
    """
    kts = storage.code_ktypes(conn)
    kd = storage.latest_kline_dates(conn)
    sd = storage.latest_score_dates(conn)
    n = 0
    for code, ref in sorted(kd.items()):
        if sd.get(code) == ref:
            continue                               # 最新交易日已有记录
        kt = kts.get(code)
        try:
            if kt == "stock":
                r = stock_score(conn, code)
            elif kt == "index":
                if not storage.load_constituents(conn, code):
                    continue
                r = index_score(conn, code)
            else:
                continue
        except Exception as e:                     # noqa: BLE001
            log.warning("%s 最新分位更新失败：%s", code, e)
            continue
        if r:
            n += 1
    if n:
        log.info("最新一天分位/评分补充：%d 个标的", n)
    return n


# =====================================================================
# 动态股息率（离线计算）
# =====================================================================
#: 股息率变化的日期早于"最近这么多天"才算**历史性变化**（需要重算历史分位）。
#: 日常增量只会重写最近 KLINE_OVERLAP_DAYS(10) 天，那种变化由"补最新一天"覆盖，
#: 没必要为它整条重建；只有真的动到历史（全量重拉、close_raw 补齐）才重建。
DIVIDEND_HISTORICAL_DAYS = 30


def _div_change_is_historical(conn, code: str, first_changed: str | None) -> bool:
    """这次股息率变化是否触及历史（而不是最近几天）。"""
    if not first_changed:
        return False
    last = storage.latest_kline_date(conn, code)
    if not last:
        return False
    cutoff = (date.fromisoformat(last)
              - timedelta(days=DIVIDEND_HISTORICAL_DAYS)).isoformat()
    return first_changed < cutoff


def fill_dividend_yields(conn, codes=None, full: bool = False) -> int:
    """批量计算个股的动态股息率（离线：读 kline.close_raw + dividend 表）。

    full=False（日常增量）：只处理**既有空值、又有 close_raw** 的个股——
        K 线被重写后 div_yield 会置空，这里据此精确补算。
        加 close_raw 过滤是因为：没有 close_raw 就**根本算不出来**
        （全库一度只有 6% 的行有 close_raw），不过滤会白遍历 700 多只、
        加载它们全部 K 线，几秒起步却一行都补不上。
    full=True（全量）：处理**全部**个股、全历史重算（全量重建用）。

    **历史性变化会打 `pct_dirty` 标记**，让「计算指标」下次重建该标的的历史分位
    —— div_yield 变了，历史股息率分位就全变了，而覆盖率判据发现不了这种变化。

    返回更新的行数。
    """
    if codes is None:
        if full:
            rows = conn.execute(
                "SELECT DISTINCT code FROM kline WHERE ktype='stock'").fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT code FROM kline WHERE ktype='stock' "
                "AND div_yield IS NULL AND close_raw IS NOT NULL").fetchall()
        codes = [r[0] for r in rows]
    total = 0
    for code in codes:
        try:
            r = fill_dividend_yield(conn, code, full=full)
            total += r["rows"]
            # 股息率的历史值变了 → 历史分位作废，交给下次 compute 重建
            if _div_change_is_historical(conn, code, r["first"]):
                storage.mark_history_dirty(
                    conn, code, f"股息率历史变化（{r['first']} 起 {r['rows']} 行）")
        except Exception as e:                      # noqa: BLE001
            log.warning("%s 股息率计算失败：%s", code, e)
    return total


def fill_dividend_yield(conn, code: str, full: bool = False) -> dict:
    """计算并写回某只个股的动态股息率。返回 {rows, first}。

    div_yield(某日) = 该日及之前 N 天内每股现金分红之和 / 当日不复权收盘价 × 100
    N = config 的 DIVIDEND_LOOKBACK_DAYS（默认 365，即近 12 个月）

    full=False 只补空值行；full=True 全历史重算。
    **没有分红记录 → 股息率按 0 处理**（缺分红/无分红都置 0，不再跳过）。
    close_raw 缺失（停牌等拿不到不复权价）→ 该行保留原值不动。

    first = 本次改动的最早日期（没改动则 None）。调用方用它判断这次变化是否
    触及历史、要不要把该标的的历史分位标记为需重算。
    """
    lookback = int(config.baostock().get("dividend_lookback_days", 365))
    krows = storage.load_kline(conn, code, fields=("date", "close_raw", "div_yield"))
    if not krows:
        return {"rows": 0, "first": None}

    # 分红按除息日升序；窗口 [d-N, d] 用双指针维护和（避免每行都线性扫描分红表）
    divs = sorted(storage.load_dividends(conn, code), key=lambda x: x["ex_date"])
    delta = timedelta(days=lookback)
    outs = []
    hi = lo_i = 0
    cash = 0.0
    for r in krows:
        if not full and r["div_yield"] is not None:
            continue
        d = r["date"]
        uc = r["close_raw"]
        if not uc:                                    # 停牌等无不复权收盘价
            continue
        d_obj = date.fromisoformat(d)
        lo_date = (d_obj - delta).isoformat()
        while hi < len(divs) and divs[hi]["ex_date"] <= d:
            cash += divs[hi]["cash_ps"] or 0.0
            hi += 1
        while lo_i < hi and divs[lo_i]["ex_date"] < lo_date:
            cash -= divs[lo_i]["cash_ps"] or 0.0
            lo_i += 1
        new_v = round(cash / uc * 100, 4)
        old_v = r["div_yield"]
        if old_v is not None and abs(old_v - new_v) < 1e-9:
            continue                                  # 值没变就不写（省写入）
        outs.append({"date": d, "div_yield": new_v})
    if not outs:
        return {"rows": 0, "first": None}
    n = storage.update_valuation_fields(conn, code, outs)
    return {"rows": n, "first": outs[0]["date"] if n else None}


def compute_target(conn, code: str, ktype: str, full: bool = False) -> dict:
    """算一个标的：股息率 →（指数估值聚合 / 组合K线合成）→ 当日分位与评分。

    **纯离线**（只读 kline / dividend），不联网。取数由 data_fetcher 负责，
    这里只负责算 —— 手动「拉取单只标的」的编排见 pipeline.sync_target。

    与 compute_all 的分工：那个是"全库批量补"，这个是"单个标的即时算"。
    """
    out = {}
    if ktype == "index":
        out["dividend_yield_filled"] = fill_dividend_yields(
            conn, storage.load_constituents(conn, code))
        rebuild_index_valuation(conn, code, only_missing=not full)
        r = index_score(conn, code)
    elif ktype == "portfolio":
        out["dividend_yield_filled"] = fill_dividend_yields(
            conn, storage.load_constituents(conn, code))
        out["kline_synth"] = rebuild_portfolio_kline(conn, code)
        r = portfolio_score(conn, code)
    else:
        fr = fill_dividend_yield(conn, code)
        out["dividend_yield_filled"] = fr["rows"]
        if _div_change_is_historical(conn, code, fr["first"]):
            storage.mark_history_dirty(
                conn, code, f"股息率历史变化（{fr['first']} 起）")
        r = stock_score(conn, code)
    out["score"] = (r or {}).get("score")
    out["status"] = (r or {}).get("status")
    return out


# =====================================================================
# 编排
# =====================================================================
#: 判定"分位历史已完整"的覆盖率阈值：评分行数 / K线行数
HISTORY_COMPLETE_RATIO = 0.9


def history_gap_codes(conn) -> list[tuple]:
    """找出**分位历史需要重算**的标的 [(code, ktype)]。

    判据（满足其一就重算）：
      * 打了 `pct_dirty` 标记 —— 输入数据变了（例如 close_raw 补齐后 div_yield
        才算得出来），历史分位整条作废。**这种变化覆盖率判据看不出来**
        （K 线行数没变），所以必须由改数据的那一方显式标记；
      * 没打过 `pct_history` 标记，且「评分行数 / K线行数」低于
        HISTORY_COMPLETE_RATIO —— 例如刚被拉进个股池、只在最新一天算过一次。

    为什么不用「分位字段 IS NULL」当判据：早期交易日的窗口长度不够，
    分位**天然算不出来**（永远是 NULL）。拿它当"缺失"会让每轮任务都去重试
    这几十万天，永远补不上，纯烧 CPU。
    """
    built = storage.history_built_codes(conn)
    dirty = storage.dirty_codes(conn)
    cov = storage.score_kline_coverage(conn)
    out = []
    for code, ktype in all_percentile_codes(conn):
        c = cov.get(code)
        if code in dirty:
            out.append((code, ktype))            # 输入变了 → 无条件重算
            continue
        if code in built:
            continue
        if not c or not c["kline"]:
            continue
        if c["scores"] / c["kline"] < HISTORY_COMPLETE_RATIO:
            out.append((code, ktype))
    return out


def _history_worker(args) -> dict:
    """多进程工作单元：整条重建某标的的分位/评分历史，并打上"已补齐"标记。

    顶层函数（可 pickle），供 ProcessPoolExecutor 调用。
    """
    code, ktype, freq, db_path = args
    if db_path and db_path != config.DB_PATH:
        config.use_db(db_path)
    conn = storage.get_conn()
    try:
        n = rebuild_score_history(conn, code, ktype=ktype, freq=freq, clean=True)
        last = storage.latest_kline_date(conn, code)
        storage.mark_history_built(conn, code, last_date=last, row_count=n)
        # 重算完就把"需重算"标记清掉，否则每轮都会再重建一次
        storage.clear_history_dirty(conn, code)
        return {"code": code, "ok": True, "rows": n}
    except Exception as e:                          # noqa: BLE001
        log.warning("%s 历史分位重建失败：%s", code, e)
        return {"code": code, "ok": False, "rows": 0, "error": str(e)}
    finally:
        conn.close()


def _agg_worker(args) -> dict:
    """多进程工作单元：算一个指数/组合的当日分位与评分（落库）。

    **必须等个股分位落库之后**才能跑 —— 它读的是成分股已存的分位。
    """
    code, ktype, db_path = args
    if db_path and db_path != config.DB_PATH:
        config.use_db(db_path)
    conn = storage.get_conn()
    try:
        r = index_score(conn, code, save=True, ktype=ktype)
        return {"code": code, "ok": r is not None,
                "score": (r or {}).get("score")}
    except Exception as e:                          # noqa: BLE001
        log.warning("%s 分位/评分计算失败：%s", code, e)
        return {"code": code, "ok": False, "error": str(e)}
    finally:
        conn.close()


def resolve_workers(n_tasks: int, workers: int | None = None) -> int:
    """并发进程数：取 REBUILD_WORKERS（0=自动 = min(3, 核数-1)），并夹到任务数。"""
    if workers is None:
        workers = config.get_int("REBUILD_WORKERS", 0) or 0
    if workers <= 0:
        # 树莓派 3B+ 是 4 核：留 1 核给 web/调度器，避免跑批时页面卡死
        workers = max(1, min(3, (os.cpu_count() or 2) - 1))
    return max(1, min(workers, n_tasks or 1))


def _run_parallel(tasks: list, worker, workers: int) -> tuple:
    """把 tasks 交给多进程跑（workers<=1 或只有 1 个任务时串行）。返回 (ok, rows)。"""
    ok = rows = 0
    if not tasks:
        return 0, 0
    # 任务太少就别开进程池：进程启动（web 线程里是 spawn，每个要重新 import 模块）
    # 本身比这点活儿还贵。每个进程至少要摊到 2 个任务才划算。
    if workers > 1 and len(tasks) < 2 * workers:
        workers = 1
    if workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing as mp
        import threading as _threading
        # 单线程进程（命令行）用 fork：最快；多线程进程（web 后台线程）用 spawn：
        # 避免 fork 继承到别的线程持有的锁而死锁。
        try:
            ctx = mp.get_context("fork" if _threading.active_count() == 1
                                 else "spawn")
        except ValueError:                          # 平台不支持 fork
            ctx = mp.get_context()
        log.info("并发计算 %d 个任务，%d 进程（%s）",
                 len(tasks), workers, ctx.get_start_method())
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            for res in ex.map(worker, tasks, chunksize=2):
                ok += 1 if res.get("ok") else 0
                rows += res.get("rows") or 0
    else:
        log.info("串行计算 %d 个任务（workers=%d）", len(tasks), workers)
        for t in tasks:
            res = worker(t)
            ok += 1 if res.get("ok") else 0
            rows += res.get("rows") or 0
    return ok, rows


def compute_all(conn, save_score: bool = True, rebuild_valuation: bool = True,
                workers: int | None = None):
    """「计算指标」任务：检查所有个股/指数/组合的分位，缺什么补什么。

    阶段之间**有依赖，顺序不能换**：

      ① 补股息率        —— 个股动态股息率（只补空的）
      ② 个股分位（并发）—— 历史残缺的整条重建；历史完整的不用管（最新一天由 ⑤ 兜底）
      ③ 组合 K 线落库   —— 组合的分位要靠它当日期轴，**必须先建 K 线再算分位**
      ④ 指数/组合聚合（并发）—— 读 ② 已落库的个股分位加权，不重算
      ⑤ 兜底补最新      —— 所有标的的最新交易日；已经是最新的直接跳过

    纯离线计算（读 kline/dividend），不联网。返回统计字典
    （含 `fingerprint`，供调用方判断"数据没变、也没留待办"时下次早退）。
    """
    t0 = datetime.now()
    # 取数侧的数据指纹：本轮的"输入版本"。既用于判断组合K线要不要重合成，
    # 也回传给调用方，作为"这轮跑完是干净的"的凭据。
    fp = storage.data_fingerprint(conn)
    out = {"ok": True, "started_at": t0.strftime("%Y-%m-%d %H:%M:%S"),
           "fingerprint": fp}

    # ① 股息率（个股，只补空值；历史性变化会打 pct_dirty 让 ② 重建）
    out["dividend_yield_filled"] = fill_dividend_yields(conn)

    # ② 个股：只有历史残缺 / 被标脏的才需要整条重建（实测 0.2~0.5s/只）
    gaps = history_gap_codes(conn)
    stock_gaps = [(c, k) for c, k in gaps if k == "stock"]
    agg_gaps = [(c, k) for c, k in gaps if k != "stock"]

    # ③ 组合 K 线：必须在组合分位之前落库（组合用自身 K 线做日期轴）。
    # 按需重建：输入数据指纹没变、且组合 K 线已经到最新交易日，就跳过 ——
    # 否则每轮都要重写 2800+1700 行，一天几十次是白磨 SD 卡。
    n_port, port_skipped = 0, 0
    port_end = storage.latest_trade_date(conn, on_or_before=date.today().isoformat())
    for t in config.targets():
        if t["ktype"] != "portfolio":
            continue
        code = t["code"]
        last = storage.latest_kline_date(conn, code)
        fresh = bool(last and port_end and last >= port_end)
        if fresh and storage.get_portfolio_fingerprint(conn, code) == fp:
            port_skipped += 1
            continue
        try:
            n_port += rebuild_portfolio_kline(conn, code)
            storage.set_portfolio_fingerprint(conn, code, fp)
        except Exception as e:                      # noqa: BLE001
            log.warning("%s 组合K线合成失败：%s", code, e)
    out["portfolio_rows"] = n_port
    out["portfolio_skipped"] = port_skipped

    # 指数估值聚合（写回指数K线的五指标绝对值，供估值列/图表用）
    if rebuild_valuation:
        for t in config.targets():
            if t["ktype"] == "index":
                try:
                    rebuild_index_valuation(conn, t["code"], only_missing=True)
                except Exception as e:              # noqa: BLE001
                    log.warning("%s 指数估值聚合失败：%s", t["code"], e)

    # ② 个股历史补齐（并发；组合的历史放在 ③ 之后一起补）
    w = resolve_workers(len(stock_gaps) + len(agg_gaps), workers)
    db_path = config.DB_PATH
    ok_s, rows_s = _run_parallel(
        [(c, k, "D", db_path) for c, k in stock_gaps], _history_worker, w)
    out["history_stocks"] = len(stock_gaps)
    out["history_stock_rows"] = rows_s
    out["history_stock_ok"] = ok_s

    # 组合历史：K 线已落库，可以补了
    ok_p, rows_p = _run_parallel(
        [(c, k, "D", db_path) for c, k in agg_gaps], _history_worker, w)
    out["history_agg"] = len(agg_gaps)
    out["history_agg_rows"] = rows_p

    # ④ 指数 / 组合：聚合成分股已存分位 → 落综合评分（并发）
    agg_targets = [(t["code"], t["ktype"]) for t in config.targets()
                   if t["ktype"] in ("index", "portfolio")]
    ok_a, _ = _run_parallel(
        [(c, k, db_path) for c, k in agg_targets], _agg_worker,
        resolve_workers(len(agg_targets), workers))
    out["agg_ok"] = ok_a
    out["agg_total"] = len(agg_targets)

    # ⑤ 兜底：所有标的的最新交易日（缺才补，幂等）
    out["percentiles"] = update_latest_percentiles(conn)

    # 记录"当前已最新的标的数"供日志/结果展示
    out["lagging"] = len(history_gap_codes(conn))
    out["elapsed"] = round((datetime.now() - t0).total_seconds(), 1)
    return out


def run(conn, rebuild_valuation: bool = True, save_score: bool = True,
        history_freq: str | None = None):
    """计算全部估值目标：指数估值聚合 + 组合K线合成 + 当日评分（落库）。

    对所有**配置了权重**的目标（valuation_target 里的 index/portfolio/stock）：
      指数   → 聚合指数五指标 + index_score
      组合   → 合成组合K线(OHLCV+五指标) + portfolio_score
      个股   → stock_score（未配置权重则跳过）

    history_freq: 传入 'D'/'W'/'M' 时，额外重建各标的的历史评分序列
                  （供评分走势图；首次建库或补历史时用，日常 cron 无需传）。
    """
    results = []
    for t in config.targets():
        code, ktype = t["code"], t["ktype"]
        try:
            r = None
            if ktype == "index":
                if rebuild_valuation:
                    rebuild_index_valuation(conn, code, only_missing=True)
                if history_freq:
                    rebuild_score_history(conn, code, ktype, freq=history_freq)
                r = index_score(conn, code, save=save_score)
            elif ktype == "portfolio":
                rebuild_portfolio_kline(conn, code)
                if history_freq:
                    rebuild_score_history(conn, code, ktype, freq=history_freq)
                r = portfolio_score(conn, code, save=save_score)
            else:  # stock（已配置权重的个股）
                if history_freq:
                    rebuild_score_history(conn, code, ktype, freq=history_freq)
                r = stock_score(conn, code, save=save_score)
            if r:
                results.append(r)
                if r.get("score") is None:
                    log.info("%s 仅落分位（未配置权重）| 成分股 %d/%d",
                             r["name"], r.get("constituents_used", 0),
                             r.get("constituents_total", 0))
                else:
                    log.info("%s 评分 %.1f (%s%s) 5年 %.1f | 成分股 %d/%d",
                             r["name"], r["score"], r["emoji"], r["status"],
                             r.get("score5") or 0, r.get("constituents_used", 0),
                             r.get("constituents_total", 0))
        except Exception as e:                      # noqa: BLE001
            log.exception("%s 计算失败: %s", code, e)
    return results
