# -*- coding: utf-8 -*-
"""季频业绩快报：baostock ``query_performance_express_report()`` 返回数据对象。

接口说明
--------
获取 A 股业绩快报数据（未经审计的全年/半年关键财务指标），提供 2006 年至今。
除特殊情形外交易所未强制要求披露。返回 pandas DataFrame，单元格均为字符串，
空串表示无数据。

用法：
    rs = bs.query_performance_express_report("sh.600000",
        start_date="2020-01-01", end_date="2024-12-31")
    rows = PerformanceExpress.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class PerformanceExpress(BaseModel):
    """业绩快报一行（一段统计期一份快报）。"""

    code: str
    """证券代码，如 ``sh.600000``（必填）。"""

    performanceExpPubDate: Optional[str] = None
    """业绩快报披露日。"""

    performanceExpStatDate: Optional[str] = None
    """业绩快报统计日期（报告期末日）。"""

    performanceExpUpdateDate: Optional[str] = None
    """业绩快报最新披露日（若后续修订过，此字段与首次披露日不同）。"""

    performanceExpressTotalAsset: Optional[float] = None
    """业绩快报总资产（元）。"""

    performanceExpressNetAsset: Optional[float] = None
    """业绩快报净资产（元）。"""

    performanceExpressEPSChgPct: Optional[float] = None
    """每股收益增长率（%）。"""

    performanceExpressROEWa: Optional[float] = None
    """业绩快报加权净资产收益率 ROE（%）。"""

    performanceExpressEPSDiluted: Optional[float] = None
    """业绩快报每股收益 EPS（摊薄）。"""

    performanceExpressGRYOY: Optional[float] = None
    """营业总收入同比增长率。"""

    performanceExpressOPYOY: Optional[float] = None
    """营业利润同比增长率。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；code 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["PerformanceExpress"]:
        """DataFrame → list[PerformanceExpress]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
