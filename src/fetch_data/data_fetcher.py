# -*- coding: utf-8 -*-
"""baostock 数据抓取：K线（含估值）、成分股、分红、动态股息率。

数据流
------
    指数成分股（baostock）
        ↓
    成分股 K 线（前复权 + peTTM/pbMRQ/psTTM/pcfNcfTTM）
    指数 K 线（前复权）
    成分股分红（按年查询）
        ↓
    动态股息率 = 近 365 天每股现金分红之和 / 当日【未复权】收盘价 × 100
        ↓
    写入 SQLite：kline / index_constituent / dividend / stock_basic / sync_state

为什么股息率要用未复权价
------------------------
K 线统一存前复权价（历史价格已按分红送股调整过），若直接拿分红除以复权价，
历史股息率会被系统性高估。因此额外取一次不复权（adjustflag=3）的收盘价，
仅在内存中参与计算，不入库。

增量与断点续传
--------------
* 每个标的的 K 线起点由 storage.latest_kline_date() 决定（无数据→配置起点，
  有数据→最新日期+1 天）；
* 分红按年份增量（sync_state.dtype='dividend' 记录已覆盖年份）；
* 每个标的处理完立即写 sync_state，中断后重跑可跳过已完成部分；
* --full 可强制全量重拉。

用法
----
    python3 -m src.fetch_data.data_fetcher                 # 自动增量
    python3 -m src.fetch_data.data_fetcher --full          # 全量重拉
    python3 -m src.fetch_data.data_fetcher --limit 5       # 只处理 5 只(调试)
    python3 -m src.fetch_data.data_fetcher --only kline    # 只同步 K 线
"""

import argparse
import logging
import os
import socket
import sys
import time
from datetime import datetime, timedelta

import baostock as bs

_HERE = os.path.dirname(os.path.abspath(__file__))
if __package__ in (None, ""):              # 支持 python src/fetch_data/data_fetcher.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

from src.config import config            # noqa: E402
from src.storage import storage          # noqa: E402

log = logging.getLogger("fetch")


# =====================================================================
# baostock 字段映射
# =====================================================================
# K 线查询字段（含四个估值指标）
KLINE_FIELDS = ("date,open,high,low,close,preclose,volume,amount,"
                "turn,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST")

# baostock 字段名 → 数据库字段名
FIELD_MAP = {
    "date": "date", "open": "open", "high": "high", "low": "low",
    "close": "close", "preclose": "preclose", "volume": "volume",
    "amount": "amount", "turn": "turn", "pctChg": "pct_chg",
    "peTTM": "pe_ttm", "pbMRQ": "pb_mrq", "psTTM": "ps_ttm",
    "pcfNcfTTM": "pcf_ncf_ttm", "isST": "is_st",
}

# =====================================================================
# 复权方式（固定值，**不**做成配置项）
# =====================================================================
# 前复权：价格序列连续，分位/涨跌/评分才有可比性。改成后复权或不复权会让
# 除权日前后的指标口径错乱，因此写死在这里，设置页不再暴露该开关。
ADJUST_KLINE = "2"      # 日线 close
ADJUST_RAW = "3"        # close_raw（不复权），仅用于股息率计算

# 指数代码 → baostock 成分股查询函数名（科创50 无接口，需手工维护）
CONSTITUENT_API = {
    "sh.000300": "query_hs300_stocks",     # 沪深300
    "sh.000905": "query_zz500_stocks",     # 中证500
    "sh.000016": "query_sz50_stocks",      # 上证50
}

# 大盘指数（仅行情展示，不算估值/不参与评分）：随每日同步增量拉取
MARKET_INDEXES = ("sh.000001",)            # 上证指数


# 只有这些异常才值得重试（baostock 把网络故障包装成 RuntimeError/OSError）
_NETWORK_ERRORS = (OSError, TimeoutError, RuntimeError, ConnectionError)

# K 线增量拉取的回退天数：已入库日期也要重新拉一小段
#   ① 盘中跑批会把当日盘中快照写进库，收盘后必须用最终收盘价覆盖；
#   ② 除权除息后 baostock 的前复权历史会整体变化，需要重叠重拉修正。
# 重拉是幂等的（INSERT OR REPLACE），代价可忽略。
KLINE_OVERLAP_DAYS = 10


