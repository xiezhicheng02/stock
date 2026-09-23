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
import html
import logging
import os
import re
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

    # ---------------------------------------------------------------
    # 每日全市场快照（三个批量接口，返回不复权价 adjustflag=3）
    # ---------------------------------------------------------------
    def daily_all_astock(self, date):
        """某日全市场 A股日K。字段含不复权价 + peTTM/pbMRQ/psTTM/pcfNcfTTM。"""
        return self._call(bs.query_daily_history_k_AStock, date,
                          what=f"全市场A股 {date}")

    def daily_all_etf(self, date):
        """某日全部 ETF 日K（只有价格，估值字段为空）。"""
        return self._call(bs.query_daily_history_k_ETF, date,
                          what=f"全市场ETF {date}")

    def daily_adjust_factor(self, date):
        """某日复权因子（事件驱动，只有当天除权除息的股票才有行）。"""
        return self._call(bs.query_daily_adjust_factor, date,
                          what=f"复权因子 {date}")

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
def _f(v):
    """把 baostock 返回的字符串转成 float。

    批量接口（daily_history / adjust_factor）返回的都是字符串，空串表示"无此数据"。
    统一转成 None，交给 storage 落库时按 NULL 处理。
    （storage 里也有一个同名私有函数，两边各自独立，别跨模块引用。）
    """
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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


def meta_coverage(conn) -> dict:
    """个股元数据的覆盖情况（用来校验"批量补元数据到底补上没有"）。

    为什么要它：`sync_stock_basics` 在 `sync_all` 里是被 try/except 包住的，
    失败只留一行 warning，而且**它自己不做任何校验** —— 实测生产库里
    listed_date / industry 的覆盖率长期停在 6%（852 只里只有 51 只有），
    没有任何地方能看出来。加上校验后，补没补上一眼可见。
    """
    total, ld, ind = conn.execute(
        "SELECT count(*), count(listed_date), count(industry) "
        "FROM stock_basic WHERE ktype='stock'").fetchone()
    return {"total": total, "missing_listed": total - ld,
            "missing_industry": total - ind}


def sync_stock_basics(conn, sess) -> int:
    """补全 stock_basic 的**名称 / 行业 / 上市日期**（各一次批量查询全市场）。

    upsert：已有行定点更新、缺失的行新建（见 storage.upsert_stock_basic_info）。
    ktype 从 kline 带过来；末尾顺手清掉"没有 kline 数据"的行。
    """
    len_b = len_i = 0
    merged: dict[str, dict] = {}
    try:
        basics = sess.stock_basics()
        len_b = len(basics)
        for r in basics:
            e = merged.setdefault(r["code"], {"code": r["code"]})
            # ★ name 必须也取：原来只取 listed_date，导致新插入的 4000+ 只
            #   个股（以及全部 ETF）在 stock_basic 里有行却没有名称
            if r.get("name"):
                e["name"] = r["name"]
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

    # ktype 必须带上：每日快照会拉进几千只**新**股票，新行没有 ktype 的话
    # "WHERE ktype='stock'" 这类查询就看不到它（元数据等于白补）。
    kts = storage.code_ktypes(conn)
    rows = []
    for v in merged.values():
        if len(v) <= 1:
            continue
        v.setdefault("ktype", kts.get(v["code"]))
        rows.append(v)
    # 关键：upsert（先 UPDATE，命中 0 行才 INSERT）——
    # 旧的 update_stock_basic_info **只 UPDATE**，新股票没有行就永远补不上。
    upd, ins = storage.upsert_stock_basic_info(conn, rows)
    # 顺手清掉"没有 kline 数据"的行（批量接口给的是全市场证券表，含退市股，
    # 那些 code 查不到 ktype 会落成 NULL，不清就会每次同步新增两千多行垃圾）
    pruned = storage.prune_stock_basic_without_kline(conn)
    gaps = storage.kline_meta_gaps(conn)
    log.info("标的元信息补充：基本资料 %d 条、行业 %d 条 → 更新 %d、新建 %d | "
             "kline 标的 %d，其中无元数据行 %d、元数据不全 %d",
             len_b, len_i, upd, ins, gaps["total"], gaps["no_row"],
             gaps["no_meta"])
    log.info("已清理无 kline 数据的元数据行：%d 条", pruned)
    return upd + ins


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


