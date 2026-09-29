"""Risk engine and position sizer — the gate every order must pass."""

from datetime import timedelta

import pytest

from trading_engine.core.types import (
    Side,
    Tick,
    TradingMode,
    get_spec,
    utcnow,
)
from trading_engine.risk.engine import RiskConfig, RiskContext, RiskEngine
from trading_engine.risk.sizing import PositionSizer, SizingRequest


@pytest.fixture
def spec():
    return get_spec("EURUSD")


def ctx(**kw) -> RiskContext:
    base = dict(equity=10_000.0, balance=10_000.0, daily_pnl=0.0, open_positions=[],
                now=utcnow(), broker_connected=True, leverage=100, live_enabled=False)
    base.update(kw)
    return RiskContext(**base)


def config(**kw) -> RiskConfig:
    base = dict(trading_mode=TradingMode.PAPER, allowed_symbols=("EURUSD",),
                max_spread_pips=5.0, max_risk_percent_per_trade=1.0,
                max_daily_loss_percent=5.0, max_open_positions=3,
                max_lots_total=5.0)
    base.update(kw)
    return RiskConfig(**base)


def fresh_tick(bid=1.0850, ask=1.0851, ts=None) -> Tick:
    return Tick("EURUSD", bid, ask, ts or utcnow())


class TestHappyPath:
    def test_valid_order_is_approved(self, spec):
        r = RiskEngine(config())
        d = r.evaluate(symbol="EURUSD", side=Side.BUY, volume=0.1,
                       tick=fresh_tick(), spec=spec, ctx=ctx())
        assert d.approved, d.reason

    def test_approval_has_no_rule_triggered(self, spec):
        d = RiskEngine(config()).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(), spec=spec, ctx=ctx())
        assert d.rule is None


class TestStaleData:
    def test_stale_tick_is_blocked(self, spec):
        old = utcnow() - timedelta(seconds=120)
        d = RiskEngine(config()).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(ts=old), spec=spec, ctx=ctx())
        assert d.blocked
        assert "old" in d.reason.lower()

    def test_tick_just_inside_the_window_passes(self, spec):
        ok = utcnow() - timedelta(seconds=5)
        d = RiskEngine(config()).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(ts=ok), spec=spec, ctx=ctx())
        assert d.approved, d.reason

    def test_future_tick_is_blocked(self, spec):
        # Guards against a broker feeding timestamps from the wrong clock.
        ahead = utcnow() + timedelta(minutes=5)
        d = RiskEngine(config()).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(ts=ahead), spec=spec, ctx=ctx())
        assert d.blocked


class TestSpread:
    def test_wide_spread_is_blocked(self, spec):
        d = RiskEngine(config(max_spread_pips=2.0)).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(1.0850, 1.0950), spec=spec, ctx=ctx())
        assert d.blocked
        assert "spread" in d.reason.lower()

    def test_crossed_quote_is_rejected_at_construction(self):
        # A crossed market is corrupt data and must never become a Tick.
        with pytest.raises(ValueError, match="crossed quote"):
            Tick("EURUSD", 1.0850, 1.0840, utcnow())

    def test_zero_width_quote_is_allowed(self, spec):
        d = RiskEngine(config(max_spread_pips=1.0)).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(1.0850, 1.0850), spec=spec, ctx=ctx())
        assert d.approved, d.reason

    def test_zero_spread_is_allowed(self, spec):
        d = RiskEngine(config(max_spread_pips=1.0)).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(1.0850, 1.0850), spec=spec, ctx=ctx())
        assert d.approved, d.reason


class TestSymbolAllowList:
    def test_symbol_outside_allowlist_is_blocked(self, spec):
        d = RiskEngine(config()).evaluate(
            symbol="GBPJPY", side=Side.BUY, volume=0.1,
            tick=fresh_tick(), spec=spec, ctx=ctx())
        assert d.blocked

    def test_empty_allowlist_blocks_everything(self, spec):
        # Fail closed: an unconfigured allowlist must not become "trade anything".
        d = RiskEngine(config(allowed_symbols=())).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(), spec=spec, ctx=ctx())
        assert d.blocked


