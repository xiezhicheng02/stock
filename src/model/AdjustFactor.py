# -*- coding: utf-8 -*-
"""日频复权因子：query_daily_adjust_factor 返回。"""

from pydantic import BaseModel, Field


class AdjustFactor(BaseModel):
    """指定日期全部证券的复权因子。

    与 ``query_daily_history_k_AStock`` 同日拉取，用于把前复权 K 线
    换算成后复权/不复权口径。

    Attributes:
        date: 查询日期 YYYY-MM-DD（接口未直接返回，调用方填入）。
        code: 证券代码。
        dividOperateDate: 除权除息日期。
        foreAdjustFactor: 向前复权因子 = 除权除息日前一交易日收盘价 /
            除权除息日最近一交易日前收盘价。
        backAdjustFactor: 向后复权因子 = 除权除息日最近一交易日前收盘价 /
            除权除息日前一交易日收盘价。
        adjustFactor: 本次复权因子。
    """

    date: str = Field(..., description="查询日期 YYYY-MM-DD")
    code: str = Field(..., description="证券代码")
    dividOperateDate: str | None = Field(None, description="除权除息日期")
    foreAdjustFactor: float | None = Field(None, description="向前复权因子")
    backAdjustFactor: float | None = Field(None, description="向后复权因子")
    adjustFactor: float | None = Field(None, description="本次复权因子")

    @staticmethod
    def from_dataframe(df):
        """从 baostock DataFrame 转 list[AdjustFactor]。"""
        if df is None or len(df) == 0:
            return []
        out = []
        for _, r in df.iterrows():
            out.append(AdjustFactor(
                date=r.get("date", ""),
                code=r.get("code", ""),
                dividOperateDate=r.get("dividOperateDate") or None,
                foreAdjustFactor=_f(r.get("foreAdjustFactor")),
                backAdjustFactor=_f(r.get("backAdjustFactor")),
                adjustFactor=_f(r.get("adjustFactor")),
            ))
        return out


def _f(v):
    if v in (None, "", "NaN"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