#: sync_state 里「K线全历史已核对过」的标记（核对后缺口不再反复重拉）
KLINE_FULL_DTYPE = "kline_full"


def meta_missing(conn, code: str) -> bool:
    """这只标的的元数据是否缺失（缺名称/行业/上市日期任一项就算缺）。

    元数据只在"缺"的时候才去 baostock 补 —— 成分股有几十只时，
    每次重算都全量查一遍是白跑（一次网络往返/只），没必要。
    （属于取数层：判断"要不要去拉"，不涉及任何计算。）
    """
    rows = storage.load_stock_basic(conn, code=code)
    if not rows:
        return True
    b = rows[0]
    return not (b.get("name") and b.get("industry") and b.get("listed_date"))


def ensure_trade_calendar_span(conn, sess) -> bool:
    """交易日历没覆盖到 BAOSTOCK_START_DATE 时，整段重拉一次。返回是否重拉过。

    为什么要它：`sync_trade_dates` 的增量是从"库里已有日历的最新年份"往后接的，
    **从不回头**。所以建库时 start_date 若是 2015，之后改成 1990 也永远不会补。
    而没有 1990~2015 的日历，就判断不了那段哪天该有 K 线（缺口检测的前提）。
    """
    cfg_start = config.baostock().get("start_date", "1990-01-01")
    first, _last = storage.trade_date_range(conn)
    if first and first <= cfg_start:
        return False
    log.info("交易日历只覆盖到 %s，按 start_date=%s 整段重拉", first, cfg_start)
    sync_trade_dates(conn, sess, full=True)
    return True


def _expected_start(conn, code: str):
    """某只个股"应该从哪天开始有 K 线" = max(上市日期, BAOSTOCK_START_DATE)。

    **上市日期未知时返回 None** —— 由调用方退化成"只看中间/尾部缺口，
    不判断头部"。这一点很关键：如果拿 BAOSTOCK_START_DATE(1990) 去顶替，
    一只 1995 年上市、库里有 2015 年起数据的股票会被算成缺 6000 个交易日，
    于是被当成"头部截断"去全历史重拉 —— 800 只一起跑就是几小时白工。
    """
    rows = storage.load_stock_basic(conn, code=code)
    ld = rows[0].get("listed_date") if rows else None
    if not ld:
        return None
    return max(ld, config.baostock().get("start_date", "1990-01-01"))


def kline_gap_codes(conn, include_verified: bool = False) -> list[dict]:
    """体检：找出**K 线不完整**的个股 [{code, have, should, missing, start, head}]。

    逐只比较「交易日历里该有的交易日」和「实际 K 线日期」：
      * 起点 = max(上市日期, BAOSTOCK_START_DATE)；
      * 终点 = 最新交易日。

    为什么这个判据可靠：**停牌日 baostock 也会返回 K 线行**（volume=0），
    所以"某个交易日没有行"就是真缺数据，可以放心补 —— 不会像 close_raw /
    div_yield 那样出现"永远补不上、每天重试"的死循环。

    include_verified=False 时跳过已打 KLINE_FULL_DTYPE 标记的（核对过、缺口在
    baostock 那边也没有）。实测全库 838 只只要 ~0.9s，所以每次拉取都能跑。
    """
    # 终点必须是**今天或之前**的最近交易日：日历里含未来日期（到明年年底），
    # 直接取 MAX(date) 会把 10~12 月还没到的交易日也算成"该有 K 线"，
    # 于是每只股票都被误判成缺一大截。
    end = storage.latest_trade_date(conn, on_or_before=_today())
    if not end:
        return []
    verified = {r[0] for r in conn.execute(
        "SELECT code FROM sync_state WHERE dtype=?", (KLINE_FULL_DTYPE,))}
    out, unknown_ld = [], 0
    for (code,) in conn.execute(
            "SELECT DISTINCT code FROM kline WHERE ktype='stock'"):
        if not include_verified and code in verified:
            continue
        have, amin = conn.execute(
            "SELECT count(*), min(date) FROM kline WHERE code=? AND ktype='stock'",
            (code,)).fetchone()
        if not have:
            continue
        start = _expected_start(conn, code)
        head = False
        if start is None:
            # 上市日期未知 → 不判断头部，只用它自己的首条 K 线当起点
            unknown_ld += 1
            start = amin
        elif amin and amin > start:
            head = True
        if start > end:
            continue
        should = conn.execute(
            "SELECT count(*) FROM trade_date WHERE is_open=1 AND date>=? AND date<=?",
            (start, end)).fetchone()[0]
        if should > have:
            out.append({"code": code, "have": have, "should": should,
                        "missing": should - have, "start": start, "head": head})
    if unknown_ld:
        log.warning("%d 只个股缺上市日期，本次只检查中间/尾部缺口（头部不判断）——"
                    " 等 sync_stock_basics 把 listed_date 补齐后才会检查头部",
                    unknown_ld)
    out.sort(key=lambda x: -x["missing"])
    return out