# =====================================================================
# 会话：登录 / 重试 / 限流 / 结果解析
# =====================================================================
class BaostockSession:
    """baostock 查询会话（上下文管理器）。

    所有查询统一走 _call()，带查询间隔与失败重试。
    """

    # 连续失败多少次后重连 / 熔断退出
    RECONNECT_AFTER = 5
    ABORT_AFTER = 12

    def __init__(self, cfg=None):
        self.cfg = cfg or config.baostock()
        self._logged = False
        self._fail_streak = 0

    # ---------- 生命周期 ----------
    def login(self):
        if self._logged:
            return
        # baostock 底层是裸 socket，没有超时会永久挂起（历史上出现过卡死），
        # 这里把配置里的 BAOSTOCK_TIMEOUT 真正用起来。
        timeout = int(self.cfg.get("timeout", 60) or 60)
        socket.setdefaulttimeout(timeout)
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
        self._logged = True
        self._fail_streak = 0
        log.info("baostock 登录成功（socket 超时 %ds）", timeout)

    def reconnect(self):
        """重新登录（会话失效时用）。"""
        log.warning("baostock 会话疑似失效，尝试重新登录…")
        self.logout()
        self.login()

    def logout(self):
        if self._logged:
            try:
                bs.logout()
            finally:
                self._logged = False

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.logout()
        return False

    # ---------- 底层：取行 + 重试 ----------
    @staticmethod
    def _rows(rs, what=""):
        """把 baostock ResultData 转成 list[dict]，失败抛异常。"""
        if rs.error_code != "0":
            raise RuntimeError(f"{what} 查询失败: {rs.error_msg}")
        fields = rs.fields
        out = []
        while rs.next():
            out.append(dict(zip(fields, rs.get_row_data())))
        return out

    def _call(self, fn, *args, what="", **kwargs):
        """带重试、限流、失败熔断的查询包装。

        失败分级处理（避免会话失效时"每只股票都白重试 3 次"空跑数小时）：
          * 单次查询最多重试 retry 次（只重试网络/IO 类异常，编程错误直接抛）；
          * 连续失败达 RECONNECT_AFTER 次 → 重新登录一次；
          * 连续失败达 ABORT_AFTER 次 → 直接抛错终止本次同步（熔断）。
        """
        retry = max(1, int(self.cfg.get("retry", 3)))
        sleep = float(self.cfg.get("sleep", 0.2))
        last_err = None
        for attempt in range(1, retry + 1):
            try:
                if sleep:
                    time.sleep(sleep)
                out = self._rows(fn(*args, **kwargs), what=what)
                self._fail_streak = 0
                return out
            except _NETWORK_ERRORS as e:
                last_err = e
                self._fail_streak += 1
                log.warning("%s 第 %d/%d 次失败（连续 %d 次）: %s",
                            what or "查询", attempt, retry, self._fail_streak, e)
                if self._fail_streak >= self.ABORT_AFTER:
                    raise RuntimeError(
                        f"{what or '查询'} 连续失败 {self._fail_streak} 次，"
                        f"判定数据源不可用，终止本次同步（可稍后重跑续传）") from e
                if self._fail_streak == self.RECONNECT_AFTER:
                    try:
                        self.reconnect()
                    except Exception as re_:            # noqa: BLE001
                        log.error("重新登录失败：%s", re_)
                if attempt < retry:
                    time.sleep(sleep * attempt * 3)     # 退避
            # 非网络类异常（TypeError/KeyError 等编程错误）不重试，直接上抛
        raise RuntimeError(f"{what or '查询'} 重试 {retry} 次仍失败: {last_err}")

    # ---------- 业务查询 ----------
    def kline(self, code, start, end):
        """日 K 线（含估值指标），固定前复权。返回数据库字段格式的 list[dict]。"""
        rows = self._call(bs.query_history_k_data_plus, code, KLINE_FIELDS,
                          start_date=start, end_date=end, frequency="d",
                          adjustflag=ADJUST_KLINE, what=f"K线 {code}")
        return [self._to_kline_row(r) for r in rows]

    @staticmethod
    def _to_kline_row(r):
        """baostock 行 → 数据库字段行（空串转 None）。"""
        out = {}
        for src, dst in FIELD_MAP.items():
            v = r.get(src)
            out[dst] = None if v == "" else v
        return out

    def unadjusted_close(self, code, start, end):
        """未复权收盘价 {date: close}，用于动态股息率计算（不入库）。"""
        rows = self._call(bs.query_history_k_data_plus, code, "date,close",
                          start_date=start, end_date=end, frequency="d",
                          adjustflag=ADJUST_RAW, what=f"未复权价 {code}")
        out = {}
        for r in rows:
            c = r.get("close")
            if c not in (None, ""):
                try:
                    out[r["date"]] = float(c)
                except (TypeError, ValueError):
                    continue
        return out

    def trade_dates(self, start, end):
        """交易日历 [(date, is_trading_day)]。"""
        rows = self._call(bs.query_trade_dates, start_date=start, end_date=end,
                          what="交易日历")
        return [(r["calendar_date"], r["is_trading_day"] == "1") for r in rows]

    def constituents(self, index_code):
        """指数成分股 [{code, name}]；无对应接口时返回 None。"""
        fn_name = CONSTITUENT_API.get(index_code)
        if not fn_name:
            return None
        rows = self._call(getattr(bs, fn_name), what=f"成分股 {index_code}")
        out = []
        for r in rows:
            code = r.get("code")
            if code:
                out.append({"code": code, "name": r.get("code_name", "")})
        return out

    def dividends(self, code, year):
        """某年分红记录（yearType=report 按分红预案年份）。"""
        rows = self._call(bs.query_dividend_data, code=code, year=str(year),
                          yearType="report", what=f"分红 {code} {year}")
        out = []
        for r in rows:
            ex_date = (r.get("dividOperateDate") or r.get("dividPayDate")
                       or r.get("dividRegistDate"))
            if not ex_date:
                continue
            out.append({
                "ex_date": ex_date,
                "cash_ps": r.get("dividCashPsBeforeTax") or None,   # 每股税前现金
                "stock_ps": r.get("dividStocksPs") or None,         # 每股送股
            })
        return out

    def stock_basics(self, code=None):
        """证券基本资料 [{code, name, listed_date}]。

        code 留空 = 一次批量查全市场；传 code 则只查这一只（新增成分股时用）。
        baostock 字段：code / code_name / ipoDate / outDate / type / status。
        """
        rows = (self._call(bs.query_stock_basic, code=code, what=f"证券基本资料 {code}")
                if code else self._call(bs.query_stock_basic, what="证券基本资料"))
        out = []
        for r in rows:
            code = r.get("code")
            if not code:
                continue
            out.append({"code": code,
                        "name": (r.get("code_name") or "").strip(),
                        "listed_date": r.get("ipoDate") or None})
        return out

    def stock_industries(self, code=None):
        """股票行业分类 [{code, industry}]（code 留空=一次批量查全市场）。"""
        rows = (self._call(bs.query_stock_industry, code=code, what=f"行业分类 {code}")
                if code else self._call(bs.query_stock_industry, what="行业分类"))
        out = []
        for r in rows:
            code = r.get("code")
            if not code:
                continue
            out.append({"code": code,
                        "industry": (r.get("industry") or "").strip() or None})
        return out



