"""Pydantic request/response models for the public API."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)

TradingModeLiteral = Literal["DEMO", "PAPER", "LIVE"]
BotStateLiteral = Literal[
    "OFFLINE", "STARTING", "RUNNING", "PAUSING", "PAUSED",
    "STOPPING", "STOPPED", "ERROR", "EMERGENCY_STOP",
]
SideLiteral = Literal["BUY", "SELL"]


def _optional_uuid(value: str | None) -> str | None:
    """Accept None/"" as "unset"; reject anything else that is not a UUID.

    Without this an empty string reaches asyncpg as a UUID column and surfaces
    as a 500 DataError instead of a 422.
    """
    if value is None or value == "":
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ValueError("must be a valid UUID") from None


OptionalUuid = Annotated[str | None, BeforeValidator(_optional_uuid)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class RegisterRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=10, max_length=256)
    country: str = Field(min_length=2, max_length=2)
    accepted_terms: bool

    @field_validator("password")
    @classmethod
    def strong_password(cls, v: str) -> str:
        if v.lower() == v and v.upper() == v:
            raise ValueError("Password must mix upper and lower case")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain a digit")
        return v

    @field_validator("country")
    @classmethod
    def upper_country(cls, v: str) -> str:
        return v.upper()

    @field_validator("accepted_terms")
    @classmethod
    def must_accept(cls, v: bool) -> bool:
        if not v:
            raise ValueError("You must accept the terms to create an account")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenPair(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class UserOut(ORMModel):
    id: str
    email: EmailStr
    name: str
    country: str
    role: str
    is_active: bool
    is_email_verified: bool
    two_factor_enabled: bool
    created_at: datetime


class AuthResponse(BaseModel):
    user: UserOut
    tokens: TokenPair


# --------------------------------------------------------------------------- #
# Bots
# --------------------------------------------------------------------------- #
class RiskConfigIn(BaseModel):
    max_risk_percent_per_trade: float = Field(default=1.0, gt=0, le=10)
    max_daily_loss_percent: float = Field(default=5.0, gt=0, le=50)
    max_total_risk_percent: float = Field(default=6.0, gt=0, le=50)
    max_concurrent_positions: int = Field(default=3, ge=1, le=20)
    max_spread_pips: float = Field(default=3.0, gt=0, le=50)
    max_daily_trades: int = Field(default=20, ge=1, le=500)
    max_open_minutes: int = Field(default=120, ge=1, le=1440)
    allowed_symbols: list[str] = Field(default_factory=list)
    allowed_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    trading_window: list[int] | None = None


class TrailingConfigIn(BaseModel):
    mode: Literal["FIXED", "STEP", "BREAK_EVEN", "ATR", "PERCENTAGE"] = "FIXED"
    distance_pips: float = Field(default=10.0, gt=0, le=500)
    step_pips: float = Field(default=0.0, ge=0, le=500)
    break_even_trigger_pips: float = Field(default=5.0, ge=0, le=500)
    break_even_offset_pips: float = Field(default=0.0, ge=0, le=500)
    percentage: float = Field(default=0.0, ge=0, le=50)
    atr_multiplier: float = Field(default=2.0, gt=0, le=20)


class BotCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    symbol: str = Field(min_length=1, max_length=24)
    strategy: str = Field(default="hft_scalper", max_length=48)
    timeframe: str = Field(default="M1", max_length=8)
    mode: TradingModeLiteral = "PAPER"
    auto_start: bool = False
    broker_connection_id: OptionalUuid = None
    risk: RiskConfigIn = Field(default_factory=RiskConfigIn)
    trailing: TrailingConfigIn = Field(default_factory=TrailingConfigIn)

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return v.upper()


class BotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    mode: TradingModeLiteral | None = None
    auto_start: bool | None = None
    live_granted: bool | None = None
    risk: RiskConfigIn | None = None
    trailing: TrailingConfigIn | None = None


class BotOut(ORMModel):
    id: str
    name: str
    symbol: str
    strategy: str
    timeframe: str
    mode: str
    state: str
    auto_start: bool
    live_granted: bool
    broker_connection_id: str | None = None
    risk_config: dict[str, Any] = {}
    trailing_config: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    last_heartbeat_at: datetime | None = None
    created_at: datetime


class BotActionResponse(BaseModel):
    ok: bool
    message: str
    bot_id: str | None = None
    state: str | None = None


# --------------------------------------------------------------------------- #
# Trading data
# --------------------------------------------------------------------------- #
class PositionOut(ORMModel):
    id: str
    bot_id: str | None = None
    symbol: str
    side: str
    volume: Decimal
    open_price: Decimal
    current_price: Decimal
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    commission: Decimal
    swap: Decimal
    status: str
    mode: str
    break_even_done: bool
    opened_at: datetime
    closed_at: datetime | None = None


class PositionModify(BaseModel):
    stop_loss: float | None = None
    take_profit: float | None = None


class OrderOut(ORMModel):
    id: str
    client_order_id: str
    broker_order_id: str | None = None
    bot_id: str | None = None
    symbol: str
    side: str
    order_type: str
    volume: Decimal
    filled_volume: Decimal
    status: str
    requested_price: Decimal | None = None
    filled_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    commission: Decimal
    reject_reason: str | None = None
    mode: str
    created_at: datetime


class TradeOut(ORMModel):
    id: str
    position_id: str | None = None
    bot_id: str | None = None
    symbol: str
    side: str
    volume: Decimal
    open_price: Decimal
    close_price: Decimal
    commission: Decimal
    swap: Decimal
    pnl: Decimal
    mode: str
    result: str
    r_multiple: Decimal | None = None
    close_reason: str | None = None
    opened_at: datetime
    closed_at: datetime


class PerformanceSummary(BaseModel):
    balance: float = 0.0
    equity: float = 0.0
    open_positions: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float | None = None
    realized_today: float = 0.0
    active_bots: int = 0
    total_bots: int = 0


class EquityPoint(BaseModel):
    t: datetime
    equity: float
    realized: float = 0.0


class NotificationOut(ORMModel):
    id: str
    title: str
    body: str
    level: str
    action_url: str | None = None
    read_at: datetime | None = None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
class SymbolInfo(BaseModel):
    symbol: str
    digits: int
    pip_size: float
    contract_size: float
    tick_size: float
    tick_value: float
    min_volume: float
    max_volume: float
    volume_step: float
    commission_per_lot: float
    min_stop_distance: float
    max_spread_pips: float
    asset_class: str = "forex"


class Quote(BaseModel):
    symbol: str
    bid: float
    ask: float
    mid: float
    spread_pips: float
    timestamp: datetime


# --------------------------------------------------------------------------- #
# Broker connections
# --------------------------------------------------------------------------- #
class BrokerConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    broker: str = Field(default="metaapi", max_length=40)
    is_demo: bool = True
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    server: str = Field(min_length=1, max_length=120)
    currency: str = Field(default="USD", min_length=3, max_length=8)

    @field_validator("is_demo")
    @classmethod
    def demo_only_by_default(cls, v: bool) -> bool:
        return v


class BrokerConnectionOut(ORMModel):
    """Never includes credentials: only encrypted blobs are stored, never
    returned, and the login is masked so a screenshot cannot leak it."""
    id: str
    name: str
    broker: str
    is_demo: bool
    masked_login: str
    connection_state: str
    last_error: str | None = None
    last_synced_at: datetime | None = None
    balance: float | None = None
    equity: float | None = None
    currency: str
    created_at: datetime


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    balance: float | None = None
    equity: float | None = None
    currency: str | None = None
    latency_ms: float | None = None


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: bool
    redis: bool
    trading_mode: str
    live_trading_enabled: bool
    time: datetime


class ErrorResponse(BaseModel):
    detail: str


class PerformanceMetrics(BaseModel):
    """Honest performance report over a trailing window.

    Optional metrics are ``None`` when they cannot be computed from the data
    available (e.g. profit factor with no losing trades) rather than being
    defaulted to a flattering value.
    """

    period_days: int
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    # Average result per trade: the number that decides whether a strategy is
    # worth running, independent of how good the win rate looks.
    expectancy: float = 0.0
    payoff_ratio: float | None = None
    profit_factor: float | None = None
    longest_win_streak: int = 0
    longest_loss_streak: int = 0
    max_drawdown: float = 0.0
    max_drawdown_percent: float = 0.0
    # PnL-based, not return-on-equity; see app/services/analytics.py.
    sharpe_pnl_ratio: float | None = None
    trading_days: int = 0
    profitable_days: int = 0
    best_day: float = 0.0
    worst_day: float = 0.0


class EquityPointDaily(BaseModel):
    date: str | None = None
    daily_pnl: float = 0.0
    cumulative_pnl: float = 0.0


class SymbolPerformance(BaseModel):
    symbol: str
    trades: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    expectancy: float = 0.0
    profit_factor: float | None = None


class StrategyPerformance(BaseModel):
    strategy: str
    trades: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    expectancy: float = 0.0
    profit_factor: float | None = None


class AnalyticsReport(BaseModel):
    metrics: PerformanceMetrics
    equity: list[EquityPointDaily] = Field(default_factory=list)
    by_symbol: list[SymbolPerformance] = Field(default_factory=list)
    by_strategy: list[StrategyPerformance] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Backtesting
# --------------------------------------------------------------------------- #
class BacktestCreate(BaseModel):
    strategy: str
    symbol: str
    timeframe: str = "M1"
    date_from: date
    date_to: date
    initial_capital: float = Field(default=10_000.0, gt=0, le=100_000_000)
    risk_percent: float = Field(default=1.0, gt=0, le=10)
    spread_pips: float = Field(default=0.0, ge=0, le=500)
    slippage_pips: float = Field(default=0.0, ge=0, le=500)
    commission_per_lot: float = Field(default=0.0, ge=0, le=1000)


class BacktestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    strategy: str
    symbol: str
    timeframe: str
    date_from: date
    date_to: date
    initial_capital: float
    risk_percent: float
    # Cost assumptions travel with the result: the same strategy can flip from
    # profitable to losing purely by changing these, so a reader must see them.
    spread_pips: float
    slippage_pips: float
    commission_per_lot: float
    status: str
    progress: float
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BacktestTrade(BaseModel):
    """Mirrors what the engine's TradeRecord.as_dict() produces.

    The field is ``close_reason`` because that is what the engine writes;
    naming it ``reason`` here silently dropped the exit reason to null.
    """

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    symbol: str | None = None
    entry_time: str | None = None
    exit_time: str | None = None
    side: str | None = None
    volume: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    pnl: float | None = None
    commission: float | None = None
    swap: float | None = None
    close_reason: str | None = None
    r_multiple: float | None = None
    bars_held: int | None = None


class BacktestResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    final_equity: float
    net_pnl: float
    roi_percent: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float | None = None
    max_drawdown_percent: float
    max_drawdown_amount: float
    average_trade: float
    best_trade: float
    worst_trade: float
    avg_win: float
    avg_loss: float
    total_commission: float
    equity_curve: list[dict[str, Any]] = Field(default_factory=list)
    drawdown_curve: list[dict[str, Any]] = Field(default_factory=list)
    monthly_returns: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[BacktestTrade] = Field(default_factory=list)


class BacktestDetail(BacktestOut):
    result: BacktestResultOut | None = None


class StrategyOption(BaseModel):
    value: str
    label: str


class BacktestDefaults(BaseModel):
    initial_capital: float
    risk_percent: float
    spread_pips: float
    slippage_pips: float
    commission_per_lot: float


class BacktestOptionsOut(BaseModel):
    """Served from the engine's strategy registry so the client never invents
    an option the server will reject."""

    strategies: list[StrategyOption]
    timeframes: list[str]
    symbols: list[str]
    defaults: BacktestDefaults