def repair_kline_gaps(conn, sess, gaps: list) -> dict:
    """对体检出的缺口个股**全历史重拉**，并重新落 close_raw。

    重拉是安全的：upsert_kline 用 UPSERT + COALESCE 保住 close_raw；
    div_yield 会被置空 —— 那是有意的失效信号，由「计算指标」任务重算。
    新增的历史日期没有 close_raw，所以紧接着补一次（full）。
    """
    fixed = failed = 0
    for i, g in enumerate(gaps, 1):
        code = g["code"]
        try:
            n = sync_kline(conn, sess, code, "stock", full=True)
            sync_raw_close(conn, sess, code, full=True)
            storage.set_sync(conn, code, KLINE_FULL_DTYPE,
                             last_date=storage.latest_kline_date(conn, code),
                             row_count=n, incremental=False)
            fixed += 1
            log.info("[补K线缺口 %d/%d] %s：重拉 %d 行（原缺 %d 个交易日）",
                     i, len(gaps), code, n, g["missing"])
        except Exception as e:                          # noqa: BLE001
            failed += 1
            log.warning("补K线缺口 %s 失败：%s", code, e)
    return {"fixed": fixed, "failed": failed, "total": len(gaps)}


def refetch_for_readjust(conn, sess, codes) -> dict:
    """全量重拉这些个股的 K 线，修正前复权历史（并补一次 close_raw）。

    成功 → 清掉待办标记；失败 → **记下待办**下次重试。
    这就是"别让失败静默丢失"的落点：分红已经落库，下一轮不会再报"新分红"，
    只有靠这个标记才能把重拉补上。
    """
    ok = failed = 0
    for code in codes:
        try:
            n = sync_kline(conn, sess, code, "stock", full=True)
            sync_raw_close(conn, sess, code, full=True)
            storage.clear_readjust_pending(conn, code)
            ok += 1
            log.info("  重拉 %s：%d 行", code, n)
        except Exception as e:                          # noqa: BLE001
            failed += 1
            storage.mark_readjust_pending(conn, code, str(e))
            log.warning("  重拉 %s 失败（已记为待办，下次重试）: %s", code, e)
    return {"ok": ok, "failed": failed, "total": len(codes)}


def sync_market_snapshot(conn, sess, date) -> dict:
    """当天全市场快照：A股 + ETF → kline（**只落这一天**）。

    这三个批量接口返回的是**不复权**价（adjustflag=3）：
      * 不复权收盘价 → close_raw
      * peTTM/pbMRQ/psTTM/pcfNcfTTM → 指数之外的个股估值（选股用）
      * **前复权 close 不写** —— 接口没给，也绝不覆盖库里已有的值
        （见 storage.snapshot_rows 的 COALESCE 说明）。
    """
    n_a = n_e = 0
    try:
        rows = sess.daily_all_astock(date)
        n_a = _write_snapshot(conn, rows, "stock")
        log.info("全市场快照 A股：%d 行", n_a)
    except Exception as e:                              # noqa: BLE001
        log.error("全市场 A股快照失败：%s", e)
    try:
        rows = sess.daily_all_etf(date)
        n_e = _write_snapshot(conn, rows, "etf")
        log.info("全市场快照 ETF：%d 行", n_e)
    except Exception as e:                              # noqa: BLE001
        log.error("全市场 ETF 快照失败：%s", e)
    return {"astock": n_a, "etf": n_e}


