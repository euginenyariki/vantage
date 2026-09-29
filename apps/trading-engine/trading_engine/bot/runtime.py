"""Bot lifecycle: an explicit state machine plus the runtime loop.

BotState is never a boolean. Every transition is validated against
BOT_TRANSITIONS, every transition is persisted and published, and illegal
transitions raise rather than silently doing nothing.

The runtime loop per bot:
    tick -> market data -> strategy -> risk -> sizing -> execute
         -> trailing on open positions -> publish events -> persist

PAPER/LIVE invariant: closing the browser does not stop the engine. The engine
process owns the loop; the UI only sends commands.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from trading_engine.brokers.base import BrokerInterface
from trading_engine.core.types import (
    BotState,
    Position,
    SignalAction,
    SymbolSpec,
    Tick,
    TradingMode,
    can_transition,
    get_spec,
    utcnow,
)
from trading_engine.execution.engine import EmergencyStopError, ExecutionEngine
from trading_engine.execution.events import EngineEvent, EventBus
from trading_engine.market_data.aggregator import BarAggregator
from trading_engine.market_data.feed import MarketDataFeed
from trading_engine.risk.engine import RiskConfig, RiskContext
from trading_engine.strategies.base import Strategy, StrategyContext, StrategyRegistry
from trading_engine.trailing.service import TrailingConfig, TrailingService

logger = logging.getLogger("vantage.bot")


class IllegalTransition(Exception):
    pass


@dataclass
class BotConfig:
    bot_id: str
    user_id: str
    name: str
    symbol: str
    strategy_name: str = "hft_scalper"
    mode: TradingMode = TradingMode.PAPER
    broker_name: str = "paper"
    timeframe: str = "M1"
    max_concurrent_positions: int = 1
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    live_granted: bool = False
    auto_start: bool = True

    @property
    def strategy_class(self) -> type[Strategy]:
        cls_ = StrategyRegistry.get(self.strategy_name)
        if cls_ is None:
            raise IllegalTransition(f"Unknown strategy {self.strategy_name!r}")
        return cls_

    def new_strategy(self) -> Strategy:
        return self.strategy_class()


@dataclass
class BotStats:
    signals: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    risk_rejections: int = 0
    trailing_moves: int = 0
    break_even_moves: int = 0
    closed_trades: int = 0
    realized_pnl: float = 0.0
    started_at: datetime | None = None
    last_signal_at: datetime | None = None
    last_error: str | None = None
    daily_start_equity: float | None = None
    daily_date: date | None = None

    def as_dict(self) -> dict:
        return {
            "signals": self.signals,
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_filled,
            "orders_rejected": self.orders_rejected,
            "risk_rejections": self.risk_rejections,
            "trailing_moves": self.trailing_moves,
            "break_even_moves": self.break_even_moves,
            "closed_trades": self.closed_trades,
            "realized_pnl": round(self.realized_pnl, 2),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_signal_at": self.last_signal_at.isoformat() if self.last_signal_at else None,
            "last_error": self.last_error,
        }


class BotStateMachine:
    """Owns and validates the state; emits a callback on every change."""

    def __init__(self, initial: BotState = BotState.OFFLINE, on_change: Callable | None = None) -> None:
        self._state = initial
        self._on_change = on_change
        self._history: list[tuple[BotState, BotState, datetime]] = []
        self._pending: set[asyncio.Task] = set()

    @property
    def state(self) -> BotState:
        return self._state

    @property
    def history(self) -> list[tuple[BotState, BotState, datetime]]:
        return list(self._history)

    def can(self, target: BotState) -> bool:
        return can_transition(self._state, target)

    def to(self, target: BotState, reason: str = "") -> BotState:
        if not self.can(target):
            raise IllegalTransition(
                f"Cannot move bot from {self._state.value} to {target.value}"
                + (f" ({reason})" if reason else "")
            )
        previous, self._state = self._state, target
        self._history.append((previous, target, utcnow()))
        if self._on_change:
            outcome = self._on_change(previous, target, reason)
            if asyncio.iscoroutine(outcome):
                task = asyncio.ensure_future(outcome)
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
        return target


class TradingBot:
    """A single configured strategy instance with its own risk profile."""

    def __init__(
        self,
        config: BotConfig,
        *,
        broker: BrokerInterface,
        feed: MarketDataFeed,
        execution: ExecutionEngine,
        event_bus: EventBus | None = None,
        persist: Callable | None = None,
    ) -> None:
        self.config = config
        self.broker = broker
        self.feed = feed
        self.execution = execution
        self.events = event_bus
        self.persist = persist
        self.strategy = config.new_strategy()
        self.trailing = TrailingService(config.trailing)
        self.stats = BotStats()
        self.machine = BotStateMachine(on_change=self._on_state_change)
        self._task: asyncio.Task | None = None
        self._risk_ctx_provider: Callable[[], Awaitable[RiskContext]] | None = None
        self._bars_cache: list = []
        self._last_signal_bucket: int = 0
        self._bar_count: int = 0
        self._aggregator = BarAggregator(
            config.symbol, config.timeframe, get_spec(config.symbol)
        )
        # Wall clock by default; replay harnesses pin this to the data's clock.
        self.clock: Callable[[], datetime] = utcnow

    # ------------------------------------------------------------------ #
    def bind_risk_context(self, provider: Callable[[], Awaitable[RiskContext]]) -> None:
        self._risk_ctx_provider = provider

    @property
    def state(self) -> BotState:
        return self.machine.state

    # ------------------------------------------------------------------ #
    # Controls
    # ------------------------------------------------------------------ #
    async def start(self) -> BotState:
        if self.machine.state is BotState.OFFLINE:
            self.machine.to(BotState.STARTING, "start requested")
        elif self.machine.state in (BotState.PAUSED, BotState.STOPPED):
            self.machine.to(BotState.STARTING, "restart requested")
        elif self.machine.state is BotState.RUNNING:
            return BotState.RUNNING

        await self._publish_state("start requested")
        try:
            if self.broker.connection_state.value != "CONNECTED":
                await self.broker.connect()
            self.stats.started_at = self.stats.started_at or utcnow()
            self._bars_cache = await self.broker.get_bars(
                self.config.symbol, self.config.timeframe, self.strategy.min_bars + 20
            )
            self.machine.to(BotState.RUNNING, "running")
            await self._publish_state("running")
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run(), name=f"bot-{self.config.bot_id}")
        except Exception as exc:
            logger.exception("Bot %s failed to start", self.config.name)
            self.stats.last_error = str(exc)
            if self.machine.can(BotState.ERROR):
                self.machine.to(BotState.ERROR, str(exc))
            await self._publish_state(f"start failed: {exc}")
            raise
        return self.state

    async def pause(self) -> BotState:
        if self.machine.state is not BotState.RUNNING:
            return self.state
        self.machine.to(BotState.PAUSING, "pause requested")
        await self._publish_state("pausing")
        await self._cancel_loop()
        self.machine.to(BotState.PAUSED, "paused")
        await self._publish_state("paused")
        return self.state

    async def stop(self) -> BotState:
        if self.machine.state in (BotState.STOPPED, BotState.OFFLINE):
            return self.state
        self.machine.to(BotState.STOPPING, "stop requested")
        await self._publish_state("stopping")
        await self._cancel_loop()
        self.machine.to(BotState.STOPPED, "stopped")
        await self._publish_state("stopped")
        return self.state

    async def emergency_stop(self, reason: str = "manual") -> BotState:
        """Hard stop: cancel the loop AND block new orders at the engine level."""
        self.execution.trigger_emergency_stop(reason)
        if self.machine.state is not BotState.EMERGENCY_STOP:
            if self.machine.can(BotState.EMERGENCY_STOP):
                self.machine.to(BotState.EMERGENCY_STOP, reason)
            else:
                self.machine.to(BotState.STOPPING, f"emergency: {reason}")
        await self._cancel_loop()
        await self._publish_state(f"EMERGENCY STOP: {reason}")
        return self.state

    async def _cancel_loop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    # ------------------------------------------------------------------ #
    # The loop
    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        symbol = self.config.symbol
        try:
            async for tick in self.feed.stream(symbol):
                if self.machine.state is not BotState.RUNNING:
                    break
                try:
                    await self._on_tick(tick)
                except EmergencyStopError:
                    logger.warning("Bot %s halted by emergency stop", self.config.name)
                    break
                except Exception as exc:
                    logger.exception("Tick handling failed for %s", self.config.name)
                    self.stats.last_error = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Bot %s loop crashed", self.config.name)
            self.stats.last_error = str(exc)
            if self.machine.can(BotState.ERROR):
                self.machine.to(BotState.ERROR, str(exc))
                await self._publish_state(f"loop error: {exc}")

    async def _on_tick(self, tick: Tick) -> None:
        spec = get_spec(self.config.symbol)

        if self.broker.connection_state.value == "CONNECTED" and hasattr(self.broker, "on_tick"):
            closed = await self.broker.on_tick(tick)
            for trade in closed:
                self.stats.closed_trades += 1
                self.stats.realized_pnl += trade.pnl
                if self.events:
                    await self.events.publish(
                        EngineEvent.trade_closed(
                            bot_id=self.config.bot_id, user_id=self.config.user_id,
                            trade_id=trade.id, symbol=trade.symbol, side=trade.side.value,
                            volume=trade.volume, pnl=round(trade.pnl, 2),
                            closed_at=trade.closed_at.isoformat(), reason=trade.reason,
                            mode=trade.mode.value,
                        )
                    )

        # Fold the tick into its timeframe bucket. A bar is only appended when
        # it closes, so the strategy never evaluates a partial candle.
        closed_bar = self._aggregator.feed(tick.bid, tick.ask, tick.timestamp)
        if closed_bar is not None:
            self._bars_cache.append(closed_bar)
            self._bar_count += 1
            if len(self._bars_cache) > self.strategy.min_bars + 20:
                self._bars_cache = self._bars_cache[-self.strategy.min_bars - 20:]

        await self._manage_trailing(tick, spec)

        positions = await self.broker.get_positions()
        mine = [p for p in positions if p.symbol == self.config.symbol]
        if len(mine) >= self.config.max_concurrent_positions:
            return

        ctx = await self._risk_context(tick, mine, spec)
        strategy_ctx = StrategyContext(
            symbol=self.config.symbol, spec=spec, bars=self._bars_cache,
            tick=tick, equity=ctx.equity, open_positions=len(mine),
        )
        signal = self.strategy.evaluate(strategy_ctx)

        if signal.action is SignalAction.HOLD:
            return

        self.stats.signals += 1
        self.stats.last_signal_at = utcnow()
        if self.events:
            await self.events.publish(
                EngineEvent.order_state(
                    bot_id=self.config.bot_id, user_id=self.config.user_id,
                    symbol=signal.symbol, state="SIGNAL", action=signal.action.value,
                    confidence=signal.confidence, reason=signal.reason,
                    message=f"Confidence {signal.confidence:.0%}",
                )
            )

        result = await self.execution.execute_signal(
            signal=signal, tick=tick, ctx=ctx,
            bot_id=self.config.bot_id, trailing=self.config.trailing,
        )
        self.stats.orders_submitted += 1
        if result.state.value in ("RISK_REJECTED",):
            self.stats.risk_rejections += 1
        elif result.state.value == "REJECTED":
            self.stats.orders_rejected += 1
        elif result.filled:
            self.stats.orders_filled += 1

    async def _manage_trailing(self, tick: Tick, spec: SymbolSpec) -> None:
        positions = await self.broker.get_positions()
        for pos in positions:
            if pos.symbol != self.config.symbol or pos.stop_loss is None and pos.status.value != "OPEN":
                continue
            result = self.trailing.update(pos, tick, spec)
            if result.changed:
                ok = await self.broker.modify_position(pos.id, stop_loss=result.new_stop)
                if ok:
                    pos.stop_loss = result.new_stop
                    self.stats.trailing_moves += 1
                    if result.break_even_done and not pos.break_even_done:
                        pos.break_even_done = True
                        self.stats.break_even_moves += 1
                    if self.events:
                        await self.events.publish(
                            EngineEvent.position(
                                bot_id=self.config.bot_id, user_id=self.config.user_id,
                                action="TRAILING_MOVED", position_id=pos.id,
                                symbol=pos.symbol, old_stop=result.previous_stop,
                                new_stop=result.new_stop, reason=result.reason,
                            )
                        )

    async def _risk_context(
        self, tick: Tick, positions: list[Position], spec: SymbolSpec
    ) -> RiskContext:
        if self._risk_ctx_provider is not None:
            return await self._risk_ctx_provider()

        account = await self.broker.get_account()
        # Production uses wall-clock time. Replay/backtest harnesses override
        # `self.clock` so the stale-data guard evaluates against the timestamp
        # of the data being replayed rather than wall time.
        now = self.clock()
        today = now.date()
        if self.stats.daily_date != today or self.stats.daily_start_equity is None:
            self.stats.daily_date = today
            self.stats.daily_start_equity = account.balance

        unrealized = sum(p.unrealized_pnl for p in positions)
        equity = account.balance + unrealized
        daily_pnl = self.stats.realized_pnl

        ctx = RiskContext(
            equity=equity,
            balance=account.balance,
            daily_pnl=daily_pnl,
            open_positions=positions,
            now=now,
            broker_connected=self.broker.connection_state.value == "CONNECTED",
            leverage=account.leverage,
            live_enabled=self.config.live_granted,
        )

        if self.events:
            utilisation = self.execution.risk.risk_utilisation(ctx)
            await self.events.publish(
                EngineEvent.risk(bot_id=self.config.bot_id, user_id=self.config.user_id, **utilisation)
            )

        # Daily loss auto-halt (Phase 6)
        daily_loss_pct = (
            ((self.stats.daily_start_equity or account.balance) - equity)
            / max(self.stats.daily_start_equity or account.balance, 1e-9)
            * 100.0
        )
        if daily_loss_pct >= self.config.risk.max_daily_loss_percent:
            logger.error(
                "Bot %s halted: daily loss %.2f%% reached limit",
                self.config.name, daily_loss_pct,
            )
            await self.emergency_stop(f"Daily loss limit hit ({daily_loss_pct:.2f}%)")
        return ctx

    # ------------------------------------------------------------------ #
    async def _on_state_change(self, prev: BotState, new: BotState, reason: str) -> None:
        logger.info("Bot %s: %s -> %s (%s)", self.config.name, prev.value, new.value, reason)
        if self.persist:
            result = self.persist(self.config.bot_id, new.value, reason)
            if asyncio.iscoroutine(result):
                await result

    async def _publish_state(self, reason: str) -> None:
        if self.events:
            await self.events.publish(
                EngineEvent.bot_state(
                    bot_id=self.config.bot_id, state=self.machine.state.value, reason=reason
                )
            )
