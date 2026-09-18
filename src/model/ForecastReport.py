# -*- coding: utf-8 -*-
"""季频业绩预告：baostock ``query_forecast_report()`` 返回数据对象。

接口说明
--------
获取 A 股业绩预告数据（对净利润变动区间的预判），提供 2003 年至今。
除特殊情形外交易所未强制要求披露。返回 pandas DataFrame，单元格均为字符串，
空串表示无数据。

用法：
    rs = bs.query_forecast_report("sh.600000",
        start_date="2020-01-01", end_date="2024-12-31")
    rows = ForecastReport.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class ForecastReport(BaseModel):
    """业绩预告一行（一次预告一条）。"""

    code: str
    """证券代码，如 ``sh.600000``（必填）。"""

    profitForcastExpPubDate: Optional[str] = None
    """业绩预告发布日期。"""

    profitForcastExpStatDate: Optional[str] = None
    """业绩预告统计日期（报告期末日）。"""

    profitForcastType: Optional[str] = None
    """业绩预告类型（中文），如：略增 / 略降 / 扭亏为盈 / 续盈 / 预增 / 预减 等。"""

    profitForcastAbstract: Optional[str] = None
    """业绩预告摘要（公司对本期净利润的文字说明）。"""

    profitForcastChgPctUp: Optional[float] = None
    """预告归属母公司股东的净利润增长上限（%）。"""

    profitForcastChgPctDwn: Optional[float] = None
    """预告归属母公司股东的净利润增长下限（%）。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；code 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["ForecastReport"]:
        """DataFrame → list[ForecastReport]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
