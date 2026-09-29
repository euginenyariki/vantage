"""Domain primitives shared by the engine, the backtester and the API.

Everything here is deliberately dependency-free and side-effect free so the
exact same financial math runs in paper mode, in backtests and (eventually) in
live mode. Rule 16: all money math is unit tested in tests/test_financial_math.py
"""

from __future__ import annotations

import enum
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

Number = float


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class TradingMode(str, enum.Enum):
    DEMO = "DEMO"
    PAPER = "PAPER"
    LIVE = "LIVE"

    @property
    def is_real(self) -> bool:
        return self is TradingMode.LIVE


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }

    @property
    def is_open(self) -> bool:
        return not self.is_terminal


class PositionStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class BotState(str, enum.Enum):
    OFFLINE = "OFFLINE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    EMERGENCY_STOP = "EMERGENCY_STOP"

    @property
    def is_tradable(self) -> bool:
        return self is BotState.RUNNING


#: Legal bot state transitions. Anything absent here is a bug, not a shortcut.
BOT_TRANSITIONS: dict[BotState, set[BotState]] = {
    BotState.OFFLINE: {BotState.STARTING},
    BotState.STARTING: {BotState.RUNNING, BotState.ERROR, BotState.STOPPING},
    BotState.RUNNING: {BotState.PAUSING, BotState.STOPPING, BotState.ERROR, BotState.EMERGENCY_STOP},
    BotState.PAUSING: {BotState.PAUSED, BotState.ERROR, BotState.STOPPING},
    BotState.PAUSED: {BotState.STARTING, BotState.STOPPING, BotState.EMERGENCY_STOP},
    BotState.STOPPING: {BotState.STOPPED, BotState.ERROR},
    BotState.STOPPED: {BotState.STARTING, BotState.OFFLINE},
    BotState.ERROR: {BotState.STARTING, BotState.STOPPING, BotState.OFFLINE},
    BotState.EMERGENCY_STOP: {BotState.STARTING, BotState.STOPPING, BotState.OFFLINE},
}


def can_transition(src: BotState, dst: BotState) -> bool:
    if src is dst:
        return True
    return dst in BOT_TRANSITIONS.get(src, set())


class TrailingMode(str, enum.Enum):
    FIXED = "FIXED"
    STEP = "STEP"
    BREAK_EVEN = "BREAK_EVEN"
    ATR = "ATR"
    PERCENTAGE = "PERCENTAGE"


