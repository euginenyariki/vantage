"""SQLAlchemy models. PostgreSQL is the source of truth for money; Redis is
only ever a transport (rule 8).

All timestamps are stored as timezone-aware UTC. Money is Numeric(18,4) —
never float in the database. Every state transition gets an `*_events` table so
the UI can show a full audit trail of what happened and when.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from trading_engine.core.types import (
    BotState,
    OrderStatus,
    OrderType,
    PositionStatus,
    Side,
    TradingMode,
    TrailingMode,
)

SCHEMA = "vantage"


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    # Applying the schema to the metadata keeps every table (and its indexes
    # and constraints) inside "vantage" without repeating __table_args__ on
    # each model. A bare {"schema": ...} dict in __table_args__ is not accepted
    # by SQLAlchemy 2.x.
    metadata = MetaData(schema=SCHEMA)
    type_annotation_map = {dict: JSON, list: JSON}


# --------------------------------------------------------------------------- #
# Auth / tenancy
# --------------------------------------------------------------------------- #
class Role(str, enum.Enum):
    USER = "USER"
    ADMIN = "ADMIN"
    SUPER_ADMIN = "SUPER_ADMIN"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    role: Mapped[Role] = mapped_column(
        SAEnum(Role, name="user_role", values_callable=lambda e: [m.value for m in e]),
        default=Role.USER, nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    two_factor_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    terms_accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sessions: Mapped[list[UserSession]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_sessions_user_active", "user_id", "revoked_at")),

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="sessions")


class EmailVerification(Base):
    __tablename__ = "email_verifications"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), default="verify_email", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TwoFactorAuth(Base):
    __tablename__ = "two_factor_auth"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), unique=True
    )
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_codes_hash: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_user_time", "user_id", "created_at"),
        Index("ix_audit_action", "action"),
    )

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="SET NULL")
    )
    actor_email: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(48))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# --------------------------------------------------------------------------- #
# Broker credentials
# --------------------------------------------------------------------------- #
class BrokerConnection(Base):
    __tablename__ = "broker_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_broker_user_name"),
    )

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    broker: Mapped[str] = mapped_column(String(32), default="metaapi", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    login_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    server_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    key_version: Mapped[str] = mapped_column(String(16), default="v1", nullable=False)
    connection_state: Mapped[str] = mapped_column(String(24), default="DISCONNECTED")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    balance: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    equity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --------------------------------------------------------------------------- #
# Trading
# --------------------------------------------------------------------------- #
class Bot(Base):
    __tablename__ = "bots"
    __table_args__ = (Index("ix_bots_user_state", "user_id", "state")),

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    broker_connection_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.broker_connections.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    strategy: Mapped[str] = mapped_column(String(48), default="hft_scalper", nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), default="M1", nullable=False)
    mode: Mapped[TradingMode] = mapped_column(
        SAEnum(TradingMode, name="trading_mode", values_callable=lambda e: [m.value for m in e]),
        default=TradingMode.PAPER, nullable=False,
    )
    state: Mapped[BotState] = mapped_column(
        SAEnum(BotState, name="bot_state", values_callable=lambda e: [m.value for m in e]),
        default=BotState.OFFLINE, nullable=False,
    )
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_config: Mapped[dict] = mapped_column(JSON, default=dict)
    trailing_config: Mapped[dict] = mapped_column(JSON, default=dict)
    live_granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    live_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_start: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_orders_client_id"),
        Index("ix_orders_bot_created", "bot_id", "created_at"),
        Index("ix_orders_broker_id", "broker_order_id"),
    )

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    bot_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.bots.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[Side] = mapped_column(
        SAEnum(Side, name="order_side", values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    order_type: Mapped[OrderType] = mapped_column(
        SAEnum(OrderType, name="order_type", values_callable=lambda e: [m.value for m in e]),
        default=OrderType.MARKET, nullable=False,
    )
    volume: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    filled_volume: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status", values_callable=lambda e: [m.value for m in e]),
        default=OrderStatus.PENDING, nullable=False, index=True,
    )
    requested_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    filled_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    commission: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[TradingMode] = mapped_column(
        SAEnum(TradingMode, name="order_mode", values_callable=lambda e: [m.value for m in e]),
        default=TradingMode.PAPER, nullable=False,
    )
    signal_reason: Mapped[str | None] = mapped_column(Text)
    signal_confidence: Mapped[float | None] = mapped_column(Float)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    events: Mapped[list[OrderEvent]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderEvent.created_at"
    )


class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (Index("ix_order_events_order", "order_id", "created_at")),

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    order_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.orders.id", ondelete="CASCADE")
    )
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    order: Mapped[Order] = relationship(back_populates="events")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_bot_status", "bot_id", "status"),
        Index("ix_positions_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    broker_position_id: Mapped[str | None] = mapped_column(String(64), index=True)
    order_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.orders.id", ondelete="SET NULL")
    )
    bot_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.bots.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[Side] = mapped_column(
        SAEnum(Side, name="position_side", values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    volume: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    open_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    status: Mapped[PositionStatus] = mapped_column(
        SAEnum(PositionStatus, name="position_status", values_callable=lambda e: [m.value for m in e]),
        default=PositionStatus.OPEN, nullable=False,
    )
    commission: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    swap: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    mode: Mapped[TradingMode] = mapped_column(
        SAEnum(TradingMode, name="position_mode", values_callable=lambda e: [m.value for m in e]),
        default=TradingMode.PAPER, nullable=False,
    )
    trailing_mode: Mapped[TrailingMode] = mapped_column(
        SAEnum(TrailingMode, name="trailing_mode", values_callable=lambda e: [m.value for m in e]),
        default=TrailingMode.FIXED, nullable=False,
    )
    break_even_done: Mapped[bool] = mapped_column(Boolean, default=False)
    close_reason: Mapped[str | None] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_user_closed", "user_id", "closed_at"),
        Index("ix_trades_symbol_closed", "symbol", "closed_at"),
    )

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    position_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.positions.id", ondelete="SET NULL")
    )
    bot_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.bots.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[Side] = mapped_column(
        SAEnum(Side, name="trade_side", values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    volume: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    open_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    commission: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    swap: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    mode: Mapped[TradingMode] = mapped_column(
        SAEnum(TradingMode, name="trade_mode", values_callable=lambda e: [m.value for m in e]),
        default=TradingMode.PAPER, nullable=False,
    )
    result: Mapped[str] = mapped_column(String(8), default="OPEN")   # OPEN | WIN | LOSS
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    close_reason: Mapped[str | None] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class BotEventLog(Base):
    __tablename__ = "bot_event_logs"
    __table_args__ = (Index("ix_bot_events_bot_time", "bot_id", "created_at")),

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.bots.id", ondelete="CASCADE")
    )
    level: Mapped[str] = mapped_column(String(12), default="INFO")
    category: Mapped[str] = mapped_column(String(32), default="bot")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (Index("ix_recon_user_time", "user_id", "created_at")),

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    broker_connection_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.broker_connections.id", ondelete="CASCADE")
    )
    trigger: Mapped[str] = mapped_column(String(24), default="scheduled")
    checked_positions: Mapped[int] = mapped_column(Integer, default=0)
    checked_orders: Mapped[int] = mapped_column(Integer, default=0)
    discrepancies: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="OK")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# --------------------------------------------------------------------------- #
# Backtesting — kept in separate tables from operational data (Phase 4)
# --------------------------------------------------------------------------- #
class BacktestJob(Base):
    __tablename__ = "backtest_jobs"
    __table_args__ = (Index("ix_bt_user_created", "user_id", "created_at")),

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    strategy: Mapped[str] = mapped_column(String(48), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), default="M1")
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    risk_percent: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    spread_pips: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0)
    commission_per_lot: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=0)
    slippage_pips: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    progress: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    job_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.backtest_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    final_equity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    roi_percent: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    max_drawdown_percent: Mapped[Decimal] = mapped_column(Numeric(8, 4))
    max_drawdown_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    average_trade: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    best_trade: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    worst_trade: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    avg_win: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    avg_loss: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    total_commission: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)
    drawdown_curve: Mapped[list] = mapped_column(JSON, default=list)
    monthly_returns: Mapped[list] = mapped_column(JSON, default=list)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HistoricalBar(Base):
    """Separate from operational tables — historical data is append-only and
    never mixed with live/paper data (rule 14)."""

    __tablename__ = "historical_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "ts", name="uq_hist_bar"),
        Index("ix_hist_lookup", "symbol", "timeframe", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    tick_volume: Mapped[BigInteger | None] = mapped_column(BigInteger)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_user_read", "user_id", "read_at")),

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), index=True
    )
    level: Mapped[str] = mapped_column(String(12), default="info")
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    action_url: Mapped[str | None] = mapped_column(String(256))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
