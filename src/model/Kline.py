# -*- coding: utf-8 -*-
"""baostock 日 K 线返回数据对象。

对应 baostock `query_history_k_data_plus(frequency="d")` 的返回字段。
baostock 底层是裸 socket，`rs.get_row_data()` 拿到的每个值都是**字符串**，
空串（""）代表该字段无数据。这里用 pydantic v2 把一行字符串 dict 解析成
带类型的对象：

    * 数值字段自动转 float / int；
    * 空串统一转成 None；
    * 字段名严格沿用 baostock 的原始驼峰名（pctChg / peTTM / pbMRQ …），
      这样拿到一行后可以直接 `Kline(**row)`，无需再做字段改名。

注意：这个类只描述「baostock 返回的一行长什么样」，与数据库 kline 表的
snake_case 列名（pct_chg / pe_ttm …）不是一回事 —— 落库前的字段映射仍由
`src/fetch_data/data_fetcher.py` 里的 FIELD_MAP 负责。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class Kline(BaseModel):
    """baostock 日 K 线单行数据（前/后复权或不复权均可，由 adjustflag 区分）。"""

    # ---- 标识与行情 ----
    date: str                              # 交易所行情日期 YYYY-MM-DD
    code: str                              # 证券代码，如 sh.600000 / sz.000001
    open: Optional[float] = None           # 开盘价
    high: Optional[float] = None           # 最高价
    low: Optional[float] = None             # 最低价
    close: Optional[float] = None          # 收盘价
    preclose: Optional[float] = None        # 前收盘价
    volume: Optional[float] = None          # 成交量（累计，单位：股）
    amount: Optional[float] = None         # 成交额（单位：人民币元）

    # ---- 复权与交易状态 ----
    adjustflag: Optional[int] = None         # 复权状态：1=后复权, 2=前复权, 3=不复权
    tradestatus: Optional[int] = None        # 交易状态：1=正常交易, 0=停牌
    turn: Optional[float] = None            # 换手率(%) = 当日成交量/流通股总数 × 100
    pctChg: Optional[float] = None          # 涨跌幅(%) = (收盘-前收盘)/前收盘 × 100

    # ---- 估值指标（TTM/MRQ）----
    peTTM: Optional[float] = None          # 滚动市盈率 = 收盘价*总股本 / 归母净利润TTM
    pbMRQ: Optional[float] = None           # 市净率 = 总市值 / 最近一期归母净资产
    psTTM: Optional[float] = None          # 滚动市销率 = 收盘价*总股本 / 营业总收入TTM
    pcfNcfTTM: Optional[float] = None       # 滚动市现率 = 收盘价*总股本 / 现金及等价物净增加额TTM

    # ---- 风险标识 ----
    isST: Optional[int] = None              # 是否 ST 股：1=是, 0=否

    # ---- 本地扩展（非 baostock 返回，落库时由调用方填充）----
    ktype: Optional[str] = None             # 标的类型：stock=个股, etf=ETF, index=指数

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """baostock 用空串表示无数据，统一转成 None（再由 pydantic 做类型转换）。

        date / code 也是 str 字段，若它们真的为空，这里返回 None 后会触发
        pydantic 的必填类型校验报错 —— 这正是想要的：日期/代码为空属于数据错误，
        不应静默吞掉。
        """
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["Kline"]:
        """把 baostock 返回的 pandas DataFrame 逐行转成 Kline 对象列表。

        用法：
            rs = bs.query_history_k_data_plus(...)
            df = rs.get_data()          # baostock 返回的就是 DataFrame
            rows = Kline.from_dataframe(df)

        列名即 baostock 的 fields（date/open/close/pctChg/peTTM …），与本类
        字段一一对应，因此逐行直接 ``cls(**row)`` 构造即可。

        为什么要先归一化 NaN：baostock 的 get_data() 把缺失值落成 ``float('nan')``
        （而不是空串），上面的空串 validator 抓不到它；若直接 ``Kline(**row)``，
        nan 会被当成合法 float 保留下来。这里统一把 NaN 替换成 None，再交给
        构造方法做类型转换。
        """
        if df is None or len(df) == 0:
            return []
        # astype(object) 是为了让 None 能写进列（纯数值列不支持 None）；
        # where(notna, None) 在 NaN 处填 None。
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
