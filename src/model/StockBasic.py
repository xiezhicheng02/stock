# -*- coding: utf-8 -*-
"""baostock 证券基本资料返回数据对象。

对应 baostock `query_stock_basic()` 的返回字段。和 Kline 一样，baostock 拿到的
每个值都是**字符串**，空串（""）代表无数据；这里用 pydantic v2 把一行字符串
dict 解析成带类型的对象：

    * 数值字段自动转 float / int；
    * 空串统一转成 None；
    * 字段名严格沿用 baostock 原始名（code_name / ipoDate / outDate …），
      拿到一行后可以直接 ``StockBasic(**row)``，无需改名。

注意：这个类只描述「baostock 返回的一行基本资料长什么样」，与数据库
stock_basic 表的列（code/name/ktype/market/industry/listed_date …）不是一回事
——落库前的字段映射由 `src/fetch_data/data_fetcher.py` 负责。

行业字段说明
------------
``industry`` **不是** ``query_stock_basic()`` 返回的，它来自**另一个独立接口**
``query_stock_industry()``（见 data_fetcher.stock_industries）。由于同一个标的
的基本资料和行业分类在实际业务里总是一起使用，这里把它合并进 StockBasic，
方便调用方拿到一个完整对象。两个接口需要分别请求后按 code 合并，例如：

    basics = {r["code"]: r for r in sess.stock_basics()}
    industries = {r["code"]: r["industry"] for r in sess.stock_industries()}
    rows = [StockBasic(**b, industry=industries.get(b["code"])) for b in basics.values()]
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class StockBasic(BaseModel):
    """baostock 证券基本资料一行（个股 / 指数 / ETF 等）。"""

    code: str                              # 证券代码，如 sh.600000 / sh.000300
    code_name: Optional[str] = None       # 证券名称，如 浦发银行 / 沪深300
    ipoDate: Optional[str] = None         # 上市日期 YYYY-MM-DD
    outDate: Optional[str] = None         # 退市日期 YYYY-MM-DD（未退市为 None）
    type: Optional[int] = None             # 证券类型：1股票 2指数 3其它 4可转债 5ETF
    status: Optional[int] = None          # 上市状态：1上市 0退市

    # ---- 以下字段来自另一个接口，不是 query_stock_basic() 的返回 ----
    industry: Optional[str] = None
    """所属行业（申万/证监会行业分类名称，如 ``"银行"``）。

    **来源：``query_stock_industry()``，不是 ``query_stock_basic()``。**
    两个接口需分别请求后按 code 合并；未查询或未匹配到时为 None。
    指数/ETF 通常无行业分类。
    """

    industryClassification: Optional[str] = None
    """行业分类标准（如 ``"申万"`` / ``"证监会行业分类"``），即 industry 取自哪套分类体系。

    **与 industry 同源**：均来自 ``query_stock_industry()``，需另外请求后按 code 合并。
    """

    # ---- 以下字段是本地同步状态，不是 baostock 返回 ----
    kline_full_sync_date: Optional[str] = None
    """本地记录：该证券的完整 kline 已补到哪一天（YYYY-MM-DD）。

    由 ``kline_backfill_task`` 维护：
      * 首次为空 → 从 ipoDate（或 10 年前）全量补拉；
      * 已有值 → 只增量补拉 [kline_full_sync_date, 今天] 的缺口；
      * 补完后更新为最后一个有数据的交易日。
    """

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """baostock 用空串表示无数据，统一转成 None（再由 pydantic 做类型转换）。

        code 是必填 str 字段，若它真为空，这里返回 None 后会触发 pydantic 的
        必填类型校验报错 —— 这正是想要的：代码为空属于数据错误，不应静默吞掉。
        """
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["StockBasic"]:
        """把 baostock 返回的 pandas DataFrame 逐行转成 StockBasic 对象列表。

        用法：
            rs = bs.query_stock_basic(code="sh.600000")
            rows = StockBasic.from_dataframe(rs.get_data())

        与 Kline.from_dataframe 同理：先把 DataFrame 里的 NaN 统一替换成
        None（pandas 数值列缺失是 float('nan')，不是空串），再逐行构造。
        """
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
