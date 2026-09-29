"""ExecutionEngine — turns an approved signal into a confirmed broker position.

The order lifecycle is an explicit state machine (rule 9-ish for orders):
    VALIDATED -> RISK_APPROVED -> SUBMITTING -> SUBMITTED -> CONFIRMED
                                                     -> AMBIGUOUS -> RECONCILING
                                                      -> REJECTED

Key behaviours:
  * Idempotency: `client_order_id` is deterministic per (bot, symbol, side,
    signal timestamp bucket). A retry after a crash re-uses the same key, so the
    broker is asked for the existing order rather than a new one (rule 7).
  * No blind retries: an ambiguous submit goes to RECONCILING, which queries
    the broker and only retries when the broker definitively has no such order
    (rule 6).
  * Emergency stop is checked at the top of every public method, so a stopped
    bot cannot open a new position through any code path (Phase 6 requirement).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from trading_engine.brokers.base import (
    BrokerError,
    BrokerInterface,
    BrokerRejectedError,
    BrokerTimeoutError,
)
from trading_engine.core.types import (
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    StrategySignal,
    Tick,
    TradingMode,
    get_spec,
    utcnow,
)
from trading_engine.execution.events import EngineEvent
from trading_engine.risk.engine import RiskContext, RiskDecision, RiskEngine
from trading_engine.risk.sizing import PositionSizer, SizingRequest
from trading_engine.trailing.service import TrailingConfig

logger = logging.getLogger("vantage.execution")


class OrderState(str, Enum):
    VALIDATED = "VALIDATED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"
    RECONCILING = "RECONCILING"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


ORDER_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.VALIDATED: {OrderState.RISK_APPROVED, OrderState.RISK_REJECTED, OrderState.REJECTED},
    OrderState.RISK_APPROVED: {OrderState.SUBMITTING, OrderState.FAILED},
    OrderState.SUBMITTING: {
        OrderState.SUBMITTED, OrderState.CONFIRMED, OrderState.AMBIGUOUS, OrderState.REJECTED,
    },
    OrderState.SUBMITTED: {OrderState.CONFIRMED, OrderState.AMBIGUOUS, OrderState.REJECTED},
    OrderState.AMBIGUOUS: {OrderState.RECONCILING, OrderState.CONFIRMED, OrderState.REJECTED},
    OrderState.RECONCILING: {OrderState.CONFIRMED, OrderState.SUBMITTED, OrderState.REJECTED},
    OrderState.CONFIRMED: set(),
    OrderState.RISK_REJECTED: set(),
    OrderState.REJECTED: set(),
    OrderState.FAILED: {OrderState.SUBMITTING},
}


def transition_order(src: OrderState, dst: OrderState) -> None:
    if dst is not src and dst not in ORDER_TRANSITIONS.get(src, set()):
        raise ValueError(f"Illegal order transition {src.value} -> {dst.value}")


class EmergencyStopError(Exception):
    pass


@dataclass
class ExecutionResult:
    state: OrderState
    order: Order | None = None
    position: Position | None = None
    decision: RiskDecision | None = None
    message: str = ""
    client_order_id: str = ""

    @property
    def filled(self) -> bool:
        return self.state is OrderState.CONFIRMED


class EventPublisher(Protocol):
    async def publish(self, event: EngineEvent) -> None: ...


def make_client_order_id(bot_id: str, symbol: str, side: Side, bucket: int) -> str:
    """Deterministic idempotency key. Same bot+signal bucket -> same key, even
    across a process restart, so a duplicate submission is impossible."""
    raw = f"{bot_id}|{symbol}|{side.value}|{bucket}"
    return f"v1-{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


class ExecutionEngine:
    def __init__(
        self,
        *,
        broker: BrokerInterface,
        risk_engine: RiskEngine,
        sizer: PositionSizer,
        event_bus: EventPublisher | None = None,
        mode: TradingMode = TradingMode.PAPER,
        live_allowed: bool = False,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.broker = broker
        self.risk = risk_engine
        self.sizer = sizer
        self.events = event_bus
        self.mode = mode
        self.live_allowed = live_allowed
        self.clock = clock
        self._emergency_stop = False
        self._kill_switch = False
        self._idempotency: dict[str, Order] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Safety switches
    # ------------------------------------------------------------------ #
    def trigger_emergency_stop(self, reason: str = "manual") -> None:
        self._emergency_stop = True
        logger.error("EMERGENCY STOP engaged: %s", reason)

    def clear_emergency_stop(self) -> None:
        self._emergency_stop = False
        logger.warning("Emergency stop cleared")

    def set_kill_switch(self, engaged: bool) -> None:
        """Engage or release the kill switch.

        The switch is mirrored onto the risk config so every downstream check
        sees the same state; previously it was written to a private flag that
        nothing read, which meant engaging it silently had no effect.
        """
        self._kill_switch = engaged
        self.risk.config.kill_switch = engaged
        logger.warning("Kill switch %s", "ENGAGED" if engaged else "released")

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch or self.risk.config.kill_switch

    @property
    def emergency_stopped(self) -> bool:
        return self._emergency_stop

    def _assert_not_stopped(self) -> None:
        if self._emergency_stop:
            raise EmergencyStopError(
                "Emergency stop is active — new orders are blocked"
            )

    def _assert_live_ok(self) -> None:
        if self.mode.is_real and not self.live_allowed:
            raise BrokerRejectedError(
                "Live mode requested but live execution is not enabled (rule 15)"
            )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def execute_signal(
        self,
        *,
        signal: StrategySignal,
        tick: Tick,
        ctx: RiskContext,
        bot_id: str,
        trailing: TrailingConfig | None = None,
    ) -> ExecutionResult:
        """The single path from a strategy signal to a live/paper position."""
        async with self._lock:
            try:
                self._assert_not_stopped()
            except EmergencyStopError as exc:
                return ExecutionResult(
                    state=OrderState.REJECTED, message=str(exc),
                    client_order_id="", order=None,
                )

            if self.kill_switch_engaged:
                return ExecutionResult(
                    state=OrderState.RISK_REJECTED,
                    message="Kill switch is engaged — trading is halted",
                    client_order_id="", order=None,
                )

            try:
                self._assert_live_ok()
            except BrokerRejectedError as exc:
                # Fail closed, and surface it as a rejection rather than an
                # exception so the API and bot loop can report it uniformly.
                return ExecutionResult(
                    state=OrderState.REJECTED, message=str(exc),
                    client_order_id="", order=None,
                )
            spec = get_spec(signal.symbol)
            side = (
                Side.BUY if signal.action.value == "BUY" else Side.SELL
            ) if hasattr(signal.action, "value") else Side(signal.action)

            client_order_id = make_client_order_id(
                bot_id, signal.symbol, side, int(signal.created_at.timestamp())
            )

            # --- 0. Idempotent replay -------------------------------------
            if client_order_id in self._idempotency:
                existing = self._idempotency[client_order_id]
                return ExecutionResult(
                    state=OrderState.CONFIRMED, order=existing, message="Idempotent replay",
                    client_order_id=client_order_id,
                )

            # --- 1. Stop loss validation ----------------------------------
            sl_decision = self.risk.evaluate_stop_loss(
                symbol=signal.symbol, side=side, stop_loss=signal.stop_loss,
                entry_price=tick.price_for(side), tick=tick, spec=spec,
            )
            if sl_decision.blocked:
                await self._emit(OrderState.RISK_REJECTED, bot_id, signal, sl_decision)
                return ExecutionResult(
                    state=OrderState.RISK_REJECTED, decision=sl_decision,
                    message=sl_decision.reason, client_order_id=client_order_id,
                )

            # --- 2. Sizing ------------------------------------------------
            sizing = self.sizer.compute(
                SizingRequest(
                    symbol=signal.symbol, side=side, equity=ctx.equity,
                    risk_percent=self.risk.config.max_risk_percent_per_trade,
                    stop_loss=signal.stop_loss, entry_price=tick.price_for(side),
                    atr=signal.atr, spec=spec,
                )
            )
            if not sizing.ok:
                decision = RiskDecision.__new__(RiskDecision)
                message = sizing.skipped_reason or "Sizing failed"
                await self._emit(OrderState.RISK_REJECTED, bot_id, signal, None, message)
                return ExecutionResult(
                    state=OrderState.RISK_REJECTED, message=message,
                    client_order_id=client_order_id,
                )

            # --- 3. Risk ---------------------------------------------------
            decision = self.risk.evaluate(
                symbol=signal.symbol, side=side, volume=sizing.volume,
                tick=tick, spec=spec, signal_id=client_order_id, ctx=ctx,
            )
            if decision.blocked:
                await self._emit(OrderState.RISK_REJECTED, bot_id, signal, decision)
                return ExecutionResult(
                    state=OrderState.RISK_REJECTED, decision=decision,
                    message=decision.reason, client_order_id=client_order_id,
                )

            transition_order(OrderState.VALIDATED, OrderState.RISK_APPROVED)
            await self._emit(OrderState.RISK_APPROVED, bot_id, signal, decision)

            # --- 4. Build the request -------------------------------------
            tc = trailing or TrailingConfig()
            request = OrderRequest(
                client_order_id=client_order_id,
                symbol=signal.symbol,
                side=side,
                volume=sizing.volume,
                order_type=OrderType.MARKET,
                stop_loss=sizing.stop_loss,
                take_profit=sizing.take_profit,
                trailing_mode=tc.mode,
                trailing_distance_pips=tc.distance_pips,
                trailing_step_pips=tc.step_pips,
                break_even_trigger_pips=tc.break_even_trigger_pips,
                break_even_offset_pips=tc.break_even_offset_pips,
                atr_period=tc.atr_period,
                meta={"atr": signal.atr, "reason": signal.reason,
                      "confidence": signal.confidence, "bot_id": bot_id},
            )

            # --- 5. Submit with explicit state transitions -----------------
            transition_order(OrderState.RISK_APPROVED, OrderState.SUBMITTING)
            try:
                result = await self.broker.submit_order(request)
            except BrokerRejectedError as exc:
                transition_order(OrderState.SUBMITTING, OrderState.REJECTED)
                await self._emit(OrderState.REJECTED, bot_id, signal, None, str(exc))
                return ExecutionResult(
                    state=OrderState.REJECTED, message=str(exc),
                    client_order_id=client_order_id,
                )
            except BrokerTimeoutError as exc:
                transition_order(OrderState.SUBMITTING, OrderState.AMBIGUOUS)
                await self._emit(OrderState.AMBIGUOUS, bot_id, signal, None, str(exc))
                reconciled = await self._reconcile_submission(request, bot_id, signal)
                return ExecutionResult(
                    state=reconciled.state, order=reconciled.order,
                    message=reconciled.message or str(exc), client_order_id=client_order_id,
                )
            except BrokerError as exc:
                transition_order(OrderState.SUBMITTING, OrderState.FAILED)
                await self._emit(OrderState.FAILED, bot_id, signal, None, str(exc))
                return ExecutionResult(
                    state=OrderState.FAILED, message=str(exc), client_order_id=client_order_id,
                )

            if result.ambiguous:
                transition_order(OrderState.SUBMITTING, OrderState.AMBIGUOUS)
                await self._emit(OrderState.AMBIGUOUS, bot_id, signal, None, result.message)
                reconciled = await self._reconcile_submission(request, bot_id, signal)
                return ExecutionResult(
                    state=reconciled.state, order=reconciled.order,
                    message=reconciled.message or result.message,
                    client_order_id=client_order_id,
                )

            if not result.accepted or result.order is None:
                transition_order(OrderState.SUBMITTING, OrderState.REJECTED)
                await self._emit(OrderState.REJECTED, bot_id, signal, None, result.message)
                return ExecutionResult(
                    state=OrderState.REJECTED, message=result.message,
                    client_order_id=client_order_id,
                )

            order = result.order
            self._idempotency[client_order_id] = order
            transition_order(OrderState.SUBMITTING, OrderState.CONFIRMED)

            # --- 6. Confirm the fill with the broker (rule 6) -------------
            confirmed = order
            if order.broker_order_id:
                fresh = await self.broker.get_order(order.broker_order_id)
                if fresh is not None:
                    confirmed = fresh
                    if fresh.status is OrderStatus.FILLED:
                        self._idempotency[client_order_id] = fresh
                    else:
                        # Accepted but not yet filled: still NOT confirmed.
                        await self._emit(
                            OrderState.SUBMITTED, bot_id, signal, None,
                            f"Order accepted, status {fresh.status.value}",
                        )
                        return ExecutionResult(
                            state=OrderState.SUBMITTED, order=fresh,
                            message="Accepted by broker, awaiting fill",
                            client_order_id=client_order_id,
                        )

            await self._emit(OrderState.CONFIRMED, bot_id, signal, decision, result.message)
            return ExecutionResult(
                state=OrderState.CONFIRMED, order=confirmed, decision=decision,
                message=result.message, client_order_id=client_order_id,
            )

    async def _reconcile_submission(
        self, request: OrderRequest, bot_id: str, signal: StrategySignal
    ) -> ExecutionResult:
        """An ambiguous submit means we ASK, we do not retry (rule 6)."""
        transition_order(OrderState.AMBIGUOUS, OrderState.RECONCILING)
        try:
            open_orders = await self.broker.get_orders(only_open=True)
            recent = [
                o for o in open_orders
                if o.client_order_id == request.client_order_id
                or (o.symbol == request.symbol and o.side is request.side
                    and abs((o.created_at - self.clock()).total_seconds()) < 60)
            ]
            if recent:
                await self._emit(
                    OrderState.CONFIRMED, bot_id, signal, None,
                    "Reconciliation found the order at the broker",
                )
                transition_order(OrderState.RECONCILING, OrderState.CONFIRMED)
                self._idempotency[request.client_order_id] = recent[0]
                return ExecutionResult(
                    state=OrderState.CONFIRMED, order=recent[0],
                    message="Order located at broker during reconciliation",
                    client_order_id=request.client_order_id,
                )

            positions = await self.broker.get_positions()
            for pos in positions:
                if pos.symbol == request.symbol and pos.side is request.side:
                    await self._emit(
                        OrderState.CONFIRMED, bot_id, signal, None,
                        "Reconciliation found the position at the broker",
                    )
                    transition_order(OrderState.RECONCILING, OrderState.CONFIRMED)
                    return ExecutionResult(
                        state=OrderState.CONFIRMED, position=pos,
                        message="Position located at broker during reconciliation",
                        client_order_id=request.client_order_id,
                    )

            transition_order(OrderState.RECONCILING, OrderState.REJECTED)
            await self._emit(
                OrderState.REJECTED, bot_id, signal, None,
                "Order not found at broker after ambiguous submit; nothing was sent twice",
            )
            return ExecutionResult(
                state=OrderState.REJECTED,
                message="No matching order at broker — treated as not executed. "
                        "Manual review recommended.",
                client_order_id=request.client_order_id,
            )
        except BrokerError as exc:
            logger.error("Reconciliation failed: %s", exc)
            return ExecutionResult(
                state=OrderState.AMBIGUOUS,
                message=f"Could not reconcile ambiguous submit: {exc}",
                client_order_id=request.client_order_id,
            )

    # ------------------------------------------------------------------ #
    async def close_position(self, broker_position_id: str, volume: float | None = None) -> bool:
        """Closing existing risk is always allowed, even under emergency stop."""
        try:
            result = await self.broker.close_position(broker_position_id, volume)
            await self.events.publish(EngineEvent.manual(
                "position.closing", {"broker_position_id": broker_position_id}
            )) if self.events else None
            return result.accepted
        except BrokerError as exc:
            logger.error("Close failed for %s: %s", broker_position_id, exc)
            return False

    async def _emit(
        self,
        state: OrderState,
        bot_id: str,
        signal: StrategySignal,
        decision: RiskDecision | None,
        message: str = "",
    ) -> None:
        if self.events is None:
            return
        await self.events.publish(
            EngineEvent.order_state(
                bot_id=bot_id, symbol=signal.symbol, state=state.value,
                action=signal.action.value, confidence=signal.confidence,
                reason=signal.reason, risk_reason=decision.reason if decision else None,
                risk_rule=decision.rule.value if decision and decision.rule else None,
                message=message,
            )
        )
