# -*- coding: utf-8 -*-
"""季频现金流量：baostock ``query_cash_flow_data()`` 返回数据对象。

接口说明
--------
获取 A 股季频现金流量结构指标（资产占比、已获利息倍数、现金流含量等），
提供 2007 年至今数据。返回 pandas DataFrame，单元格均为字符串，空串表示无数据。

用法：
    rs = bs.query_cash_flow_data(code="sh.600000", year=2024, quarter=2)
    rows = CashFlowData.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class CashFlowData(BaseModel):
    """单季现金流量结构指标。"""

    code: str
    """证券代码，如 ``sh.600000``（必填）。"""

    pubDate: Optional[str] = None
    """公司发布财报的日期。"""

    statDate: Optional[str] = None
    """财报统计季度的最后一天。"""

    CAToAsset: Optional[float] = None
    """流动资产 / 总资产。"""

    NCAToAsset: Optional[float] = None
    """非流动资产 / 总资产。"""

    tangibleAssetToAsset: Optional[float] = None
    """有形资产 / 总资产。"""

    ebitToInterest: Optional[float] = None
    """已获利息倍数（息税前利润 / 利息费用），衡量偿付利息的能力。"""

    CFOToOR: Optional[float] = None
    """经营活动产生的现金流量净额 / 营业收入。"""

    CFOToNP: Optional[float] = None
    """经营活动产生的现金流量净额 / 净利润。"""

    CFOToGr: Optional[float] = None
    """经营活动产生的现金流量净额 / 营业总收入。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；code 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["CashFlowData"]:
        """DataFrame → list[CashFlowData]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