class TestVolumeLimits:
    def test_volume_above_broker_max_is_blocked(self, spec):
        d = RiskEngine(config()).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=spec.max_volume + 1,
            tick=fresh_tick(), spec=spec, ctx=ctx())
        assert d.blocked

    def test_volume_below_broker_min_is_blocked(self, spec):
        d = RiskEngine(config()).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.001,
            tick=fresh_tick(), spec=spec, ctx=ctx())
        assert d.blocked

    def test_exposure_cap_is_blocked(self, spec):
        d = RiskEngine(config(max_lots_total=1.0)).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=2.0,
            tick=fresh_tick(), spec=spec, ctx=ctx())
        assert d.blocked


class TestDailyLoss:
    def test_daily_loss_limit_trips(self, spec):
        d = RiskEngine(config(max_daily_loss_percent=5.0)).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(), spec=spec, ctx=ctx(daily_pnl=-600.0))
        assert d.blocked
        assert "daily" in d.reason.lower()

    def test_small_daily_loss_is_tolerated(self, spec):
        d = RiskEngine(config(max_daily_loss_percent=5.0)).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(), spec=spec, ctx=ctx(daily_pnl=-100.0))
        assert d.approved, d.reason


class TestKillSwitch:
    def test_kill_switch_blocks_everything(self, spec):
        d = RiskEngine(config(kill_switch=True)).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(), spec=spec, ctx=ctx())
        assert d.blocked

    def test_trading_disabled_blocks_everything(self, spec):
        d = RiskEngine(config(trading_enabled=False)).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(), spec=spec, ctx=ctx())
        assert d.blocked


class TestBrokerConnection:
    def test_disconnected_broker_is_blocked(self, spec):
        d = RiskEngine(config()).evaluate(
            symbol="EURUSD", side=Side.BUY, volume=0.1,
            tick=fresh_tick(), spec=spec, ctx=ctx(broker_connected=False))
        assert d.blocked


class TestStopLossValidation:
    def test_long_stop_above_entry_is_rejected(self, spec):
        d = RiskEngine(config()).evaluate_stop_loss(
            symbol="EURUSD", side=Side.BUY, stop_loss=1.0900,
            entry_price=1.0850, tick=fresh_tick(), spec=spec)
        assert d.blocked

    def test_short_stop_below_entry_is_rejected(self, spec):
        d = RiskEngine(config()).evaluate_stop_loss(
            symbol="EURUSD", side=Side.SELL, stop_loss=1.0800,
            entry_price=1.0850, tick=fresh_tick(), spec=spec)
        assert d.blocked

    def test_correct_side_is_accepted(self, spec):
        d = RiskEngine(config()).evaluate_stop_loss(
            symbol="EURUSD", side=Side.BUY, stop_loss=1.0800,
            entry_price=1.0850, tick=fresh_tick(), spec=spec)
        assert not d.blocked

    def test_stop_at_entry_is_rejected(self, spec):
        d = RiskEngine(config()).evaluate_stop_loss(
            symbol="EURUSD", side=Side.BUY, stop_loss=1.0850,
            entry_price=1.0850, tick=fresh_tick(), spec=spec)
        assert d.blocked

    def test_missing_stop_is_rejected(self, spec):
        # Every position must be protected before it reaches the broker.
        d = RiskEngine(config()).evaluate_stop_loss(
            symbol="EURUSD", side=Side.BUY, stop_loss=None,
            entry_price=1.0850, tick=fresh_tick(), spec=spec)
        assert d.blocked

    def test_stop_inside_broker_minimum_distance_is_rejected(self, spec):
        # min_stop_distance is a property of the instrument, not the config.
        d = RiskEngine(config()).evaluate_stop_loss(
            symbol="EURUSD", side=Side.BUY, stop_loss=1.0850 - spec.min_stop_distance / 2,
            entry_price=1.0850, tick=fresh_tick(), spec=spec)
        assert d.blocked

    def test_stop_beyond_broker_minimum_distance_is_accepted(self, spec):
        d = RiskEngine(config()).evaluate_stop_loss(
            symbol="EURUSD", side=Side.BUY,
            stop_loss=1.0850 - spec.pips_to_price(50),
            entry_price=1.0850, tick=fresh_tick(), spec=spec)
        assert not d.blocked, d.reason