def _write_snapshot(conn, rows, ktype) -> int:
    """把批量接口的行按 code 分组后落库（storage.snapshot_rows 按 code 写）。"""
    by_code = {}
    for r in rows:
        code = r.get("code")
        if not code:
            continue
        by_code.setdefault(code, []).append({
            "date": r.get("date"),
            "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
            "close_raw": r.get("close"),          # ← 不复权价进 close_raw
            "preclose": r.get("preclose"), "volume": r.get("volume"),
            "amount": r.get("amount"), "turn": r.get("turn"),
            "pct_chg": r.get("pctChg"), "pe_ttm": r.get("peTTM"),
            "pb_mrq": r.get("pbMRQ"), "ps_ttm": r.get("psTTM"),
            "pcf_ncf_ttm": r.get("pcfNcfTTM"), "is_st": r.get("isST"),
        })
    n = 0
    for code, rs in by_code.items():
        n += storage.snapshot_rows(conn, code, ktype, rs)
    return n


def sync_daily_adjust_factors(conn, sess, date) -> int:
    """当天复权因子落库（先只存不参与计算）。"""
    rows = sess.daily_adjust_factor(date)
    out = [{"code": r.get("code"), "date": r.get("dividOperateDate"),
            "fore_factor": _f(r.get("foreAdjustFactor")),
            "back_factor": _f(r.get("backAdjustFactor")),
            "adjust_factor": _f(r.get("adjustFacto"))} for r in rows]
    n = storage.upsert_adjust_factors(conn, out)
    log.info("复权因子 %s：%d 行", date, n)
    return n


def sync_target_history(conn, sess) -> dict:
    """只给「标的信息」里的 index / stock 补全历史（**组合跳过**）。

    * 指标判据：拿交易日历比对，找出「该有 K 线行却没有」的交易日；
    * 指数**不拉成分股** —— 指数自己的 K 线就带 peTTM/pbMRQ/psTTM/pcfNcfTTM；
    * 前复权价只能走 query_history_k_data_plus(adjustflag=2) 逐只拉（批量接口
      只给不复权价）。
    """
    todo = [t for t in config.targets(only_enabled=False)
            if t["ktype"] in ("index", "stock")]
    out = {"targets": len(todo), "synced": 0, "failed": []}
    for t in todo:
        code = t["code"]
        try:
            before = storage.count_kline(conn, code)
            sync_kline(conn, sess, code, t["ktype"], full=False)
            after = storage.count_kline(conn, code)
            out["synced"] += 1
            log.info("标的 %s(%s) 历史补齐：%d → %d 行",
                     code, t["ktype"], before, after)
        except Exception as e:                          # noqa: BLE001
            log.warning("标的 %s 历史补齐失败：%s", code, e)
            out["failed"].append(code)
    return out


def ensure_trade_calendar(conn, sess) -> bool:
    """交易日历缺失时补一次（离线判断"最新"的基准）。返回是否补过。

    属于**取数**：只联网落库，不做任何计算。
    """
    if storage.latest_trade_date(conn, _today()):
        return False
    sync_trade_dates(conn, sess, full=False)
    return True


def sync_target_data(conn, sess, code, ktype, full=False) -> dict:
    """按标的类型取数 —— **只取数，不算任何东西**。

      指数    成分股 + 指数 K 线
      组合    成分股（组合 K 线由计算层合成）
      个股    元数据 + K 线 + 不复权收盘价 + 分红

    股息率 / 指数估值聚合 / 组合K线合成 / 评分一律由
    `indicators.compute_target` 在取数完成后单独做。
    这样「拉取数据」定时任务只需调取数这一半，职责边界清楚。
    """
    if ktype == "index":
        return sync_index(conn, sess, code, full)
    if ktype == "portfolio":
        return sync_portfolio(conn, sess, code, full)
    # 元数据（名称/行业/上市日期）也要拉：新加入组合的个股以前只拉了
    # K线+分红，页面上名称/行业是空的
    sync_stock_meta(conn, sess, code)
    return sync_stock(conn, sess, code, full)


