# -*- coding: utf-8 -*-
"""同步接口：个股清单 + 按目标/个股拉取数据（K线→分红→动态股息率→指标→评分）。

"拉取数据"是长任务（首次全量可能 5~15 分钟），这里同步执行：接口拿进程锁
（pipeline._RUN_LOCK），并发时返回 409。前端显示"拉取中"并放宽超时。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from src.infrastructure.config import config
from src.indicators import indicators
from src.notify import pipeline
from src.infrastructure.persistence import storage
from src.web import deps

log = logging.getLogger("web.sync")
router = APIRouter(prefix="/api", tags=["sync"])


@router.get("/stocks", summary="个股清单（游标分页）")
def stocks(after: str | None = None, limit: int = 60,
           _: None = Depends(deps.require_auth)):
    """左侧个股列表用：游标分页（after=上一页最后一只代码），多取一条判断 has_more。

    用游标而非 OFFSET：深翻页时 OFFSET 要跳过大量索引项（实测 ~0.9s/页），
    游标是常数时间（~0.07s/页）。
    """
    limit = max(1, min(limit, 200))
    conn = deps.conn()
    try:
        items = storage.list_stocks(conn, limit=limit + 1, after=after or None)
        has_more = len(items) > limit
        items = items[:limit]
        return {"items": items, "limit": limit, "after": after,
                "next_after": items[-1]["code"] if (items and has_more) else None,
                "has_more": has_more}
    finally:
        conn.close()


@router.post("/stock/{code}/sync", summary="拉取单只个股数据")
def sync_stock(code: str, full: bool = False, _: None = Depends(deps.require_auth)):
    """拉历史K线 → 分红 → 实时算动态股息率写回 → 评分。"""
    if pipeline.is_busy():
        raise HTTPException(status_code=409, detail="已有同步在进行中，请稍后再试")
    conn = deps.conn()
    try:
        r = pipeline.sync_target(conn, code, ktype="stock", full=full)
    except Exception as e:                      # noqa: BLE001
        log.exception("个股同步失败 %s", code)
        raise HTTPException(status_code=400, detail=f"同步失败：{deps.safe_msg(e)}")
    finally:
        conn.close()
    if r.get("busy"):
        raise HTTPException(status_code=409, detail=r.get("message", "已有同步在进行中"))
    return r


@router.post("/target/{code}/score", summary="立即重算评分（删旧 + 全量重算）")
def target_score(code: str, _: None = Depends(deps.require_auth)):
    """立即重算该标的的评分：**先删掉已有分位/评分，再整条重建 + 重算当日**。

    三种类型都支持（index / portfolio / stock）。以前这里只有两个分支 ——
    portfolio 走 portfolio_score、**其余（含个股）都走 index_score**，
    而 index_score 第一件事是读成分股，个股没有成分股 → 返回 None →
    前端报"成分股无可用的估值数据"。而且它只 upsert 当日一条，既不删旧数据、
    也不重建历史，和按钮名/用户预期都不符。
    """
    t = config.target(code)
    if not t:
        raise HTTPException(status_code=404, detail=f"目标不存在：{code}")
    conn = deps.conn()
    try:
        r = indicators.rebuild_scores(conn, code, t["ktype"])
    finally:
        conn.close()
    if not r or (r.get("score") is None and not r.get("history_rows")):
        raise HTTPException(status_code=400,
                            detail="评分失败：该标的无可用的估值数据（先拉取数据）")
    return r


@router.post("/target/{code}/sync", summary="拉取指数/组合数据")
def sync_target(code: str, full: bool = False, _: None = Depends(deps.require_auth)):
    """指数：查成分股→补拉→拉指数K线→聚合五指标→评分；
    组合：成分股补拉→组合K线合成→评分。"""
    t = config.target(code)
    if not t:
        raise HTTPException(status_code=404, detail=f"目标不存在：{code}")
    if pipeline.is_busy():
        raise HTTPException(status_code=409, detail="已有同步在进行中，请稍后再试")
    conn = deps.conn()
    try:
        r = pipeline.sync_target(conn, code, ktype=t["ktype"], full=full)
    except Exception as e:                      # noqa: BLE001
        log.exception("目标同步失败 %s", code)
        raise HTTPException(status_code=400, detail=f"同步失败：{deps.safe_msg(e)}")
    finally:
        conn.close()
    if r.get("busy"):
        raise HTTPException(status_code=409, detail=r.get("message", "已有同步在进行中"))
    return r
