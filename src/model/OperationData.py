# -*- coding: utf-8 -*-
"""季频营运能力：baostock ``query_operation_data()`` 返回数据对象。

接口说明
--------
获取 A 股季频营运能力（周转率/周转天数）指标，提供 2007 年至今数据。
返回 pandas DataFrame，单元格均为字符串，空串表示无数据。

用法：
    rs = bs.query_operation_data(code="sh.600000", year=2024, quarter=2)
    rows = OperationData.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class OperationData(BaseModel):
    """单季营运能力指标（周转率）。"""

    code: str
    """证券代码，如 ``sh.600000``（必填）。"""

    pubDate: Optional[str] = None
    """公司发布财报的日期。"""

    statDate: Optional[str] = None
    """财报统计季度的最后一天。"""

    NRTurnRatio: Optional[float] = None
    """应收账款周转率（次）：营业收入 /
    [(期初应收票据及应收账款净额 + 期末应收票据及应收账款净额) / 2]。"""

    NRTurnDays: Optional[float] = None
    """应收账款周转天数（天）：季报天数 / 应收账款周转率。

    季报天数口径：一季报 90 天、中报 180 天、三季报 270 天、年报 360 天。
    """

    INVTurnRatio: Optional[float] = None
    """存货周转率（次）：营业成本 /
    [(期初存货净额 + 期末存货净额) / 2]。"""

    INVTurnDays: Optional[float] = None
    """存货周转天数（天）：季报天数 / 存货周转率。"""

    CATurnRatio: Optional[float] = None
    """流动资产周转率（次）：营业总收入 /
    [(期初流动资产 + 期末流动资产) / 2]。"""

    AssetTurnRatio: Optional[float] = None
    """总资产周转率：营业总收入 /
    [(期初资产总额 + 期末资产总额) / 2]。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；code 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["OperationData"]:
        """DataFrame → list[OperationData]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