# =====================================================================
# 工具函数
# =====================================================================
def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _next_day(date_str):
    return _shift_date(date_str, 1)


def _shift_date(date_str, days):
    return (datetime.strptime(date_str, "%Y-%m-%d")
            + timedelta(days=days)).strftime("%Y-%m-%d")


def _kline_start(conn, code, full=False):
    """K 线增量起点：全量→配置起点；已有数据→最新日期回退若干天（重叠重拉）。

    为什么不是"最新日期+1"：那样已入库的日期永远不会被重新请求，会导致
      ① 盘中跑批写入的当日盘中快照再也无法被收盘价覆盖（当天估值/评分全错）；
      ② 除权除息后前复权历史整体变化，而库里旧口径的数据永远不更新。
    重叠重拉是幂等的（upsert 用 INSERT OR REPLACE），代价只有几天数据。
    """
    if not full:
        last = storage.latest_kline_date(conn, code)
        if last:
            start = _shift_date(last, -KLINE_OVERLAP_DAYS)
            cfg_start = config.baostock().get("start_date", "1990-01-01")
            return max(start, cfg_start)
    return config.baostock().get("start_date", "1990-01-01")


# =====================================================================
# 同步：标的元信息
# =====================================================================
def sync_targets(conn, sess, targets):
    """把估值目标（指数/个股）写入 stock_basic。"""
    rows = []
    for t in targets:
        market = t["code"].split(".")[0] if "." in t["code"] else ""
        rows.append({"code": t["code"], "name": t["name"], "ktype": t["ktype"],
                     "market": market})
    n = storage.upsert_stock_basic(conn, rows)
    log.info("标的元信息：写入 %d 条（来自估值目标配置）", n)
    return n


