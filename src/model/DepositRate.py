# -*- coding: utf-8 -*-
"""存款利率：baostock ``query_deposit_rate_data()`` 返回数据对象。

接口说明
--------
获取央行公布的**存款基准利率**历次调整数据。返回 pandas DataFrame，
单元格均为字符串，空串表示无数据。利率单位为 %。

注意：本接口**没有证券代码**，以 pubDate（发布日期）为键。

用法：
    rs = bs.query_deposit_rate_data(start_date="2015-01-01", end_date="2024-12-31")
    rows = DepositRate.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class DepositRate(BaseModel):
    """一次存款利率调整公告（一个发布日一条）。"""

    pubDate: str
    """利率发布/调整日期（必填）。"""

    demandDepositRate: Optional[float] = None
    """活期存款利率（不定期，%）。"""

    fixedDepositRate3Month: Optional[float] = None
    """定期存款（三个月）利率（%）。"""

    fixedDepositRate6Month: Optional[float] = None
    """定期存款（半年）利率（%）。"""

    fixedDepositRate1Year: Optional[float] = None
    """定期整存整取（一年）利率（%）。"""

    fixedDepositRate2Year: Optional[float] = None
    """定期整存整取（二年）利率（%）。"""

    fixedDepositRate3Year: Optional[float] = None
    """定期整存整取（三年）利率（%）。"""

    fixedDepositRate5Year: Optional[float] = None
    """定期整存整取（五年）利率（%）。"""

    installmentFixedDepositRate1Year: Optional[float] = None
    """零存整取 / 整存零取 / 存本取息定期存款（一年）利率（%）。"""

    installmentFixedDepositRate3Year: Optional[float] = None
    """零存整取 / 整存零取 / 存本取息定期存款（三年）利率（%）。"""

    installmentFixedDepositRate5Year: Optional[float] = None
    """零存整取 / 整存零取 / 存本取息定期存款（五年）利率（%）。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；pubDate 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["DepositRate"]:
        """DataFrame → list[DepositRate]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