class SignalAction(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    CLOSE = "CLOSE"


class DiscrepancyType(str, enum.Enum):
    POSITION_MISSING_LOCALLY = "POSITION_MISSING_LOCALLY"
    POSITION_MISSING_BROKER = "POSITION_MISSING_BROKER"
    ORDER_STATUS_MISMATCH = "ORDER_STATUS_MISMATCH"
    BALANCE_MISMATCH = "BALANCE_MISMATCH"


class RiskLevel(str, enum.Enum):
    OK = "OK"
    WARNING = "WARNING"
    BREACH = "BREACH"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def round_price(value: float, digits: int = 5) -> float:
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def round_volume(value: float, step: float = 0.01) -> float:
    """Round volume DOWN to the broker's lot step. Never round up: that would
    silently increase risk beyond what the risk engine approved."""
    if step <= 0:
        return 0.0
    steps = math.floor((value + 1e-9) / step)
    return float(Decimal(steps * step).quantize(Decimal(1).scaleb(-2)))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Tick:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.ask < self.bid:
            raise ValueError(f"crossed quote for {self.symbol}: bid={self.bid} ask={self.ask}")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pips(self) -> float:
        return self.spread / PIP_SIZE

    def age_seconds(self, now: datetime | None = None) -> float:
        ref = now or utcnow()
        return (ref - self.timestamp).total_seconds()

    def is_stale(self, max_age_seconds: float, now: datetime | None = None) -> bool:
        return self.age_seconds(now) > max_age_seconds

    def is_future_dated(self, tolerance_seconds: float = 5.0,
                        now: datetime | None = None) -> bool:
        """True when the tick is stamped ahead of our clock.

        A small forward skew is normal feed latency and is tolerated. A large one
        means a broker/VM clock mismatch, which would corrupt every age-based
        freshness check, so it is treated as untrustworthy data.
        """
        return self.age_seconds(now) < -abs(tolerance_seconds)

    def price_for(self, side: Side) -> float:
        """Price you actually transact at. Buy lifts the ask, sell hits the bid."""
        return self.ask if side is Side.BUY else self.bid


PIP_SIZE = 0.0001
POINTS_PER_PIP = 10


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Everything the engine needs to size an order correctly for one symbol."""

    symbol: str
    digits: int
    pip_size: float
    contract_size: float
    tick_size: float
    tick_value: float          # account currency per tick per 1.0 lot
    min_volume: float
    max_volume: float
    volume_step: float
    commission_per_lot: float = 0.0
    swap_long: float = 0.0     # per lot per night
    swap_short: float = 0.0
    min_stop_distance: float = 0.0   # in price units
    max_spread_pips: float = 50.0
    # Market family this instrument belongs to, surfaced in the UI so the
    # classes a user can actually trade are never overstated.
    asset_class: str = "forex"

    def pips_to_price(self, pips: float) -> float:
        return pips * self.pip_size

    def price_to_pips(self, price_delta: float) -> float:
        return price_delta / self.pip_size

    def normalize_volume(self, volume: float) -> float:
        v = clamp(volume, self.min_volume, self.max_volume)
        return round_volume(v, self.volume_step)

    def value_per_price_unit(self, volume: float) -> float:
        """Account-currency P/L for a 1.0 price-unit move, at `volume` lots.

        Derived from ticks: a price-unit move spans `1 / tick_size` ticks, and
        each tick is worth `tick_value` per lot. `contract_size` is deliberately
        NOT multiplied in here — for a standard FX contract it is already
        expressed through tick_value (e.g. EURUSD tick_size=1e-5,
        tick_value=1.0 -> 100,000 ticks x $1 = $100,000 per 1.0 price unit).
        """
        return (volume / self.tick_size) * self.tick_value


DEFAULT_SPECS: dict[str, SymbolSpec] = {
    "EURUSD": SymbolSpec(
        symbol="EURUSD", digits=5, pip_size=0.0001, contract_size=100_000.0,
        tick_size=0.00001, tick_value=1.0, min_volume=0.01, max_volume=100.0,
        volume_step=0.01, commission_per_lot=3.5,
        min_stop_distance=0.00010, max_spread_pips=3.0,
    ),
    "GBPUSD": SymbolSpec(
        symbol="GBPUSD", digits=5, pip_size=0.0001, contract_size=100_000.0,
        tick_size=0.00001, tick_value=1.0, min_volume=0.01, max_volume=100.0,
        volume_step=0.01, commission_per_lot=3.5,
        min_stop_distance=0.00010, max_spread_pips=4.0,
    ),
    "USDJPY": SymbolSpec(
        symbol="USDJPY", digits=3, pip_size=0.01, contract_size=100_000.0,
        tick_size=0.001, tick_value=6.5, min_volume=0.01, max_volume=100.0,
        volume_step=0.01, commission_per_lot=3.0,
        min_stop_distance=0.010, max_spread_pips=4.0,
    ),
    "XAUUSD": SymbolSpec(
        symbol="XAUUSD", digits=2, pip_size=0.01, contract_size=100.0,
        tick_size=0.01, tick_value=1.0, min_volume=0.01, max_volume=50.0,
        volume_step=0.01, commission_per_lot=6.0,
        min_stop_distance=0.10, max_spread_pips=8.0, asset_class="metal",
    ),
    # --- Crypto CFDs --------------------------------------------------------
    # Sized as 1 lot = 1 unit of the base coin, so a $1 move in price is $1 of
    # P/L per lot, and tick_value = contract_size * tick_size holds as it does
    # for FX above. Commission is left at 0 because most crypto CFDs are
    # spread-based rather than commission-based; the live MetaApi adapter
    # replaces these defaults with the broker's own tickValue/commission when a
    # real account is connected, so these values only drive paper trading.
    "BTCUSD": SymbolSpec(
        symbol="BTCUSD", digits=2, pip_size=0.01, contract_size=1.0,
        tick_size=0.01, tick_value=0.01, min_volume=0.01, max_volume=5.0,
        volume_step=0.01, commission_per_lot=0.0, asset_class="crypto",
        min_stop_distance=1.00, max_spread_pips=50.0,
    ),
    "ETHUSD": SymbolSpec(
        symbol="ETHUSD", digits=2, pip_size=0.01, contract_size=1.0,
        tick_size=0.01, tick_value=0.01, min_volume=0.01, max_volume=25.0,
        volume_step=0.01, commission_per_lot=0.0, asset_class="crypto",
        min_stop_distance=0.50, max_spread_pips=40.0,
    ),
    "SOLUSD": SymbolSpec(
        symbol="SOLUSD", digits=3, pip_size=0.001, contract_size=1.0,
        tick_size=0.001, tick_value=0.001, min_volume=0.1, max_volume=250.0,
        volume_step=0.1, commission_per_lot=0.0, asset_class="crypto",
        min_stop_distance=0.05, max_spread_pips=30.0,
    ),
    "XRPUSD": SymbolSpec(
        symbol="XRPUSD", digits=5, pip_size=0.00001, contract_size=1.0,
        tick_size=0.00001, tick_value=0.00001, min_volume=1.0, max_volume=1000.0,
        volume_step=1.0, commission_per_lot=0.0, asset_class="crypto",
        min_stop_distance=0.001, max_spread_pips=20.0,
    ),
}


def get_spec(symbol: str) -> SymbolSpec:
    return DEFAULT_SPECS.get(symbol.upper(), DEFAULT_SPECS["EURUSD"])


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class StrategySignal:
    action: SignalAction
    symbol: str
    price: float | None = None
    confidence: float = 0.0            # 0..1
    reason: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None
    atr: float | None = None
    meta: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within 0..1")


# --------------------------------------------------------------------------- #
# Orders, positions, trades
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    volume: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_mode: TrailingMode = TrailingMode.FIXED
    trailing_distance_pips: float = 10.0
    trailing_step_pips: float = 0.0
    break_even_trigger_pips: float = 0.0
    break_even_offset_pips: float = 0.0
    atr_period: int = 14
    meta: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class Order:
    id: str
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: Side
    volume: float
    filled_volume: float
    order_type: OrderType
    status: OrderStatus
    requested_price: float | None
    filled_price: float | None
    stop_loss: float | None
    take_profit: float | None
    commission: float = 0.0
    reject_reason: str | None = None
    mode: TradingMode = TradingMode.PAPER
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def remaining_volume(self) -> float:
        return round_volume(self.volume - self.filled_volume)


@dataclass(slots=True)
class Position:
    id: str
    symbol: str
    side: Side
    volume: float
    open_price: float
    current_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    status: PositionStatus = PositionStatus.OPEN
    commission: float = 0.0
    swap: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    mode: TradingMode = TradingMode.PAPER
    trailing_mode: TrailingMode = TrailingMode.FIXED
    trailing_distance_pips: float = 0.0
    trailing_step_pips: float = 0.0
    break_even_trigger_pips: float = 0.0
    break_even_offset_pips: float = 0.0
    break_even_done: bool = False
    initial_stop_distance_pips: float = 0.0
    opened_at: datetime = field(default_factory=utcnow)
    closed_at: datetime | None = None
    bot_id: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class Trade:
    id: str
    position_id: str
    symbol: str
    side: Side
    volume: float
    open_price: float
    close_price: float
    commission: float = 0.0
    swap: float = 0.0
    pnl: float = 0.0
    mode: TradingMode = TradingMode.PAPER
    bot_id: str | None = None
    opened_at: datetime = field(default_factory=utcnow)
    closed_at: datetime = field(default_factory=utcnow)
    reason: str = ""


@dataclass(slots=True)
class AccountSnapshot:
    balance: float
    equity: float
    margin: float = 0.0
    free_margin: float = 0.0
    currency: str = "USD"
    leverage: int = 100
    updated_at: datetime = field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Financial math — the single source of truth
# --------------------------------------------------------------------------- #
def position_pnl(
    side: Side,
    volume: float,
    open_price: float,
    close_price: float,
    spec: SymbolSpec,
    commission: float = 0.0,
    swap: float = 0.0,
) -> float:
    """Cash P/L of closing a position at `close_price`, commissions included.

    Value is derived from tick_value/tick_size so it is correct for any
    contract specification, not just FX pairs.
    """
    if volume <= 0:
        return 0.0
    price_delta = (close_price - open_price) * side.sign
    per_lot = (price_delta / spec.tick_size) * spec.tick_value
    gross = per_lot * volume
    return gross - commission + swap


def unrealized_pnl(
    position: Position,
    current_price: float,
    spec: SymbolSpec,
) -> float:
    return position_pnl(
        position.side, position.volume, position.open_price, current_price, spec,
        commission=position.commission, swap=position.swap,
    )


def size_position(
    equity: float,
    risk_percent: float,
    stop_distance_price: float,
    spec: SymbolSpec,
    *,
    max_volume: float | None = None,
    min_volume: float | None = None,
) -> float:
    """Risk-based lot sizing.

    Lots = (equity * risk%) / (stop_distance * value_per_price_unit_per_lot).
    The result is clamped to broker limits and floored to the volume step so we
    never exceed the risk budget we were approved for.
    """
    if equity <= 0:
        return 0.0
    if risk_percent <= 0:
        return 0.0
    if stop_distance_price <= 0:
        return 0.0

    risk_amount = equity * (risk_percent / 100.0)
    value_per_price_unit_per_lot = spec.value_per_price_unit(1.0)
    if value_per_price_unit_per_lot <= 0:
        return 0.0

    raw_lots = risk_amount / (stop_distance_price * value_per_price_unit_per_lot)
    lots = spec.normalize_volume(raw_lots)

    cap = max_volume if max_volume is not None else spec.max_volume
    lots = min(lots, cap)

    floor = min_volume if min_volume is not None else spec.min_volume
    if lots < floor:
        # Too small to risk the configured amount on this instrument. Returning
        # 0 (skip the trade) is safer than silently oversizing to the minimum.
        return 0.0
    return lots


def position_margin(
    volume: float, price: float, spec: SymbolSpec, leverage: int = 100
) -> float:
    if leverage <= 0:
        return 0.0
    return (spec.contract_size * volume * price) / leverage


def account_equity(balance: float, open_positions: list[Position], prices: dict[str, float]) -> float:
    """Equity = balance + sum of unrealized P/L, valued at the latest known price."""
    equity = balance
    for pos in open_positions:
        price = prices.get(pos.symbol, pos.current_price or pos.open_price)
        equity += unrealized_pnl(pos, price, get_spec(pos.symbol))
    return equity


def profit_factor(gross_profit: float, gross_loss: float) -> float:
    """Gross profit / gross loss. Returns inf when there is no loss, 0 when
    there is no profit — callers must label this in the UI."""
    if gross_loss <= 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / abs(gross_loss)


def max_drawdown(equity_curve: list[float]) -> tuple[float, float]:
    """(max drawdown fraction, max drawdown in currency). Fraction is positive."""
    if not equity_curve:
        return 0.0, 0.0
    peak = equity_curve[0]
    worst_frac = 0.0
    worst_abs = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        dd = peak - value
        if peak > 0:
            worst_frac = max(worst_frac, dd / peak)
        worst_abs = max(worst_abs, dd)
    return worst_frac, worst_abs