def sync_stock_basics(conn, sess) -> int:
    """补全 stock_basic 的**行业**与**上市日期**（各一次批量查询全市场）。

    只是补充字段，用定点 UPDATE（不会覆盖已有的 name/ktype/market）。
    """
    len_b = len_i = 0
    merged: dict[str, dict] = {}
    try:
        basics = sess.stock_basics()
        len_b = len(basics)
        for r in basics:
            e = merged.setdefault(r["code"], {"code": r["code"]})
            if r.get("listed_date"):
                e["listed_date"] = r["listed_date"]
    except Exception as e:                          # noqa: BLE001
        log.warning("证券基本资料（上市日期）拉取失败：%s", e)
    try:
        inds = sess.stock_industries()
        len_i = len(inds)
        for r in inds:
            e = merged.setdefault(r["code"], {"code": r["code"]})
            if r.get("industry"):
                e["industry"] = r["industry"]
    except Exception as e:                          # noqa: BLE001
        log.warning("行业分类拉取失败：%s", e)

    rows = [v for v in merged.values() if len(v) > 1]
    n = storage.update_stock_basic_info(conn, rows)
    log.info("标的元信息补充：基本资料 %d 条、行业 %d 条，写入 %d 条",
             len_b, len_i, n)
    return n


# =====================================================================
# 同步：成分股
# =====================================================================
def sync_constituents(conn, sess, target):
    """拉取并保存指数成分股，返回成分股代码列表。"""
    code = target["code"]
    cons = sess.constituents(code)
    if not cons:
        # 两种情况都不能清空已有成分股：
        #   ① None：baostock 没有该指数接口（如科创50），沿用库中手工维护的；
        #   ② []  ：接口"成功但 0 行"（临时故障/无数据），若照常写入会
        #           把该指数全部成分股置为停用，当天起再也抓不到它的个股数据。
        existing = storage.load_constituents(conn, code)
        why = "无 baostock 成分股接口" if cons is None else "接口返回 0 行"
        log.warning("%s %s，沿用库中已有的 %d 只（不清空，可手工维护）",
                    target["name"], why, len(existing))
        return existing
    codes = [c["code"] for c in cons]
    saved, deact = storage.save_constituents(conn, code, codes)
    # 顺带把成分股名称补进 stock_basic
    storage.upsert_stock_basic(conn, [
        {"code": c["code"], "name": c["name"], "ktype": "stock",
         "market": c["code"].split(".")[0]} for c in cons])
    storage.set_sync(conn, code, "constituent", _today(), saved)
    log.info("%s 成分股：%d 只（停用旧记录 %d 条）", target["name"], saved, deact)
    return codes


# =====================================================================
# 同步：交易日历
# =====================================================================
def sync_trade_dates(conn, sess, full=False):
    """同步交易日历到 trade_date 表（按年循环，单次区间不至于过大）。

    落库后即可离线判断"今天是不是交易日"，无需每次联网。
    增量策略：从库内已有日历的最新年份开始重拉（当年日历会被 baostock 逐步补全，
    所以当年必须整年重拉，不能只补最后一天）；并按需向未来多要一年。
    """
    cfg = config.baostock()
    first_year = int(cfg.get("start_date", "1990-01-01")[:4])
    last_year = datetime.now().year + 1          # 多取一年（部分年份日历会提前公布）

    last, _ = storage.trade_date_range(conn)
    if full or not last:
        from_year = first_year
    else:
        from_year = max(first_year, int(last[:4]))

    total = 0
    last_ok_year = from_year - 1
    for y in range(from_year, last_year + 1):
        try:
            rows = sess.trade_dates(f"{y}-01-01", f"{y}-12-31")
        except RuntimeError as e:
            # 失败年份不推进进度，下次增量会重试该年份
            log.warning("交易日历 %d 年获取失败，本次跳过: %s", y, e)
            break
        if rows:
            total += storage.upsert_trade_dates(conn, rows)
        last_ok_year = y

    if last_ok_year >= from_year:
        storage.set_sync(conn, "trade_calendar", "trade_date",
                         f"{last_ok_year}-12-31", storage.count_trade_dates(conn))
    lo, hi = storage.trade_date_range(conn)
    log.info("交易日历：本次写入 %d 行，覆盖 %s ~ %s（共 %d 个交易日）",
             total, lo, hi, storage.count_trade_dates(conn, open_only=True))
    return total


