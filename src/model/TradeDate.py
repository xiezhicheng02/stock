# -*- coding: utf-8 -*-
"""交易日历：baostock ``query_trade_dates()`` 返回数据对象。

接口说明
--------
获取某段日期范围内的**自然日及是否交易日**标记。返回 pandas DataFrame，
单元格均为字符串，空串表示无数据。

用途：离线判断"某天是否开市"、计算增量同步区间，无需每次联网。

注意：本接口**没有证券代码**，以 calendar_date（自然日）为键，
周末/节假日 is_trading_day=0，周一至周五且非节假日=1。

用法：
    rs = bs.query_trade_dates(start_date="2024-01-01", end_date="2024-12-31")
    rows = TradeDate.from_dataframe(rs.get_data())
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator


class TradeDate(BaseModel):
    """一个自然日的交易日标记。"""

    calendar_date: str
    """日历日期（必填），``YYYY-MM-DD``。"""

    is_trading_day: Optional[int] = None
    """是否交易日：1=交易日（开市），0=非交易日（周末/节假日，闭市）。"""

    @property
    def is_open(self) -> bool:
        """便捷判断：当天是否开市（is_trading_day == 1）。"""
        return self.is_trading_day == 1

    @field_validator("*", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        """空串 ``""`` → None；calendar_date 为空会触发必填校验（数据错误不静默）。"""
        if isinstance(v, str) and v == "":
            return None
        return v

    @classmethod
    def from_dataframe(cls, df) -> list["TradeDate"]:
        """DataFrame → list[TradeDate]；NaN 先转 None，空表返回 []。"""
        if df is None or len(df) == 0:
            return []
        rows = df.astype(object).where(df.notna(), None).to_dict("records")
        return [cls(**row) for row in rows]
