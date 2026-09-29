"""RiskEngine — the only door between a strategy signal and an order.

Every check returns a RiskDecision carrying the exact reason for a rejection so
the UI can explain *why* a trade did not happen. A signal that is not approved
is never routed around (rule 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from trading_engine.core.types import (
    Position,
    RiskLevel,
    Side,
    SymbolSpec,
    Tick,
    TradingMode,
    get_spec,
    position_margin,
    unrealized_pnl,
)


class RiskRule(str, Enum):
    TRADING_MODE_VALID = "TRADING_MODE_VALID"
    TRADING_ENABLED = "TRADING_ENABLED"
    CONNECTION_HEALTHY = "CONNECTION_HEALTHY"
    KILL_SWITCH = "KILL_SWITCH"
    SYMBOL_SUPPORTED = "SYMBOL_SUPPORTED"
    SPREAD_OK = "SPREAD_OK"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    TRADING_HOURS = "TRADING_HOURS"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MAX_EXPOSURE = "MAX_EXPOSURE"
    MAX_POSITIONS = "MAX_POSITIONS"
    MAX_LOT_SIZE = "MAX_LOT_SIZE"
    MIN_LOT_SIZE = "MIN_LOT_SIZE"
    MARGIN_OK = "MARGIN_OK"
    SL_PRESENT = "SL_PRESENT"
    SL_MIN_DISTANCE = "SL_MIN_DISTANCE"
    DUP_SIGNAL = "DUP_SIGNAL"
    ORDER_NOTIONAL = "ORDER_NOTIONAL"


class RiskVerdict(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WARNING = "WARNING"


@dataclass
class RiskConfig:
    """Per-bot risk profile. Server-validated; the frontend copy is cosmetic."""

    trading_enabled: bool = True
    trading_mode: TradingMode = TradingMode.PAPER
    kill_switch: bool = False
    live_trading_enabled: bool = False

    max_risk_percent_per_trade: float = 1.0
    max_daily_loss_percent: float = 5.0
    max_total_exposure_percent: float = 200.0   # of equity, as margin
    max_open_positions: int = 5
    max_lots_per_symbol: float = 2.0
    max_lots_total: float = 10.0
    max_order_notional: float = 100_000.0
    max_spread_pips: float = 3.0
    max_slippage_pips: float = 2.0
    market_data_max_age_seconds: float = 15.0
    require_stop_loss: bool = True
    allowed_symbols: tuple[str, ...] = ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD")
    trading_window: tuple[int, int] | None = None   # UTC hours [start, end)
    allowed_days: tuple[int, ...] = (0, 1, 2, 3, 4)  # 0=Mon .. 6=Sun
    duplicate_signal_window_seconds: int = 30
    require_broker_connected: bool = True


@dataclass
class RiskDecision:
    verdict: RiskVerdict
    rule: RiskRule | None = None
    reason: str = ""
    details: dict = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.verdict is RiskVerdict.APPROVED

    @property
    def blocked(self) -> bool:
        return self.verdict is RiskVerdict.REJECTED


APPROVED = RiskDecision(RiskVerdict.APPROVED)


@dataclass
class RiskContext:
    """Everything the engine needs to judge a signal. Assembled by the caller
    from DB + broker state, never from the client."""

    equity: float
    balance: float
    daily_pnl: float
    open_positions: list[Position]
    now: datetime
    broker_connected: bool = True
    kill_switch: bool = False
    leverage: int = 100
    live_enabled: bool = False
    recent_signals: dict[tuple[str, str], datetime] = field(default_factory=dict)


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        *,
        symbol: str,
        side: Side,
        volume: float,
        tick: Tick | None,
        spec: SymbolSpec,
        signal_id: str | None = None,
        ctx: RiskContext,
    ) -> RiskDecision:
        cfg = self.config

        # 1. Kill switch and mode guards come first: nothing else matters.
        if cfg.kill_switch or ctx.kill_switch:
            return self._reject(RiskRule.KILL_SWITCH, "Kill switch is active")

        if cfg.trading_mode is TradingMode.LIVE and not (cfg.live_trading_enabled and ctx.live_enabled):
            return self._reject(
                RiskRule.TRADING_MODE_VALID,
                "LIVE mode requires LIVE_TRADING_ENABLED and a live-enable grant",
            )

        if not cfg.trading_enabled:
            return self._reject(RiskRule.TRADING_ENABLED, "Trading is disabled on this bot")

        # 2. Connectivity
        if cfg.require_broker_connected and not ctx.broker_connected:
            return self._reject(RiskRule.CONNECTION_HEALTHY, "Broker is not connected")

        # 3. Instrument allowlist
        if symbol.upper() not in {s.upper() for s in cfg.allowed_symbols}:
            return self._reject(
                RiskRule.SYMBOL_SUPPORTED, f"{symbol} is not in the allowed symbol list"
            )

        # 4. Market data freshness — refuse to trade on a stale quote (rule 11)
        if tick is None:
            return self._reject(RiskRule.STALE_MARKET_DATA, "No market data available")
        if tick.symbol.upper() != symbol.upper():
            return self._reject(
                RiskRule.STALE_MARKET_DATA, f"Tick for {tick.symbol} does not match {symbol}"
            )
        if tick.is_stale(cfg.market_data_max_age_seconds, ctx.now):
            age = tick.age_seconds(ctx.now)
            return self._reject(
                RiskRule.STALE_MARKET_DATA,
                f"Market data is {age:.1f}s old (limit {cfg.market_data_max_age_seconds}s)",
                {"age_seconds": round(age, 2)},
            )
        if tick.is_future_dated(5.0, ctx.now):
            age = tick.age_seconds(ctx.now)
            return self._reject(
                RiskRule.STALE_MARKET_DATA,
                f"Market data is stamped {-age:.1f}s in the future — check clock sync",
                {"age_seconds": round(age, 2)},
            )

        # 5. Spread
        spread_pips = tick.spread_pips
        limit = min(cfg.max_spread_pips, spec.max_spread_pips)
        if spread_pips > limit:
            return self._reject(
                RiskRule.SPREAD_OK,
                f"Spread {spread_pips:.1f} pips exceeds limit {limit:.1f}",
                {"spread_pips": round(spread_pips, 2), "limit": limit},
            )

        # 6. Trading window
        window_check = self._check_trading_window(ctx.now)
        if window_check is not None:
            return window_check

        # 7. Daily loss circuit breaker
        #
        # `daily_pnl` is the *realized* P/L booked since the day started, so the
        # day-start equity is `balance - daily_pnl`. The loss to measure is how
        # far equity has fallen below that mark:
        #
        #     loss = (balance - daily_pnl) - equity
        #
        # Getting this sign wrong silently disables the breaker on exactly the
        # losing day it is meant to catch.
        if ctx.equity > 0 and ctx.balance > 0:
            day_start_equity = ctx.balance - ctx.daily_pnl
            daily_loss = day_start_equity - ctx.equity
            loss_pct = (daily_loss / day_start_equity) * 100.0
            if loss_pct >= cfg.max_daily_loss_percent:
                return self._reject(
                    RiskRule.DAILY_LOSS_LIMIT,
                    f"Daily loss {loss_pct:.2f}% reached limit {cfg.max_daily_loss_percent:.2f}%",
                    {"daily_loss_pct": round(loss_pct, 3)},
                )

        # 8. Position count
        open_count = sum(
            1 for p in ctx.open_positions if p.status.value == "OPEN"
        )
        if open_count >= cfg.max_open_positions:
            return self._reject(
                RiskRule.MAX_POSITIONS,
                f"Already holding {open_count} positions (max {cfg.max_open_positions})",
            )

        # 9. Duplicate signal guard (idempotency at the strategy layer)
        if signal_id:
            last = ctx.recent_signals.get((symbol, side.value))
            if last is not None:
                age = (ctx.now - last).total_seconds()
                if age < cfg.duplicate_signal_window_seconds:
                    return self._reject(
                        RiskRule.DUP_SIGNAL,
                        f"Duplicate {side.value} signal for {symbol} {age:.1f}s ago",
                        {"window_seconds": cfg.duplicate_signal_window_seconds},
                    )

        # 10. Volume bounds
        if volume <= 0:
            return self._reject(RiskRule.MIN_LOT_SIZE, "Computed volume is zero")
        if volume < spec.min_volume - 1e-9:
            return self._reject(
                RiskRule.MIN_LOT_SIZE, f"Volume {volume} below broker minimum {spec.min_volume}"
            )
        if volume > spec.max_volume:
            return self._reject(
                RiskRule.MAX_LOT_SIZE, f"Volume {volume} above broker maximum {spec.max_volume}"
            )

        # 11. Aggregate lot caps
        symbol_lots = sum(p.volume for p in ctx.open_positions if p.symbol == symbol)
        if symbol_lots + volume > cfg.max_lots_per_symbol:
            return self._reject(
                RiskRule.MAX_LOT_SIZE,
                f"{symbol} exposure would be {symbol_lots + volume:.2f} lots "
                f"(max {cfg.max_lots_per_symbol:.2f})",
            )
        total_lots = sum(p.volume for p in ctx.open_positions)
        if total_lots + volume > cfg.max_lots_total:
            return self._reject(
                RiskRule.MAX_LOT_SIZE,
                f"Total exposure would be {total_lots + volume:.2f} lots "
                f"(max {cfg.max_lots_total:.2f})",
            )

        # 12. Notional cap
        notional = volume * spec.contract_size * tick.price_for(side)
        if notional > cfg.max_order_notional:
            return self._reject(
                RiskRule.ORDER_NOTIONAL,
                f"Order notional {notional:,.0f} exceeds cap {cfg.max_order_notional:,.0f}",
            )

        # 13. Margin availability — exposure limit + free margin
        used_margin = sum(
            position_margin(p.volume, p.current_price or p.open_price, get_spec(p.symbol), ctx.leverage)
            for p in ctx.open_positions
        )
        new_margin = position_margin(volume, tick.price_for(side), spec, ctx.leverage)
        if ctx.equity > 0:
            exposure_pct = ((used_margin + new_margin) / ctx.equity) * 100.0
            if exposure_pct > cfg.max_total_exposure_percent:
                return self._reject(
                    RiskRule.MAX_EXPOSURE,
                    f"Total exposure {exposure_pct:.1f}% exceeds limit "
                    f"{cfg.max_total_exposure_percent:.1f}%",
                    {"exposure_pct": round(exposure_pct, 2)},
                )
        free_margin = ctx.equity - used_margin
        if new_margin > free_margin:
            return self._reject(
                RiskRule.MARGIN_OK,
                f"Insufficient free margin: need {new_margin:,.2f}, have {free_margin:,.2f}",
            )

        return APPROVED

    # ------------------------------------------------------------------ #
    def evaluate_stop_loss(
        self,
        *,
        symbol: str,
        side: Side,
        stop_loss: float | None,
        entry_price: float,
        tick: Tick,
        spec: SymbolSpec,
    ) -> RiskDecision:
        """The SL must exist, sit on the correct side of price, and clear the
        broker's minimum stop distance."""
        if self.config.require_stop_loss and stop_loss is None:
            return self._reject(RiskRule.SL_PRESENT, "A stop loss is required")

        if stop_loss is None:
            return APPROVED

        # Protective direction: SL must be below entry for a long, above for a short.
        if side is Side.BUY and stop_loss >= entry_price:
            return self._reject(
                RiskRule.SL_MIN_DISTANCE,
                f"Long stop loss {stop_loss} must be below entry {entry_price}",
            )
        if side is Side.SELL and stop_loss <= entry_price:
            return self._reject(
                RiskRule.SL_MIN_DISTANCE,
                f"Short stop loss {stop_loss} must be above entry {entry_price}",
            )

        min_distance = max(spec.min_stop_distance, self.config.max_slippage_pips * spec.pip_size)
        distance = abs(entry_price - stop_loss)
        if distance < min_distance - 1e-12:
            return self._reject(
                RiskRule.SL_MIN_DISTANCE,
                f"Stop loss is {distance / spec.pip_size:.1f} pips from entry, "
                f"minimum is {min_distance / spec.pip_size:.1f}",
            )

        return APPROVED

    # ------------------------------------------------------------------ #
    def risk_utilisation(self, ctx: RiskContext) -> dict:
        """Feeds the RiskMeter UI component. Reported, never enforced here."""
        cfg = self.config
        used_margin = sum(
            position_margin(p.volume, p.current_price or p.open_price, get_spec(p.symbol), ctx.leverage)
            for p in ctx.open_positions
        )
        unrealized = sum(
            unrealized_pnl(p, p.current_price or p.open_price, get_spec(p.symbol))
            for p in ctx.open_positions
        )
        equity = ctx.equity
        day_start_equity = ctx.balance - ctx.daily_pnl if ctx.balance > 0 else 0.0
        daily_loss = day_start_equity - ctx.equity if ctx.balance > 0 else 0.0
        daily_loss_pct = (
            (daily_loss / day_start_equity * 100.0) if day_start_equity > 0 else 0.0
        )
        exposure_pct = (used_margin / equity * 100.0) if equity > 0 else 0.0

        def level(value: float, limit: float) -> RiskLevel:
            if limit <= 0:
                return RiskLevel.OK
            ratio = value / limit
            if ratio >= 1.0:
                return RiskLevel.BREACH
            if ratio >= 0.75:
                return RiskLevel.WARNING
            return RiskLevel.OK

        return {
            "equity": equity,
            "unrealized_pnl": unrealized,
            "open_positions": len(ctx.open_positions),
            "exposure_pct": round(exposure_pct, 2),
            "exposure_limit_pct": cfg.max_total_exposure_percent,
            "exposure_level": level(exposure_pct, cfg.max_total_exposure_percent),
            "daily_loss_pct": round(daily_loss_pct, 3),
            "daily_loss_limit_pct": cfg.max_daily_loss_percent,
            "daily_loss_level": level(daily_loss_pct, cfg.max_daily_loss_percent),
            "lots_used": round(sum(p.volume for p in ctx.open_positions), 2),
            "lots_limit": cfg.max_lots_total,
        }

    # ------------------------------------------------------------------ #
    def _check_trading_window(self, now: datetime) -> RiskDecision | None:
        cfg = self.config
        utc = now.astimezone(UTC)
        if cfg.allowed_days and utc.weekday() not in cfg.allowed_days:
            return self._reject(
                RiskRule.TRADING_HOURS,
                f"{utc.strftime('%A')} is not an allowed trading day",
            )
        if cfg.trading_window is not None:
            start, end = cfg.trading_window
            if not (start <= utc.hour < end):
                return self._reject(
                    RiskRule.TRADING_HOURS,
                    f"Outside trading window {start:02d}:00–{end:02d}:00 UTC",
                )
        return None

    @staticmethod
    def _reject(rule: RiskRule, reason: str, details: dict | None = None) -> RiskDecision:
        return RiskDecision(RiskVerdict.REJECTED, rule, reason, details or {})
