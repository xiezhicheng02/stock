# -*- coding: utf-8 -*-
"""季频偿债能力：baostock ``query_balance_data()`` 返回数据对象。

接口说明
--------
获取 A 股季频偿债能力（流动/速动/现金比率、资产负债率等），提供 2007 年至今数据。
返回 pandas DataFrame，单元格均为字符串，空串表示无数据。

用法：
    rs = bs.query_balance_data(code="sh.600000", year=2024, quarter=2)
    rows = BalanceData.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class BalanceData(BaseModel):
    """单季偿债能力指标。"""

    code: str
    """证券代码，如 ``sh.600000``（必填）。"""

    pubDate: Optional[str] = None
    """公司发布财报的日期。"""

    statDate: Optional[str] = None
    """财报统计季度的最后一天。"""

    currentRatio: Optional[float] = None
    """流动比率：流动资产 / 流动负债。"""

    quickRatio: Optional[float] = None
    """速动比率：(流动资产 - 存货净额) / 流动负债。"""

    cashRatio: Optional[float] = None
    """现金比率：(货币资金 + 交易性金融资产) / 流动负债。"""

    YOYLiability: Optional[float] = None
    """总负债同比增长率（%）：(本期总负债 - 上年同期总负债) /
    |上年同期总负债| * 100%。"""

    liabilityToAsset: Optional[float] = None
    """资产负债率：负债总额 / 资产总额。"""

    assetToEquity: Optional[float] = None
    """权益乘数：资产总额 / 股东权益总额 = 1 / (1 - 资产负债率)。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；code 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["BalanceData"]:
        """DataFrame → list[BalanceData]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