def sync_constituents_data(conn, codes, full=False) -> dict:
    """把给定成分股的「K线 / 分红 / 元数据」补到最新（**只补缺的，纯取数**）。

    * K线+分红：只对"没有数据"或"落后于最近交易日"或 close_raw 覆盖不足的标的拉；
    * 元数据（名称/行业/上市日期）：只对**缺元数据的**标的拉，
      已经齐全的跳过（成分股变动时新加进来的那几只才会被查）。

    新拉回来的 K 线需要补动态股息率 —— 那是**计算**，由调用方
    （pipeline.ensure_constituent_data）拿去交给 indicators 做，这里不碰。
    """
    if not codes:
        return {"checked": 0, "stale": 0, "synced": [], "meta": 0,
                "meta_checked": 0, "failed": []}
    ref = _latest_ref_date(conn)
    stale = _stale_stocks(conn, codes, full, ref)
    need_meta = [c for c in codes if meta_missing(conn, c)]
    synced, meta, failed = [], 0, []
    with BaostockSession() as sess:
        for c in need_meta:
            try:
                sync_stock_meta(conn, sess, c)
                meta += 1
            except Exception as e:                          # noqa: BLE001
                log.warning("%s 元数据拉取失败：%s", c, e)
                failed.append(c)
        for c in stale:
            try:
                sync_stock(conn, sess, c)
                synced.append(c)
            except Exception as e:                          # noqa: BLE001
                log.warning("%s 增量拉取失败：%s", c, e)
                failed.append(c)
    log.info("成分股数据检查：共 %d 只 | K线需补 %d（已补 %d）| "
             "元数据缺 %d（已补 %d）| 失败 %d",
             len(codes), len(stale), len(synced), len(need_meta), meta,
             len(failed))
    return {"checked": len(codes), "stale": len(stale), "synced": synced,
            "meta": meta, "meta_checked": len(need_meta), "failed": failed}


def _latest_ref_date(conn) -> str | None:
    """判断"是否最新"的基准日期：最近一个交易日；日历为空时返回 None（只补无数据者）。"""
    return storage.latest_trade_date(conn, _today())


def _stale_stocks(conn, codes, full, ref) -> list[str]:
    """需要补拉的成分股：全量模式全部；否则"无数据"或"落后于最近交易日"
    **或 close_raw 覆盖不足**。

    最后那条很重要：close_raw 是另一路查询落的，K 线已经最新并不代表它有值。
    以前只看 K 线，导致"K线已最新但 close_raw 整条缺失"的股票永远进不了补拉名单。
    """
    stale = []
    for c in codes:
        last = storage.latest_kline_date(conn, c)
        if (full or not last or (ref and last < ref)
                or raw_close_missing(conn, c)):
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


#: close_raw 覆盖率低于这个值就判定"需要重拉全历史"
#: （正常入库后覆盖率接近 100%；明显偏低只可能是被 K 线重写抹掉过）
RAW_CLOSE_MIN_COVER = 0.9


def raw_close_missing(conn, code: str) -> bool:
    """该股的 close_raw 是否明显缺失（被抹掉过 / 从没拉过）。

    为什么要这个检查：close_raw 有独立水位（sync_state.dtype='close_raw'），
    水位只记"拉到哪个日期"，**不保证历史真的在**。历史上 K 线重写把 close_raw
    抹掉之后，水位还在、增量又只拉最近几天，于是历史永久丢失且永远不重拉。
    改用"实际覆盖率"当判据就能自愈。
    """
    row = conn.execute(
        "SELECT count(*), count(close_raw) FROM kline "
        "WHERE code=? AND ktype='stock'", (code,)).fetchone()
    if not row or not row[0]:
        return False
    return (row[1] / row[0]) < RAW_CLOSE_MIN_COVER


def sync_raw_close(conn, sess, code, full=False):
    """同步不复权收盘价（adjustflag=3）到 kline.close_raw，供股息率离线计算。

    不复权价不受分红/送股影响，增量随 K 线重叠窗口即可；
    首次（无进度）、full、或**实际覆盖率不足**时拉全历史。
    """
    cfg_start = config.baostock().get("start_date", "1990-01-01")
    st = storage.get_sync(conn, code, "close_raw")
    repair = raw_close_missing(conn, code)
    if full or repair or not st or not st.get("last_date"):
        if repair and not full:
            log.info("%s close_raw 覆盖不足，重拉全历史", code)
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
    # 只有**真的写进去了**才推进水位：否则水位会谎报"已同步"，
    # 下次增量只拉最近几天，历史缺口永远补不回来。
    if n:
        storage.set_sync(conn, code, "close_raw", max(raw), n)
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
# 指数元数据：来自 baostock **文档**（不是行情接口）
# ---------------------------------------------------------------------
# baostock 的 query_stock_basic 只给"代码+简称+上市日"，拿不到指数全称、
# 类别、发布机构、简介。文档页 dataExplain.md 的「指数数据」章节有 10 张表
# （综合/规模/一级行业/二级行业/策略/成长/价值/主题/基金/债券），约 560 只指数。
# 该接口只接受 POST，返回 markdown（表格已是内联 HTML）。
# 这里只落**元数据**，不碰 kline / valuation_score，因此不影响任何评分。
# =====================================================================
INDEX_DOC_URL = "https://www.baostock.com/helpdocs/api/markdown/dataExplain.md"
INDEX_DOC_SRC = "baostock-doc"          # 写进 stock_basic.meta_src

