# -*- coding: utf-8 -*-
"""季频盈利能力：baostock ``query_profit_data()`` 返回数据对象。

接口说明
--------
通过 baostock API 获取 A 股**季频盈利能力**指标，提供 2007 年至今数据。
返回类型为 pandas DataFrame，每个单元格都是字符串，空串 ``""`` 表示无数据。

参数（baostock 侧）
-------------------
code    股票/指数代码，如 sh.600000（必填）
year    统计年份，为空默认当前年
quarter 统计季度（1/2/3/4），为空默认当前季度

本类字段严格沿用 baostock 返回的原始列名，可直接 ``ProfitData(**row)`` 解析；
``from_dataframe(df)`` 用于把 ``rs.get_data()`` 的整表一次性转成对象列表。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class ProfitData(BaseModel):
    """单季盈利能力指标（一个 code 一个季度一条）。"""

    code: str
    """证券代码，如 ``sh.600000``（必填）。"""

    pubDate: Optional[str] = None
    """公司发布财报的日期，``YYYY-MM-DD``。"""

    statDate: Optional[str] = None
    """财报统计季度的最后一天，如 ``2017-03-31`` / ``2017-06-30``。"""

    roeAvg: Optional[float] = None
    """净资产收益率（平均，%）。

    算法：归属母公司股东净利润 /
    [(期初归属母公司股东权益 + 期末归属母公司股东权益) / 2] * 100%。
    """

    npMargin: Optional[float] = None
    """销售净利率（%）：净利润 / 营业收入 * 100%。"""

    gpMargin: Optional[float] = None
    """销售毛利率（%）：毛利 / 营业收入 * 100%
    = (营业收入 - 营业成本) / 营业收入 * 100%。"""

    netProfit: Optional[float] = None
    """净利润（元）。"""

    epsTTM: Optional[float] = None
    """每股收益 TTM：归属母公司股东的净利润 TTM / 最新总股本。"""

    MBRevenue: Optional[float] = None
    """主营业务收入（元）。"""

    totalShare: Optional[float] = None
    """总股本（股）。"""

    liqaShare: Optional[float] = None
    """流通股本（股）。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """把 baostock 的空串 ``""`` 统一转成 None，再交给 pydantic 做类型转换。

        code 是必填 str，若为空串这里会返回 None 并触发 pydantic 的必填校验报错
        —— 这是有意为之：代码为空属于数据错误，不应静默吞掉。
        """
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["ProfitData"]:
        """把 ``query_profit_data().get_data()`` 的 DataFrame 转成对象列表。

        为什么先归一 NaN：pandas 数值列缺失是 ``float('nan')``，不是空串，
        上面的 validator 抓不到它；这里统一 ``NaN -> None`` 后再逐行构造。
        空表 / None 返回空列表。
        """
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