# =====================================================================
# 同步：单个标的（指数 / 组合 / 个股）—— 供"拉取数据"按钮使用
# =====================================================================
def sync_stock_meta(conn, sess, code) -> bool:
    """拉取单只个股的**元数据**：名称 / 上市日期 / 行业（+ 代码里的市场）。

    全市场批量补元数据走 sync_stock_basics；这里是"新加入组合的个股"用的，
    只查这一只。查询失败不算致命（K线照样能拉），返回是否写入了记录。
    """
    info = {"code": code, "ktype": "stock",
            "market": code.split(".")[0] if "." in code else None}
    try:
        for r in sess.stock_basics(code):
            if r.get("code") == code:
                info["name"] = r.get("name") or None
                info["listed_date"] = r.get("listed_date")
                break
    except Exception as e:                          # noqa: BLE001
        log.warning("%s 基本资料拉取失败：%s", code, e)
    try:
        for r in sess.stock_industries(code):
            if r.get("code") == code:
                info["industry"] = r.get("industry")
                break
    except Exception as e:                          # noqa: BLE001
        log.warning("%s 行业分类拉取失败：%s", code, e)

    # 已有记录时只补"这次拿到的字段"，别用 None 覆盖（upsert 是 REPLACE）
    have = storage.load_stock_basic(conn, code=code)
    if have:
        old = have[0]
        for k in ("name", "ktype", "market", "industry", "listed_date"):
            info.setdefault(k, old.get(k))
    storage.upsert_stock_basic(conn, [info])
    log.info("%s 元数据：%s / %s / 上市 %s", code, info.get("name"),
             info.get("industry"), info.get("listed_date"))
    return True


def sync_stock(conn, sess, code, full=False) -> dict:
    """同步单只个股：历史K线（前复权）→ 不复权收盘价 → 分红。

    注意：这里只负责取数并落库，不计算动态股息率（股息率由
    indicators.fill_dividend_yields 在「计算指标」任务里离线补算）。

    返回摘要（供接口回传/日志）。
    """
    n_k = sync_kline(conn, sess, code, "stock", full)
    n_raw = sync_raw_close(conn, sess, code, full)
    _, new_div = sync_dividends(conn, sess, code, full)
    log.info("%s 个股同步：K线 %d 行，不复权收盘价 %d 行，新分红 %s",
             code, n_k, n_raw, new_div)
    return {"code": code, "kline": n_k, "close_raw": n_raw,
            "new_dividend": bool(new_div)}


def _latest_ref_date(conn) -> str | None:
    """判断"是否最新"的基准日期：最近一个交易日；日历为空时返回 None（只补无数据者）。"""
    return storage.latest_trade_date(conn, _today())


def _stale_stocks(conn, codes, full, ref) -> list[str]:
    """需要补拉的成分股：全量模式全部；否则"无数据"或"落后于最近交易日"。"""
    stale = []
    for c in codes:
        last = storage.latest_kline_date(conn, c)
        if full or not last or (ref and last < ref):
            stale.append(c)
    return stale


