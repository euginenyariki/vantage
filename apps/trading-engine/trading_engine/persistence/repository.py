"""Async engine/session plumbing + repositories that persist the domain objects.

The engine writes to Postgres inside transactions; money is never stored in
Redis. Every state change appends to the corresponding `*_events` table so the
UI can replay exactly what happened.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trading_engine.core.types import (
    Order,
    Position,
    PositionStatus,
    Trade,
)
from trading_engine.persistence.models import (
    SCHEMA,
    Base,
    Bot,
    BotEventLog,
    HistoricalBar,
    OrderEvent,
)
from trading_engine.persistence.models import (
    Order as OrderRow,
)
from trading_engine.persistence.models import (
    Position as PositionRow,
)
from trading_engine.persistence.models import (
    Trade as TradeRow,
)

logger = logging.getLogger("vantage.persistence")

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str) -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    _engine = create_async_engine(
        database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
        future=True,
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _sessionmaker


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("init_engine() must be called first")
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def create_schema() -> None:
    """Used by tests and first boot. Production runs Alembic instead."""
    sm = get_sessionmaker()
    async with sm() as session:
        await session.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        await session.commit()
        await session.run_sync(Base.metadata.create_all)


def _dec(value: Any, default: float = 0.0) -> Decimal:
    if value is None:
        return Decimal(str(default))
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class TradingRepository:
    """Persistence for the engine's hot path. Every method takes an existing
    session so the caller controls the transaction boundary."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- orders -------------------------------------------------------- #
    async def upsert_order(
        self,
        order: Order,
        *,
        user_id: str,
        bot_id: str | None = None,
        from_state: str | None = None,
        to_state: str = "SUBMITTED",
        reason: str | None = None,
        meta: dict | None = None,
    ) -> OrderRow:
        row = await self.session.scalar(
            select(OrderRow).where(OrderRow.client_order_id == order.client_order_id)
        )
        if row is None:
            row = OrderRow(
                id=order.id,
                client_order_id=order.client_order_id,
                user_id=user_id,
                bot_id=bot_id,
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                volume=_dec(order.volume),
                status=order.status,
                mode=order.mode,
            )
            self.session.add(row)
        row.broker_order_id = order.broker_order_id
        row.volume = _dec(order.volume)
        row.filled_volume = _dec(order.filled_volume)
        row.status = order.status
        row.requested_price = _dec(order.requested_price) if order.requested_price else None
        row.filled_price = _dec(order.filled_price) if order.filled_price else None
        row.stop_loss = _dec(order.stop_loss) if order.stop_loss else None
        row.take_profit = _dec(order.take_profit) if order.take_profit else None
        row.commission = _dec(order.commission)
        row.reject_reason = order.reject_reason
        row.mode = order.mode
        if meta:
            row.signal_reason = meta.get("reason", row.signal_reason)
            row.signal_confidence = meta.get("confidence", row.signal_confidence)
        await self.session.flush()

        self.session.add(
            OrderEvent(
                order_id=row.id, from_state=from_state, to_state=to_state,
                reason=reason, meta=meta or {},
            )
        )
        return row

    async def record_order_state(
        self, order_id: str, from_state: str | None, to_state: str, reason: str, meta: dict | None = None
    ) -> None:
        self.session.add(
            OrderEvent(order_id=order_id, from_state=from_state, to_state=to_state,
                       reason=reason, meta=meta or {})
        )

    # ---- positions ------------------------------------------------------ #
    async def upsert_position(
        self, position: Position, *, user_id: str, bot_id: str | None = None,
        order_id: str | None = None,
    ) -> PositionRow:
        row = await self.session.scalar(
            select(PositionRow).where(PositionRow.id == position.id)
        )
        if row is None:
            row = PositionRow(id=position.id, user_id=user_id, bot_id=bot_id, order_id=order_id,
                              symbol=position.symbol, side=position.side,
                              volume=_dec(position.volume), open_price=_dec(position.open_price),
                              current_price=_dec(position.current_price), opened_at=position.opened_at)
            self.session.add(row)
        row.volume = _dec(position.volume)
        row.current_price = _dec(position.current_price)
        row.stop_loss = _dec(position.stop_loss) if position.stop_loss is not None else None
        row.take_profit = _dec(position.take_profit) if position.take_profit is not None else None
        row.status = position.status
        row.commission = _dec(position.commission)
        row.swap = _dec(position.swap)
        row.unrealized_pnl = _dec(position.unrealized_pnl)
        row.realized_pnl = _dec(position.realized_pnl)
        row.break_even_done = position.break_even_done
        if position.closed_at:
            row.closed_at = position.closed_at
        if position.meta.get("close_reason"):
            row.close_reason = str(position.meta["close_reason"])[:64]
        await self.session.flush()
        return row

    async def record_trade(
        self,
        trade: Trade,
        *,
        user_id: str,
        bot_id: str | None = None,
        initial_risk: float | None = None,
    ) -> TradeRow:
        """Persist a closed trade.

        ``initial_risk`` is the currency amount the position was sized against
        (the stop distance in money). It is the only way to express a real R
        multiple, so callers that know it should pass it; without it the column
        stays NULL rather than storing a fabricated value.
        """
        r_multiple: Decimal | None = None
        if initial_risk and initial_risk > 0:
            r_multiple = Decimal(str(round(float(trade.pnl) / initial_risk, 4)))
        row = TradeRow(
            id=trade.id, position_id=trade.position_id, bot_id=bot_id or trade.bot_id,
            user_id=user_id, symbol=trade.symbol, side=trade.side, volume=_dec(trade.volume),
            open_price=_dec(trade.open_price), close_price=_dec(trade.close_price),
            commission=_dec(trade.commission), swap=_dec(trade.swap), pnl=_dec(trade.pnl),
            mode=trade.mode, result="WIN" if trade.pnl > 0 else ("LOSS" if trade.pnl < 0 else "BREAKEVEN"),
            r_multiple=r_multiple,
            close_reason=trade.reason[:64] if trade.reason else None,
            opened_at=trade.opened_at, closed_at=trade.closed_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    # ---- bot bookkeeping -------------------------------------------------- #
    async def log_bot_event(
        self, bot_id: str, message: str, *, level: str = "INFO",
        category: str = "bot", meta: dict | None = None,
    ) -> None:
        self.session.add(
            BotEventLog(bot_id=bot_id, level=level, category=category,
                        message=message, meta=meta or {})
        )

    async def update_bot_state(self, bot_id: str, state: str, reason: str = "") -> None:
        bot = await self.session.get(Bot, bot_id)
        if bot is None:
            return
        bot.state = state
        bot.last_heartbeat_at = datetime.now(UTC)
        # AsyncSession.add() is synchronous; awaiting it would raise TypeError.
        self.session.add(
            BotEventLog(bot_id=bot_id, message=reason or f"State -> {state}",
                        category="state", meta={"state": state, "reason": reason})
        )

    # ---- read helpers for reconciliation ---------------------------------- #
    async def open_positions(self, user_id: str) -> list[PositionRow]:
        result = await self.session.scalars(
            select(PositionRow).where(
                PositionRow.user_id == user_id, PositionRow.status == PositionStatus.OPEN
            )
        )
        return list(result)

    async def orders_for_user(self, user_id: str, limit: int = 500) -> list[OrderRow]:
        result = await self.session.scalars(
            select(OrderRow).where(OrderRow.user_id == user_id)
            .order_by(OrderRow.created_at.desc()).limit(limit)
        )
        return list(result)

    async def recent_trades(self, user_id: str, limit: int = 200) -> list[TradeRow]:
        result = await self.session.scalars(
            select(TradeRow).where(TradeRow.user_id == user_id)
            .order_by(TradeRow.closed_at.desc()).limit(limit)
        )
        return list(result)

    async def daily_realized_pnl(self, user_id: str, since: datetime) -> Decimal:
        rows = await self.session.scalars(
            select(TradeRow).where(TradeRow.user_id == user_id, TradeRow.closed_at >= since)
        )
        return sum((_dec(r.pnl) for r in rows), Decimal("0"))


# --------------------------------------------------------------------------- #
# Historical data (Phase 4)
# --------------------------------------------------------------------------- #
class HistoricalDataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # PostgreSQL's wire protocol caps a single statement at 32767 bind
    # parameters. At 8 columns per bar that is 4095 rows, so a month of M1
    # data (~37k bars) cannot be sent as one multi-row INSERT — the driver
    # raises InterfaceError. Chunk well below the cap.
    _UPSERT_CHUNK_ROWS = 500

    async def upsert_many(self, rows: Sequence[dict]) -> int:
        if not rows:
            return 0
        values = [
            {
                "symbol": r["symbol"].upper(), "timeframe": r["timeframe"],
                "ts": r["timestamp"], "open": _dec(r["open"]), "high": _dec(r["high"]),
                "low": _dec(r["low"]), "close": _dec(r["close"]),
                "volume": _dec(r.get("volume", 0)),
            }
            for r in rows
        ]
        from sqlalchemy.dialects.postgresql import insert
        for start in range(0, len(values), self._UPSERT_CHUNK_ROWS):
            chunk = values[start:start + self._UPSERT_CHUNK_ROWS]
            stmt = insert(HistoricalBar).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "timeframe", "ts"],
                set_={
                    "open": stmt.excluded.open, "high": stmt.excluded.high,
                    "low": stmt.excluded.low, "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                },
            )
            await self.session.execute(stmt)
        await self.session.flush()
        return len(values)

    async def load(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[HistoricalBar]:
        result = await self.session.scalars(
            select(HistoricalBar)
            .where(
                HistoricalBar.symbol == symbol.upper(),
                HistoricalBar.timeframe == timeframe,
                HistoricalBar.ts >= start,
                HistoricalBar.ts <= end,
            )
            .order_by(HistoricalBar.ts)
        )
        return list(result)

    async def count(self, symbol: str, timeframe: str) -> int:
        from sqlalchemy import func as sa_func
        return await self.session.scalar(
            select(sa_func.count()).select_from(HistoricalBar).where(
                HistoricalBar.symbol == symbol.upper(),
                HistoricalBar.timeframe == timeframe,
            )
        ) or 0
