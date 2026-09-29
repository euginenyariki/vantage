"""Orders, closed trades, and the dashboard summary."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.dependencies import get_current_user
from app.schemas import EquityPoint, OrderOut, PerformanceSummary, TradeOut
from app.services.dashboard import get_equity_curve, get_summary
from trading_engine.persistence.models import Order, Trade

router = APIRouter(tags=["trading"])


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    bot_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Order]:
    stmt = select(Order).where(Order.user_id == user.id)
    if bot_id:
        stmt = stmt.where(Order.bot_id == bot_id)
    if status_filter:
        stmt = stmt.where(Order.status == status_filter.upper())
    stmt = stmt.order_by(desc(Order.created_at)).limit(limit).offset(offset)
    return list(await session.scalars(stmt))


@router.get("/trades", response_model=list[TradeOut])
async def list_trades(
    bot_id: str | None = Query(default=None),
    result: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Trade]:
    stmt = select(Trade).where(Trade.user_id == user.id)
    if bot_id:
        stmt = stmt.where(Trade.bot_id == bot_id)
    if result:
        stmt = stmt.where(Trade.result == result.upper())
    stmt = stmt.order_by(desc(Trade.closed_at)).limit(limit).offset(offset)
    return list(await session.scalars(stmt))


@router.get("/trades/{trade_id}", response_model=TradeOut)
async def get_trade(
    trade_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Trade:
    trade = await session.scalar(
        select(Trade).where(Trade.id == trade_id, Trade.user_id == user.id)
    )
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return trade


@router.get("/dashboard/summary", response_model=PerformanceSummary)
async def dashboard_summary(
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PerformanceSummary:
    return await get_summary(session, user.id)


@router.get("/dashboard/equity", response_model=list[EquityPoint])
async def equity_curve(
    days: int = Query(default=30, ge=1, le=365),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[EquityPoint]:
    return await get_equity_curve(session, user.id, days=days)