def sync_index(conn, sess, code, full=False) -> dict:
    """同步指数：① 成分股（先逐个补拉到最新）→ ② 指数自身K线。

    返回摘要。注意：这里只负责"取数"，五指标的指数估值聚合由
    indicators.rebuild_index_valuation 完成（在 pipeline.sync_target 里调用）。
    """
    t = config.target(code)
    if t:
        cons = sync_constituents(conn, sess, t)
    else:
        cons = storage.load_constituents(conn, code)
    ref = _latest_ref_date(conn)
    stale = _stale_stocks(conn, cons, full, ref)
    log.info("%s 成分股 %d 只，需补拉 %d 只（基准交易日 %s）",
             code, len(cons), len(stale), ref)
    for i, c in enumerate(stale, 1):
        try:
            sync_stock(conn, sess, c, full)
            if i % 20 == 0:
                log.info("[成分股 %d/%d] %s 已同步", i, len(stale), c)
        except Exception as e:                          # noqa: BLE001
            log.warning("成分股 %s 同步失败：%s", c, e)
    n_idx = sync_kline(conn, sess, code, "index", full)
    return {"code": code, "constituents": len(cons), "stale_synced": len(stale),
            "index_kline": n_idx}


def sync_portfolio(conn, sess, code, full=False) -> dict:
    """同步组合的成分股（组合没有 baostock 指数K线，K线由成分股综合计算）。

    这里只补成分股数据；组合K线(OHLCV)+五指标的合成由
    indicators.rebuild_portfolio_kline 完成。
    """
    cons = storage.load_constituents(conn, code)
    ref = _latest_ref_date(conn)
    stale = _stale_stocks(conn, cons, full, ref)
    log.info("%s 组合成分股 %d 只，需补拉 %d 只", code, len(cons), len(stale))
    for i, c in enumerate(stale, 1):
        try:
            sync_stock(conn, sess, c, full)
            if i % 20 == 0:
                log.info("[组合成分股 %d/%d] %s 已同步", i, len(stale), c)
        except Exception as e:                          # noqa: BLE001
            log.warning("成分股 %s 同步失败：%s", c, e)
    return {"code": code, "constituents": len(cons), "stale_synced": len(stale)}


# =====================================================================
# 同步：K 线
# =====================================================================
def sync_kline(conn, sess, code, ktype, full=False):
    """同步某标的 K 线（指数与个股通用）。返回写入行数。"""
    start = _kline_start(conn, code, full)
    today = _today()
    if start > today:
        return 0
    rows = sess.kline(code, start, today)
    if not rows:
        return 0
    n = storage.upsert_kline(conn, code, ktype, rows)
    storage.set_sync(conn, code, "kline", rows[-1]["date"],
                     storage.count_kline(conn, code))
    return n


def sync_raw_close(conn, sess, code, full=False):
    """同步不复权收盘价（adjustflag=3）到 kline.close_raw，供股息率离线计算。

    不复权价不受分红/送股影响，增量随 K 线重叠窗口即可；
    首次（无进度）或 full 时拉全历史。
    """
    cfg_start = config.baostock().get("start_date", "1990-01-01")
    st = storage.get_sync(conn, code, "close_raw")
    if full or not st or not st.get("last_date"):
        start = cfg_start
    else:
        start = max(_shift_date(st["last_date"], -KLINE_OVERLAP_DAYS), cfg_start)
    today = _today()
    if start > today:
        return 0
    raw = sess.unadjusted_close(code, start, today)
    if not raw:
        return 0
    rows = [{"date": d, "close_raw": c} for d, c in raw.items()]
    n = storage.update_close_raw(conn, code, rows)
    storage.set_sync(conn, code, "close_raw", max(raw), len(raw))
    return n


