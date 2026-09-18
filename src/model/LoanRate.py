# -*- coding: utf-8 -*-
"""贷款利率：baostock ``query_loan_rate_data()`` 返回数据对象。

接口说明
--------
获取央行公布的**贷款基准利率**历次调整数据。返回 pandas DataFrame，
单元格均为字符串，空串表示无数据。利率单位为 %。

注意：本接口**没有证券代码**，以 pubDate（发布日期）为键。

用法：
    rs = bs.query_loan_rate_data(start_date="2010-01-01", end_date="2024-12-31")
    rows = LoanRate.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class LoanRate(BaseModel):
    """一次贷款利率调整公告（一个发布日一条）。"""

    pubDate: str
    """利率发布/调整日期（必填）。"""

    loanRate6Month: Optional[float] = None
    """6 个月以内贷款利率（%）。"""

    loanRate6MonthTo1Year: Optional[float] = None
    """6 个月至 1 年贷款利率（%）。"""

    loanRate1YearTo3Year: Optional[float] = None
    """1 年至 3 年贷款利率（%）。"""

    loanRate3YearTo5Year: Optional[float] = None
    """3 年至 5 年贷款利率（%）。"""

    loanRateAbove5Year: Optional[float] = None
    """5 年以上贷款利率（%）。"""

    mortgateRateBelow5Year: Optional[float] = None
    """5 年以下住房公积金贷款利率（%）。"""

    mortgateRateAbove5Year: Optional[float] = None
    """5 年以上住房公积金贷款利率（%）。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；pubDate 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["LoanRate"]:
        """DataFrame → list[LoanRate]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
