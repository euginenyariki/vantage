"""Dashboard aggregation queries.

These read directly from the ORM models because the shared engine repository
only covers orders, positions, trades and bot bookkeeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import EquityPoint, PerformanceSummary
from trading_engine.core.types import BotState, PositionStatus
from trading_engine.persistence.models import (
    Bot,
    BrokerConnection,
    Position,
    Trade,
)


def _f(value) -> float:
    """Decimal/None -> float, treating None as 0."""
    return float(value) if value is not None else 0.0


async def get_summary(session: AsyncSession, user_id: str) -> PerformanceSummary:
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # --- balance/equity from connected broker accounts, else from closed P/L --
    conn_row = await session.scalar(
        select(BrokerConnection)
        .where(BrokerConnection.user_id == user_id)
        .order_by(BrokerConnection.last_synced_at.desc().nullslast())
        .limit(1)
    )
    realized_total = _f(
        await session.scalar(select(func.coalesce(func.sum(Trade.pnl), 0)).where(Trade.user_id == user_id))
    )
    realized_today = _f(
        await session.scalar(
            select(func.coalesce(func.sum(Trade.pnl), 0)).where(
                Trade.user_id == user_id, Trade.closed_at >= today
            )
        )
    )
    unrealized = _f(
        await session.scalar(
            select(func.coalesce(func.sum(Position.unrealized_pnl), 0)).where(
                Position.user_id == user_id, Position.status == PositionStatus.OPEN
            )
        )
    )

    if conn_row is not None and conn_row.balance is not None:
        balance = _f(conn_row.balance)
        equity = _f(conn_row.equity) if conn_row.equity is not None else balance + unrealized
    else:
        # No broker linked yet: report the P/L the user has actually made.
        balance = realized_total
        equity = realized_total + unrealized

    # --- trade stats -------------------------------------------------------- #
    counts = await session.execute(
        select(
            func.count(Trade.id),
            func.coalesce(func.sum(Trade.pnl), 0),
        ).where(Trade.user_id == user_id)
    )
    total_trades, net_pnl = counts.one()
    total_trades = int(total_trades or 0)

    wins = int(
        await session.scalar(
            select(func.count(Trade.id)).where(Trade.user_id == user_id, Trade.pnl > 0)
        )
        or 0
    )
    losses = int(
        await session.scalar(
            select(func.count(Trade.id)).where(Trade.user_id == user_id, Trade.pnl < 0)
        )
        or 0
    )
    gross_profit = _f(
        await session.scalar(
            select(func.coalesce(func.sum(Trade.pnl), 0)).where(Trade.user_id == user_id, Trade.pnl > 0)
        )
    )
    gross_loss = abs(
        _f(
            await session.scalar(
                select(func.coalesce(func.sum(Trade.pnl), 0)).where(
                    Trade.user_id == user_id, Trade.pnl < 0
                )
            )
        )
    )

    open_positions = int(
        await session.scalar(
            select(func.count(Position.id)).where(
                Position.user_id == user_id, Position.status == PositionStatus.OPEN
            )
        )
        or 0
    )
    total_bots = int(
        await session.scalar(select(func.count(Bot.id)).where(Bot.user_id == user_id)) or 0
    )
    active_bots = int(
        await session.scalar(
            select(func.count(Bot.id)).where(Bot.user_id == user_id, Bot.state == BotState.RUNNING)
        )
        or 0
    )

    profit_factor = None
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 4)

    return PerformanceSummary(
        balance=round(balance, 2),
        equity=round(equity, 2),
        open_positions=open_positions,
        total_trades=total_trades,
        winning_trades=wins,
        losing_trades=losses,
        win_rate=round(wins / total_trades * 100, 2) if total_trades else 0.0,
        net_pnl=round(_f(net_pnl), 2),
        gross_profit=round(gross_profit, 2),
        gross_loss=round(gross_loss, 2),
        profit_factor=profit_factor,
        realized_today=round(realized_today, 2),
        active_bots=active_bots,
        total_bots=total_bots,
    )


async def get_equity_curve(
    session: AsyncSession, user_id: str, days: int = 30
) -> list[EquityPoint]:
    """Cumulative realized P/L by day, oldest first.

    Built from closed trades rather than stored snapshots, so it is always
    consistent with the trade table.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                func.date_trunc("day", Trade.closed_at).label("day"),
                func.coalesce(func.sum(Trade.pnl), 0).label("pnl"),
            )
            .where(Trade.user_id == user_id, Trade.closed_at >= since)
            .group_by("day")
            .order_by("day")
        )
    ).all()

    points: list[EquityPoint] = []
    running = 0.0
    for day, pnl in rows:
        running += _f(pnl)
        points.append(EquityPoint(t=day, equity=round(running, 2), realized=round(_f(pnl), 2)))
    return points


def bots_for_user(session: AsyncSession, user_id: str) -> Select:
    return select(Bot).where(Bot.user_id == user_id).order_by(Bot.created_at.desc())
