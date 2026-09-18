# -*- coding: utf-8 -*-
"""存款准备金率：baostock ``query_required_reserve_ratio_data()`` 返回数据对象。

接口说明
--------
获取央行**存款准备金率**历次调整数据，分大型 / 中小型金融机构两档，
同时给出调整前与调整后的值。返回 pandas DataFrame，单元格均为字符串，
空串表示无数据。比率单位为 %。

注意：本接口**没有证券代码**，以 pubDate（公告日期）为键。

用法：
    rs = bs.query_required_reserve_ratio_data(start_date="2010-01-01", end_date="2024-12-31")
    rows = RequiredReserveRatio.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class RequiredReserveRatio(BaseModel):
    """一次存款准备金率调整公告（一个公告日一条）。"""

    pubDate: str
    """公告日期（必填）。"""

    effectiveDate: Optional[str] = None
    """正式生效日期。"""

    bigInstitutionsRatioPre: Optional[float] = None
    """大型存款类金融机构人民币准备金率——调整前（%）。"""

    bigInstitutionsRatioAfter: Optional[float] = None
    """大型存款类金融机构人民币准备金率——调整后（%）。"""

    mediumInstitutionsRatioPre: Optional[float] = None
    """中小型存款类金融机构人民币准备金率——调整前（%）。"""

    mediumInstitutionsRatioAfter: Optional[float] = None
    """中小型存款类金融机构人民币准备金率——调整后（%）。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；pubDate 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["RequiredReserveRatio"]:
        """DataFrame → list[RequiredReserveRatio]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
