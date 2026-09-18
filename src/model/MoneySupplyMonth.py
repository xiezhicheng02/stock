# -*- coding: utf-8 -*-
"""货币供应量（月度）：baostock ``query_money_supply_data_month()`` 返回数据对象。

接口说明
--------
获取央行公布的**月度货币供应量**（M0/M1/M2）余额与同比、环比。
返回 pandas DataFrame，单元格均为字符串，空串表示无数据。
余额单位为亿元；同比/环比单位为 %。

注意：本接口**没有证券代码**，以 statYear + statMonth 为键。

用法：
    rs = bs.query_money_supply_data_month(start_date="2010-01", end_date="2024-12")
    rows = MoneySupplyMonth.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class MoneySupplyMonth(BaseModel):
    """单月货币供应量数据。"""

    statYear: str
    """统计年度（必填），如 ``"2010"``。"""

    statMonth: str
    """统计月份（必填），如 ``"01"``。"""

    m0Month: Optional[float] = None
    """货币供应量 M0（月，亿元）。"""

    m0YOY: Optional[float] = None
    """M0 同比增速（%）。"""

    m0ChainRelative: Optional[float] = None
    """M0 环比增速（%）。"""

    m1Month: Optional[float] = None
    """货币供应量 M1（月，亿元）。"""

    m1YOY: Optional[float] = None
    """M1 同比增速（%）。"""

    m1ChainRelative: Optional[float] = None
    """M1 环比增速（%）。"""

    m2Month: Optional[float] = None
    """货币供应量 M2（月，亿元）。"""

    m2YOY: Optional[float] = None
    """M2 同比增速（%）。"""

    m2ChainRelative: Optional[float] = None
    """M2 环比增速（%）。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；statYear/statMonth 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["MoneySupplyMonth"]:
        """DataFrame → list[MoneySupplyMonth]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
