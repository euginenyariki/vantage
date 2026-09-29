"""Performance analytics endpoints.

All figures are derived from the user's own closed trades and are scoped to the
authenticated user. There is no endpoint here that reports a metric the data
cannot support.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from trading_engine.persistence.models import User

from app.core.database import get_session
from app.dependencies import get_current_user, rate_limit
from app.schemas import (
    AnalyticsReport,
    EquityPointDaily,
    PerformanceMetrics,
    StrategyPerformance,
    SymbolPerformance,
)
from app.services import analytics

router = APIRouter(prefix="/analytics", tags=["analytics"])

# Analytics is a read-only view over the user's own trades.
_READ = rate_limit("analytics-read", limit=120, window_seconds=60)


@router.get("/performance", response_model=PerformanceMetrics)
async def performance(
    days: int = Query(90, ge=1, le=730),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_READ),
) -> PerformanceMetrics:
    """Win rate, expectancy, drawdown, streaks and PnL-based Sharpe."""
    return PerformanceMetrics(**await analytics.get_performance(session, user.id, days))


@router.get("/report", response_model=AnalyticsReport)
async def report(
    days: int = Query(90, ge=1, le=730),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_READ),
) -> AnalyticsReport:
    """Everything the analytics page needs in a single round trip."""
    metrics = await analytics.get_performance(session, user.id, days)
    equity = await analytics.get_equity_series(session, user.id, days)
    by_symbol = await analytics.get_by_symbol(session, user.id, days)
    by_strategy = await analytics.get_by_strategy(session, user.id, days)
    return AnalyticsReport(
        metrics=PerformanceMetrics(**metrics),
        equity=[EquityPointDaily(**p) for p in equity],
        by_symbol=[SymbolPerformance(**s) for s in by_symbol],
        by_strategy=[StrategyPerformance(**s) for s in by_strategy],
    )