#: 文档里的章节边界：从「指数数据」到「退市数据」之间
_IDX_DOC_START = '## <a id="指数数据"'
_IDX_DOC_END = '### <a id="退市数据"'

_RE_INDEX_TOKEN = re.compile(
    r'^###\s+<a id="[^"]*"></a>(.+?)\s*$'          # 类别标题（markdown 原样）
    r'|<table class="index-table">(.*?)</table>',  # 一张指数表
    re.S | re.M)
_RE_TR = re.compile(r"<tr>")
_RE_TD = re.compile(r"<td>(.*?)</td>", re.S)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_CODE = re.compile(r"^(?:sh|sz)\.\d{6}$")
_RE_DATE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")


def fetch_index_doc(url: str = INDEX_DOC_URL, timeout: int = 30) -> str:
    """下载 baostock 文档正文（该接口只接受 POST，GET 会返回 405）。

    用标准库 urllib 而不是 requests：项目自身的取数层一直不依赖第三方 HTTP 客户端
    （requests 只是 akshare 的传递依赖），没必要为一次 POST 引入它。
    """
    import urllib.request

    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"User-Agent": "Mozilla/5.0 (index-meta import)",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def parse_index_doc(text: str) -> tuple[list[dict], dict]:
    """解析「指数数据」章节 → (指数行, 统计)。

    每行：code / name(简称) / full_name / category / publish_date / publisher / intro

    文档里三处已知瑕疵，这里容错处理而不是静默丢数据：
      * `sz.399237` 那行漏了 `</tr>` → 按 `<tr>` 切分，不配对 `</tr>`；
      * 发布日期原文错字 `2013/3/20/td>`、`22011/11/15`、`22015/8/31`
        → 用 `(\\d{4})/(\\d{1,2})/(\\d{1,2})` 抓取并规范成 YYYY-MM-DD；
      * `sh.000031` 同时出现在「价值指数」和「主题指数」→ 按 code 去重（留首次）。
    """
    try:
        i = text.index(_IDX_DOC_START)
    except ValueError as e:
        raise ValueError("文档里找不到「指数数据」章节，baostock 可能改了文档结构") from e
    j = text.find(_IDX_DOC_END)
    body = text[i:] if j < 0 else text[i:j]

    rows: list[dict] = []
    by_cat: dict[str, int] = {}
    tables = 0
    dup: list[str] = []
    bad_date: list[str] = []
    seen: set[str] = set()
    category = None
    for m in _RE_INDEX_TOKEN.finditer(body):
        if m.group(1):
            category = m.group(1).strip()
            by_cat.setdefault(category, 0)
            continue
        tables += 1
        for tr in _RE_TR.split(m.group(2))[1:]:
            tds = _RE_TD.findall(tr)
            if len(tds) < 6:
                continue
            cells = [html.unescape(_RE_TAG.sub("", t)).strip() for t in tds[:6]]
            code = cells[0]
            if not _RE_CODE.match(code):
                continue
            if code in seen:                 # 跨表重复（sh.000031）
                dup.append(code)
                continue
            seen.add(code)
            dm = _RE_DATE.search(cells[3])
            if not dm:
                bad_date.append(code)
                pub = None
            else:
                pub = "%s-%02d-%02d" % (dm.group(1), int(dm.group(2)),
                                        int(dm.group(3)))
            rows.append({"code": code, "name": cells[1], "full_name": cells[2],
                         "publish_date": pub, "publisher": cells[4],
                         "intro": cells[5], "category": category,
                         "market": code.split(".")[0]})
            if category:
                by_cat[category] += 1
    return rows, {"tables": tables, "categories": by_cat,
                  "duplicates": dup, "bad_date": bad_date}