# =====================================================================
# 同步：分红
# =====================================================================
def sync_dividends(conn, sess, code, full=False):
    """按年拉取分红并入库。

    baostock 的 query_dividend_data 必须指定年份，故按年循环；
    已同步进度记录在 sync_state(dtype='dividend')，增量时只补最近两年。

    分红只用于动态股息率（近 12 个月），故回溯年数单独用 dividend_years_back
    限制（默认近 3 年），不与 K 线的"全部历史"起点(1990)绑定，避免白拉几十年。
    """
    cfg = config.baostock()
    cur_year = datetime.now().year
    first_year = max(int(cfg.get("start_date", "1990-01-01")[:4]),
                     cur_year - int(cfg.get("dividend_years_back", 3)))

    if full:
        from_year = first_year
    else:
        st = storage.get_sync(conn, code, "dividend")
        from_year = (first_year if not st or not st.get("last_date")
                     else max(first_year, int(st["last_date"][:4]) - 1))  # 回退1年
    before = {(r["ex_date"], r["cash_ps"])
              for r in storage.load_dividends(conn, code)}
    total = 0
    last_ok_year = from_year - 1        # 已成功覆盖到的年份
    for y in range(from_year, cur_year + 1):
        try:
            rows = sess.dividends(code, y)
        except RuntimeError as e:
            # 失败年份不推进进度，下次增量会重试该年份（避免数据永久缺失）
            log.warning("%s %d 年分红获取失败，本次跳过: %s", code, y, e)
            break
        if rows:
            total += storage.upsert_dividends(conn, code, rows)
        last_ok_year = y
    if last_ok_year >= from_year:
        # 只把进度推进到"已成功覆盖的年份"，未覆盖的年份留待下次
        storage.set_sync(conn, code, "dividend", f"{last_ok_year}-12-31", total)

    # 新增分红记录 → 该股前复权历史整体变了，需要全量重拉 K 线才能修正。
    # 返回值里带上这个标记，由 sync_all 决定是否重拉（避免这里重复请求）。
    after = {(r["ex_date"], r["cash_ps"])
             for r in storage.load_dividends(conn, code)}
    return total, bool(after - before)