class TestSizer:
    def test_sizing_uses_explicit_stop(self, spec):
        r = PositionSizer().compute(SizingRequest(
            symbol="EURUSD", side=Side.BUY, equity=10_000.0, risk_percent=1.0,
            stop_loss=1.0840, entry_price=1.0850, spec=spec))
        assert r.ok
        assert r.volume > 0
        assert r.stop_loss == 1.0840

    def test_sizing_derives_stop_from_atr(self, spec):
        r = PositionSizer().compute(SizingRequest(
            symbol="EURUSD", side=Side.BUY, equity=10_000.0, risk_percent=1.0,
            stop_loss=None, entry_price=1.0850, atr=0.0010, spec=spec))
        assert r.ok
        assert r.stop_loss is not None

    def test_sizing_derives_stop_from_pips(self, spec):
        r = PositionSizer().compute(SizingRequest(
            symbol="EURUSD", side=Side.BUY, equity=10_000.0, risk_percent=1.0,
            stop_loss=None, entry_price=1.0850, pip_stop_distance=30.0, spec=spec))
        assert r.ok
        assert r.stop_distance_pips == pytest.approx(30.0, abs=0.1)

    def test_no_stop_available_is_skipped(self, spec):
        r = PositionSizer().compute(SizingRequest(
            symbol="EURUSD", side=Side.BUY, equity=10_000.0, risk_percent=1.0,
            stop_loss=None, entry_price=1.0850, spec=spec))
        assert not r.ok
        assert "stop" in r.skipped_reason.lower()

    def test_zero_stop_distance_is_skipped(self, spec):
        r = PositionSizer().compute(SizingRequest(
            symbol="EURUSD", side=Side.BUY, equity=10_000.0, risk_percent=1.0,
            stop_loss=1.0850, entry_price=1.0850, spec=spec))
        assert not r.ok

    def test_take_profit_respects_reward_ratio(self, spec):
        r = PositionSizer(default_rr=2.0).compute(SizingRequest(
            symbol="EURUSD", side=Side.BUY, equity=10_000.0, risk_percent=1.0,
            stop_loss=1.0840, entry_price=1.0850, spec=spec))
        risk = abs(1.0850 - 1.0840)
        assert r.take_profit == pytest.approx(1.0850 + risk * 2.0, abs=1e-5)

    def test_short_take_profit_is_below_entry(self, spec):
        r = PositionSizer(default_rr=2.0).compute(SizingRequest(
            symbol="EURUSD", side=Side.SELL, equity=10_000.0, risk_percent=1.0,
            stop_loss=1.0860, entry_price=1.0850, spec=spec))
        assert r.take_profit < 1.0850

    def test_shorter_risk_produces_larger_size(self, spec):
        sizer = PositionSizer()
        tight = sizer.compute(SizingRequest(
            symbol="EURUSD", side=Side.BUY, equity=10_000.0, risk_percent=1.0,
            stop_loss=1.0840, entry_price=1.0850, spec=spec))
        wide = sizer.compute(SizingRequest(
            symbol="EURUSD", side=Side.BUY, equity=10_000.0, risk_percent=1.0,
            stop_loss=1.0700, entry_price=1.0850, spec=spec))
        assert tight.volume > wide.volume