def sync_index_meta(conn, text: str | None = None) -> dict:
    """抓取（或使用传入的正文）→ 解析 → 写入 stock_basic，返回统计。

    只写元数据：不碰 kline / valuation_score / valuation_target。
    """
    if text is None:
        text = fetch_index_doc()
    rows, meta = parse_index_doc(text)
    r = storage.upsert_index_meta(
        conn, [dict(x, meta_src=INDEX_DOC_SRC) for x in rows])
    out = {"total": len(rows), **meta, **r,
           "stats": storage.index_meta_stats(conn)}
    log.info("指数元数据：解析 %d 只（%d 张表）→ 新增 %d / 更新 %d，跳过 %d",
             out["total"], meta["tables"], r["inserted"], r["updated"],
             len(r["skipped"]))
    if meta["duplicates"]:
        log.warning("文档内重复代码（已去重留首次）：%s", meta["duplicates"])
    if meta["bad_date"]:
        log.warning("发布日期解析失败：%s", meta["bad_date"])
    if r["skipped"]:
        log.warning("代码已被非 index 行占用，跳过：%s",
                    [(s["code"], s["ktype"]) for s in r["skipped"]])
    return out


# =====================================================================
# 编排
# =====================================================================
def sync_all(conn, mode="auto", limit=None, only=None, only_index=None):
    """完整同步流程（纯取数，不含任何计算）。

    改造后的流程（2026-09 起）：
      ① 交易日历（含整段重拉，判断"该不该有 K 线"的前提）
      ② **当天全市场快照**：query_daily_history_k_AStock + _ETF，只落当天
      ③ **当天复权因子**：query_daily_adjust_factor（先只存不参与计算）
      ④ **只给标的补历史**：valuation_target 里 ktype ∈ (index, stock)
         —— 组合跳过；指数**不拉成分股**（指数自己的 K 线就带估值字段）

    mode  'full' 强制全量（透传给每日历/标的补历史）；'auto' 增量。
    limit / only / only_index 保留形参以兼容旧调用（新流程不再使用）。
    """
    full = (mode == "full")
    t0 = time.time()
    today = _today()
    skipped = []
    log.info("同步开始（模式 %s，日期 %s）", mode, today)

    with BaostockSession() as sess:
        # ① 交易日历
        try:
            sync_trade_dates(conn, sess, full)
            ensure_trade_calendar_span(conn, sess)
        except Exception as e:                            # noqa: BLE001
            log.error("交易日历同步失败: %s", e)
            skipped.append(("trade_date", "-", str(e)))

        # ② 当天全市场快照（A股 + ETF）
        try:
            snap = sync_market_snapshot(conn, sess, today)
        except Exception as e:                            # noqa: BLE001
            log.exception("全市场快照失败: %s", e)
            snap = {"astock": 0, "etf": 0}
            skipped.append(("snapshot", "-", str(e)))

        # ③ 当天复权因子
        try:
            n_factor = sync_daily_adjust_factors(conn, sess, today)
        except Exception as e:                            # noqa: BLE001
            log.error("复权因子同步失败: %s", e)
            n_factor = 0
            skipped.append(("adjust_factor", "-", str(e)))

        # ④ 只给标的（index / stock）补历史；组合跳过、指数不拉成分股
        try:
            hist = sync_target_history(conn, sess)
        except Exception as e:                            # noqa: BLE001
            log.exception("标的补历史失败: %s", e)
            hist = {"targets": 0, "synced": 0, "failed": []}
            skipped.append(("target_history", "-", str(e)))

        # ⑤ 元数据（名称/行业/上市日期）—— 只补缺的
        try:
            sync_stock_basics(conn, sess)
        except Exception as e:                            # noqa: BLE001
            log.warning("标的元信息补全失败：%s", e)
            skipped.append(("basic", "-", str(e)))

    stats = storage.table_stats(conn)
    log.info("同步完成，耗时 %.1f 分钟 | 快照 A股 %d / ETF %d | 复权因子 %d | "
             "标的补历史 %d（失败 %d）",
             (time.time() - t0) / 60, snap["astock"], snap["etf"], n_factor,
             hist["synced"], len(hist.get("failed") or []))
    log.info("库内统计: kline=%s index_constituent=%s dividend=%s stock_basic=%s trade_date=%s",
             stats.get("kline"), stats.get("index_constituent"),
             stats.get("dividend"), stats.get("stock_basic"), stats.get("trade_date"))
    if skipped:
        log.warning("跳过 %d 项：", len(skipped))
        for item in skipped:
            log.warning("  %s", item)
    return {"ok": not skipped, "snapshot": snap, "adjust_factor": n_factor,
            "history": hist, "skipped": skipped,
            "elapsed": round((time.time() - t0) / 60, 2)}

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