# =====================================================================
# 计算：动态股息率 —— 已移至 src/indicators/indicators.py
#   拉取数据任务只落原始数据（含不复权收盘价 close_raw）；
#   动态股息率 div_yield 由「计算指标」任务调用 indicators.fill_dividend_yields
#   离线补算（读 close_raw + dividend 表，不再联网）。
# =====================================================================
# =====================================================================
# 编排
# =====================================================================
def sync_all(conn, mode="auto", limit=None, only=None, only_index=None):
    """完整同步流程（纯取数，不含任何计算）。

    mode       'full' 全量重拉 / 'auto' 增量（默认）
    limit      只处理前 N 只个股（调试用）
    only       仅执行某一阶段：trade_date / constituent / kline / dividend / basic
    only_index 只处理指定指数代码
    """
    full = (mode == "full")
    targets = config.targets()
    if only_index:
        targets = [t for t in targets if t["code"] == only_index]
        if not targets:
            log.error("未找到估值目标：%s", only_index)
            return
    batch = int(config.baostock().get("batch_log", 50))
    t0 = time.time()
    skipped = []

    with BaostockSession() as sess:
        # ① 交易日历（最便宜，先做；后面各阶段都能用到）
        if only in (None, "trade_date"):
            try:
                sync_trade_dates(conn, sess, full)
            except Exception as e:                            # noqa: BLE001
                log.error("交易日历同步失败: %s", e)
                skipped.append(("trade_date", "-", str(e)))

        # ② 大盘指数（上证指数，仅行情展示）：随每日同步增量拉取
        if only in (None, "kline"):
            for mcode in MARKET_INDEXES:
                try:
                    n = sync_kline(conn, sess, mcode, "index", full)
                    log.info("大盘指数K线 %s: %d 行", mcode, n)
                except Exception as e:                        # noqa: BLE001
                    log.warning("大盘指数 %s 同步失败: %s", mcode, e)
                    skipped.append(("market", mcode, str(e)))

        # ③ 标的元信息（目标名称/类型）+ 行业/上市日期补全
        sync_targets(conn, sess, targets)
        if only in (None, "basic"):
            try:
                sync_stock_basics(conn, sess)
            except Exception as e:                        # noqa: BLE001
                log.warning("标的元信息补全失败：%s", e)
                skipped.append(("basic", "-", str(e)))

        # ④ 指数：成分股 + 指数 K 线；组合：成分股（组合K线由指标层合成）；个股目标：直接拉
        stock_codes = set()
        for t in targets:
            ktype = t["ktype"]
            if ktype == "portfolio":
                # 组合没有 baostock 代码：拉它的成分股，组合K线在 indicators 里合成
                stock_codes.update(storage.load_constituents(conn, t["code"]))
                continue
            if ktype != "index":
                stock_codes.add(t["code"])          # 个股目标
                continue
            if only in (None, "constituent"):
                try:
                    stock_codes.update(sync_constituents(conn, sess, t))
                except Exception as e:                        # noqa: BLE001
                    log.error("%s 成分股同步失败: %s", t["name"], e)
                    skipped.append(("constituent", t["code"], str(e)))
            else:
                stock_codes.update(storage.load_constituents(conn, t["code"]))
            if only in (None, "kline"):
                try:
                    n = sync_kline(conn, sess, t["code"], "index", full)
                    log.info("指数K线 %s %s: %d 行", t["name"], t["code"], n)
                except Exception as e:                        # noqa: BLE001
                    log.error("%s 指数K线同步失败: %s", t["name"], e)
                    skipped.append(("index_kline", t["code"], str(e)))

        stocks = sorted(stock_codes)
        if limit:
            stocks = stocks[:limit]
        log.info("待处理个股：%d 只", len(stocks))

        # ⑤ 个股 K 线（前复权）+ 不复权收盘价（供股息率离线计算）
        if only in (None, "kline"):
            for i, code in enumerate(stocks, 1):
                try:
                    n = sync_kline(conn, sess, code, "stock", full)
                    sync_raw_close(conn, sess, code, full)
                    if i % batch == 0 or n == 0:
                        log.info("[K线 %d/%d] %s: %d 行", i, len(stocks), code, n)
                except Exception as e:                        # noqa: BLE001
                    log.warning("K线 %s 失败: %s", code, e)
                    skipped.append(("kline", code, str(e)))

        # ⑥ 分红（股息率由「计算指标」任务离线补算）
        need_refetch = []                    # 有新分红 → 前复权历史变了，需全量重拉
        if only in (None, "dividend"):
            for i, code in enumerate(stocks, 1):
                try:
                    _, new_div = sync_dividends(conn, sess, code, full)
                    if new_div:
                        need_refetch.append(code)
                    if i % batch == 0:
                        log.info("[分红 %d/%d] %s", i, len(stocks), code)
                except Exception as e:                        # noqa: BLE001
                    log.warning("分红 %s 失败: %s", code, e)
                    skipped.append(("dividend", code, str(e)))

        # ⑦ 有新分红的个股：全量重拉 K 线（修正前复权历史）并重拉不复权收盘价
        if need_refetch and only in (None, "kline", "dividend"):
            log.info("%d 只个股出现新分红记录，重拉其全部K线以修正前复权历史",
                     len(need_refetch))
            for code in need_refetch:
                try:
                    n = sync_kline(conn, sess, code, "stock", full=True)
                    sync_raw_close(conn, sess, code, full=True)
                    log.info("  重拉 %s：%d 行", code, n)
                except Exception as e:                        # noqa: BLE001
                    log.warning("  重拉 %s 失败: %s", code, e)
                    skipped.append(("kline_refetch", code, str(e)))

    # ---------- 汇总 ----------
    log.info("同步完成，耗时 %.1f 分钟", (time.time() - t0) / 60)
    stats = storage.table_stats(conn)
    log.info("库内统计: kline=%s index_constituent=%s dividend=%s stock_basic=%s trade_date=%s",
             stats.get("kline"), stats.get("index_constituent"),
             stats.get("dividend"), stats.get("stock_basic"), stats.get("trade_date"))
    if skipped:
        log.warning("跳过 %d 项（可重跑自动续传）:", len(skipped))
        for kind, code, err in skipped[:20]:
            log.warning("  [%s] %s -> %s", kind, code, err)
        if len(skipped) > 20:
            log.warning("  ... 其余 %d 项略", len(skipped) - 20)


# =====================================================================
# CLI
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="baostock 数据同步")
    ap.add_argument("--full", action="store_true", help="全量重拉（忽略已有进度）")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只个股（调试）")
    ap.add_argument("--only",
                    choices=["trade_date", "constituent", "kline", "dividend", "basic"],
                    default=None, help="仅执行某一阶段")
    ap.add_argument("--index", default=None, help="只处理指定指数代码，如 sh.000300")
    ap.add_argument("--db", default=None, help="数据库路径（默认 config.DB_PATH）")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config.use_db(args.db)          # --db 也要切换配置来源，否则配置读的是默认库
    conn = storage.get_conn()
    try:
        sync_all(conn, mode="full" if args.full else "auto",
                 limit=args.limit, only=args.only, only_index=args.index)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
