# -*- coding: utf-8 -*-
"""季频成长能力：baostock ``query_growth_data()`` 返回数据对象。

接口说明
--------
获取 A 股季频成长能力（同比增长率）指标，提供 2007 年至今数据。
返回 pandas DataFrame，单元格均为字符串，空串表示无数据。

用法：
    rs = bs.query_growth_data(code="sh.600000", year=2024, quarter=2)
    rows = GrowthData.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class GrowthData(BaseModel):
    """单季成长能力指标（同比增长率，%）。"""

    code: str
    """证券代码，如 ``sh.600000``（必填）。"""

    pubDate: Optional[str] = None
    """公司发布财报的日期。"""

    statDate: Optional[str] = None
    """财报统计季度的最后一天。"""

    YOYEquity: Optional[float] = None
    """净资产同比增长率（%）：(本期净资产 - 上年同期净资产) /
    |上年同期净资产| * 100%。"""

    YOYAsset: Optional[float] = None
    """总资产同比增长率（%）：(本期总资产 - 上年同期总资产) /
    |上年同期总资产| * 100%。"""

    YOYNI: Optional[float] = None
    """净利润同比增长率（%）：(本期净利润 - 上年同期净利润) /
    |上年同期净利润| * 100%。"""

    YOYEPSBasic: Optional[float] = None
    """基本每股收益同比增长率（%）：(本期基本 EPS - 上年同期基本 EPS) /
    |上年同期基本 EPS| * 100%。"""

    YOYPNI: Optional[float] = None
    """归属母公司股东净利润同比增长率（%）：
    (本期归母净利润 - 上年同期归母净利润) / |上年同期归母净利润| * 100%。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；code 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["GrowthData"]:
        """DataFrame → list[GrowthData]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
