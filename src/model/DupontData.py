# -*- coding: utf-8 -*-
"""季频杜邦指数：baostock ``query_dupont_data()`` 返回数据对象。

接口说明
--------
获取 A 股季频杜邦分解指标（把 ROE 拆成乘数链），提供 2007 年至今数据。
返回 pandas DataFrame，单元格均为字符串，空串表示无数据。

杜邦拆解链
----------
ROE ≈ 销售净利率 × 资产周转率 × 权益乘数 × 税负 × 利息负担 × 母子公司占比。

用法：
    rs = bs.query_dupont_data(code="sh.600000", year=2024, quarter=2)
    rows = DupontData.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class DupontData(BaseModel):
    """单季杜邦分解指标。"""

    code: str
    """证券代码，如 ``sh.600000``（必填）。"""

    pubDate: Optional[str] = None
    """公司发布财报的日期。"""

    statDate: Optional[str] = None
    """财报统计季度的最后一天。"""

    dupontROE: Optional[float] = None
    """净资产收益率：归母净利润 /
    [(期初归母权益 + 期末归母权益) / 2] * 100%。"""

    dupontAssetStoEquity: Optional[float] = None
    """权益乘数：平均总资产 / 平均归母股东权益，反映财务杠杆高低。"""

    dupontAssetTurn: Optional[float] = None
    """总资产周转率：营业总收入 /
    [(期初资产总额 + 期末资产总额) / 2]，反映资产管理效率。"""

    dupontPnitoni: Optional[float] = None
    """归母净利润 / 净利润，反映母公司对子公司的控股比例；
    追加投资、扩大持股比例时本指标上升。"""

    dupontNitogr: Optional[float] = None
    """净利润 / 营业总收入，反映销售获利率（净利率）。"""

    dupontTaxBurden: Optional[float] = None
    """净利润 / 利润总额 = 1 - 所得税/利润总额，反映税负水平；
    比值越高税负越低。"""

    dupontIntburden: Optional[float] = None
    """利润总额 / 息税前利润 = 1 - 利息费用/息税前利润，反映利息负担；
    比值越高利息负担越轻。"""

    dupontEbittogr: Optional[float] = None
    """息税前利润 / 营业总收入，反映经营利润率，
    即可供全体投资人（股东+债权人）分配的盈利占营收比重。"""

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；code 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["DupontData"]:
        """DataFrame → list[DupontData]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
