# -*- coding: utf-8 -*-
"""货币供应量（年底余额）：baostock ``query_money_supply_data_year()`` 返回数据对象。

接口说明
--------
获取央行公布的**年度货币供应量**（M0/M1/M2）年底余额与同比。
返回 pandas DataFrame，单元格均为字符串，空串表示无数据。
余额单位为亿元；同比单位为 %。

注意：本接口**没有证券代码**，以 statYear（年度）为键。

用法：
    rs = bs.query_money_supply_data_year(start_date="2010", end_date="2024")
    rows = MoneySupplyYear.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class MoneySupplyYear(BaseModel):
    """年度货币供应量年底余额。"""

    statYear: str
    """统计年度（必填），如 ``"2010"``。"""

    m0Year: Optional[float] = None
    """年货币供应量 M0（年底余额，亿元）。"""

    m0YearYOY: Optional[float] = None
    """M0 同比增速（%）。"""

    m1Year: Optional[float] = None
    """年货币供应量 M1（年底余额，亿元）。"""

    m1YearYOY: Optional[float] = None
    """M1 同比增速（%）。"""

    m2Year: Optional[float] = None
    """年货币供应量 M2（年底余额，亿元）。"""

    m2YearYOY: Optional[float] = None
    """M2 同比增速（%）。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；statYear 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["MoneySupplyYear"]:
        """DataFrame → list[MoneySupplyYear]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
