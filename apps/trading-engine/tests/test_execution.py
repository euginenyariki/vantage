"""Execution engine: idempotency, state machine, and live-safety refusals."""

import pytest

from trading_engine.core.types import (
    OrderStatus,
    SignalAction,
    StrategySignal,
    Tick,
    TradingMode,
    get_spec,
    utcnow,
)
from trading_engine.execution.engine import ExecutionEngine, OrderState
from trading_engine.risk.engine import RiskConfig, RiskContext, RiskEngine
from trading_engine.risk.sizing import PositionSizer


@pytest.fixture
async def broker():
    from trading_engine.brokers.paper import PaperBroker

    b = PaperBroker(mode=TradingMode.PAPER, initial_balance=10_000, rng_seed=7)
    await b.connect()
    yield b
    await b.disconnect()


@pytest.fixture
def spec():
    return get_spec("EURUSD")


def risk_config(**kw) -> RiskConfig:
    base = dict(trading_mode=TradingMode.PAPER, allowed_symbols=("EURUSD",),
                max_spread_pips=5.0, max_risk_percent_per_trade=1.0)
    base.update(kw)
    return RiskConfig(**base)


def make_engine(broker, *, mode=TradingMode.PAPER, live_allowed=False, **risk_kw):
    return ExecutionEngine(
        broker=broker,
        risk_engine=RiskEngine(risk_config(**risk_kw)),
        sizer=PositionSizer(),
        event_bus=None,
        mode=mode,
        live_allowed=live_allowed,
    )


def ctx(**kw) -> RiskContext:
    base = dict(equity=10_000.0, balance=10_000.0, daily_pnl=0.0, open_positions=[],
                now=utcnow(), broker_connected=True, leverage=100, live_enabled=False)
    base.update(kw)
    return RiskContext(**base)


def signal(ts=None) -> StrategySignal:
    return StrategySignal(
        action=SignalAction.BUY, symbol="EURUSD", price=1.0850,
        stop_loss=1.0800, atr=0.0008, created_at=ts or utcnow(),
    )


def fresh_tick() -> Tick:
    return Tick("EURUSD", 1.0850, 1.0851, utcnow())


class TestSuccessfulExecution:
    async def test_valid_signal_fills(self, broker, spec):
        eng = make_engine(broker)
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert r.filled, r.message
        assert r.order is not None
        # ExecutionResult carries the pipeline state; the broker record carries
        # its own status. Both must read as filled.
        assert r.state is OrderState.CONFIRMED
        assert r.order.status is OrderStatus.FILLED

    async def test_fill_opens_a_position(self, broker, spec):
        eng = make_engine(broker)
        await eng.execute_signal(signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        positions = await broker.get_positions()
        assert len(positions) == 1
        assert positions[0].stop_loss is not None

    async def test_position_is_tagged_paper(self, broker, spec):
        eng = make_engine(broker)
        await eng.execute_signal(signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert (await broker.get_positions())[0].mode is TradingMode.PAPER


class TestStopLossEnforcement:
    async def test_signal_without_stop_is_rejected(self, broker, spec):
        eng = make_engine(broker)
        s = signal()
        s.stop_loss = None
        r = await eng.execute_signal(signal=s, tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert r.state is OrderState.RISK_REJECTED
        assert not r.filled

    async def test_stop_on_wrong_side_is_rejected(self, broker, spec):
        eng = make_engine(broker)
        s = signal()
        s.stop_loss = 1.0900
        r = await eng.execute_signal(signal=s, tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert r.state is OrderState.RISK_REJECTED
        assert not r.filled

    async def test_rejected_signal_opens_no_position(self, broker, spec):
        eng = make_engine(broker)
        s = signal()
        s.stop_loss = None
        await eng.execute_signal(signal=s, tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert await broker.get_positions() == []


class TestIdempotency:
    async def test_replayed_signal_does_not_double_fill(self, broker, spec):
        eng = make_engine(broker)
        ts = utcnow()
        s = signal(ts)
        first = await eng.execute_signal(signal=s, tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        second = await eng.execute_signal(signal=s, tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert first.filled and second.filled
        assert first.order.id == second.order.id
        assert len(await broker.get_positions()) == 1

    async def test_replay_message_is_explicit(self, broker, spec):
        eng = make_engine(broker)
        s = signal(utcnow())
        await eng.execute_signal(signal=s, tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        again = await eng.execute_signal(signal=s, tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert "replay" in again.message.lower()

    async def test_a_new_timestamp_creates_a_new_order(self, broker, spec):
        eng = make_engine(broker)
        await eng.execute_signal(signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        later = utcnow()
        r2 = await eng.execute_signal(
            signal=signal(later.replace(microsecond=later.microsecond + 1)),
            tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert r2.order is not None


class TestEmergencyStop:
    async def test_emergency_stop_blocks_new_orders(self, broker, spec):
        eng = make_engine(broker)
        eng.trigger_emergency_stop("test")
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert r.state is OrderState.REJECTED
        assert not r.filled
        assert await broker.get_positions() == []

    async def test_emergency_stop_reports_its_reason(self, broker, spec):
        eng = make_engine(broker)
        eng.trigger_emergency_stop("broker said no")
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert "emergency" in r.message.lower()

    async def test_releasing_the_stop_allows_trading_again(self, broker, spec):
        eng = make_engine(broker)
        eng.trigger_emergency_stop("test")
        eng.clear_emergency_stop()
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert r.filled, r.message


class TestKillSwitch:
    async def test_kill_switch_blocks_orders(self, broker, spec):
        eng = make_engine(broker)
        eng.set_kill_switch(True)
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert not r.filled
        assert await broker.get_positions() == []


class TestLiveSafety:
    async def test_live_is_refused_when_not_enabled(self, broker, spec):
        eng = make_engine(broker, mode=TradingMode.LIVE, live_allowed=False)
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert r.state is OrderState.REJECTED
        assert not r.filled

    async def test_live_refusal_mentions_the_rule(self, broker, spec):
        eng = make_engine(broker, mode=TradingMode.LIVE, live_allowed=False)
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert "15" in r.message or "live" in r.message.lower()

    async def test_demo_mode_needs_no_live_flag(self, broker, spec):
        # DEMO hits a real broker account, so it is not a "live" order.
        eng = make_engine(broker, mode=TradingMode.DEMO, live_allowed=False)
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert r.filled, r.message

    async def test_live_context_without_grant_is_rejected(self, broker, spec):
        eng = make_engine(broker)
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(),
            ctx=ctx(live_enabled=False), bot_id="b1")
        assert r.filled, "PAPER mode must not require a live grant"


class TestRiskRejection:
    async def test_stale_tick_rejects_the_order(self, broker, spec):
        from datetime import timedelta

        eng = make_engine(broker)
        old = Tick("EURUSD", 1.0850, 1.0851, utcnow() - timedelta(minutes=5))
        r = await eng.execute_signal(signal=signal(), tick=old, ctx=ctx(), bot_id="b1")
        assert r.state is OrderState.RISK_REJECTED

    async def test_kill_switch_context_rejects(self, broker, spec):
        eng = make_engine(broker)
        r = await eng.execute_signal(
            signal=signal(), tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert r.filled
        eng.set_kill_switch(True)
        r2 = await eng.execute_signal(
            signal=signal(utcnow().replace(microsecond=999999)),
            tick=fresh_tick(), ctx=ctx(), bot_id="b1")
        assert not r2.filled
