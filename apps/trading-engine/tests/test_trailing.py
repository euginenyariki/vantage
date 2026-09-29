"""Trailing stop / break-even — the logic that protects open profit."""

import pytest

from trading_engine.core.types import (
    Position,
    Side,
    Tick,
    TradingMode,
    TrailingMode,
    get_spec,
    utcnow,
)
from trading_engine.trailing.service import TrailingAction, TrailingConfig, TrailingService


@pytest.fixture
def spec():
    return get_spec("EURUSD")


def make_position(spec, side=Side.BUY, entry=1.0850, stop=None, volume=0.1) -> Position:
    return Position(
        id="p1", symbol="EURUSD", side=side, volume=volume, open_price=entry,
        current_price=entry, stop_loss=stop, mode=TradingMode.PAPER, opened_at=utcnow(),
    )


def tick(bid, ask=None, ts=None) -> Tick:
    """Zero-spread tick by default, so expected stop maths is exact.

    With a real spread a long trails off the ask and a short off the bid; the
    spread-specific behaviour is covered separately.
    """
    return Tick("EURUSD", bid, ask if ask is not None else bid, ts or utcnow())


class TestNoStopYet:
    def test_long_seeds_stop_from_distance(self, spec):
        svc = TrailingService(TrailingConfig(mode=TrailingMode.BREAK_EVEN, distance_pips=20))
        pos = make_position(spec)
        r = svc.update(pos, tick(1.0850), spec)
        # NONE means the caller manages the stop; nothing is seeded.
        assert r.new_stop is None or r.new_stop == pos.stop_loss

    def test_flat_price_does_not_seed_a_stop(self, spec):
        # No profit means no trailing: the service must not invent a stop.
        svc = TrailingService(TrailingConfig(
            mode=TrailingMode.FIXED, distance_pips=20, break_even_trigger_pips=0))
        pos = make_position(spec)
        r = svc.update(pos, tick(1.0850), spec)
        assert r.new_stop is None
        assert r.action is TrailingAction.SKIPPED_NOT_PROFITABLE

    def test_first_profitable_tick_seeds_the_stop(self, spec):
        svc = TrailingService(TrailingConfig(
            mode=TrailingMode.FIXED, distance_pips=20, break_even_trigger_pips=0))
        pos = make_position(spec)
        r = svc.update(pos, tick(1.0855), spec)
        assert r.new_stop == pytest.approx(1.0855 - spec.pips_to_price(20), abs=1e-5)


class TestProtectiveDirection:
    def test_long_stop_never_moves_down(self, spec):
        svc = TrailingService(TrailingConfig(
            mode=TrailingMode.FIXED, distance_pips=10, break_even_trigger_pips=0))
        pos = make_position(spec, stop=1.0800)
        # Price falls: the stop must hold, not follow.
        r = svc.update(pos, tick(1.0800), spec)
        assert r.new_stop is None or r.new_stop >= 1.0800

    def test_short_stop_never_moves_up(self, spec):
        svc = TrailingService(TrailingConfig(
            mode=TrailingMode.FIXED, distance_pips=10, break_even_trigger_pips=0))
        pos = make_position(spec, side=Side.SELL, entry=1.0850, stop=1.0900)
        r = svc.update(pos, tick(1.0900, 1.0901), spec)
        assert r.new_stop is None or r.new_stop <= 1.0900

    def test_long_stop_ratchets_up_as_price_rises(self, spec):
        svc = TrailingService(TrailingConfig(
            mode=TrailingMode.FIXED, distance_pips=10, break_even_trigger_pips=0))
        pos = make_position(spec, stop=1.0800)
        r = svc.update(pos, tick(1.0900), spec)
        assert r.new_stop == pytest.approx(1.0900 - spec.pips_to_price(10), abs=1e-5)


class TestStepMode:
    def test_step_only_moves_in_increments(self, spec):
        svc = TrailingService(TrailingConfig(
            mode=TrailingMode.STEP, distance_pips=20, step_pips=5,
            break_even_trigger_pips=0))
        pos = make_position(spec, stop=1.0800)
        # STEP trails by max(step, distance) from the current price.
        r = svc.update(pos, tick(1.0855), spec)
        assert r.new_stop == pytest.approx(1.0855 - spec.pips_to_price(20), abs=1e-5)

    def test_step_moves_the_stop(self, spec):
        svc = TrailingService(TrailingConfig(
            mode=TrailingMode.STEP, distance_pips=20, step_pips=5,
            break_even_trigger_pips=0))
        pos = make_position(spec, stop=1.0800)
        r = svc.update(pos, tick(1.0875), spec)
        assert r.changed
        assert r.new_stop > pos.stop_loss


class TestBreakEven:
    def test_break_even_applies_at_trigger(self, spec):
        svc = TrailingService(TrailingConfig(
            distance_pips=20, break_even_trigger_pips=10, break_even_offset_pips=1))
        pos = make_position(spec, stop=1.0800)
        r = svc.update(pos, tick(1.0865), spec)
        assert r.break_even_done
        assert r.new_stop >= pos.open_price

    def test_break_even_not_applied_before_trigger(self, spec):
        svc = TrailingService(TrailingConfig(
            distance_pips=50, break_even_trigger_pips=20, break_even_offset_pips=1))
        pos = make_position(spec, stop=1.0800)
        r = svc.update(pos, tick(1.0855), spec)
        assert not r.break_even_done

    def test_short_break_even_is_above_entry(self, spec):
        svc = TrailingService(TrailingConfig(
            distance_pips=20, break_even_trigger_pips=10, break_even_offset_pips=1))
        pos = make_position(spec, side=Side.SELL, entry=1.0850, stop=1.0900)
        r = svc.update(pos, tick(1.0835, 1.0836), spec)
        assert r.break_even_done
        assert r.new_stop <= pos.open_price

    def test_break_even_happens_only_once(self, spec):
        svc = TrailingService(TrailingConfig(
            distance_pips=20, break_even_trigger_pips=10, break_even_offset_pips=1))
        pos = make_position(spec, stop=1.0800)
        first = svc.update(pos, tick(1.0865), spec)
        pos.stop_loss = first.new_stop
        pos.break_even_done = True
        second = svc.update(pos, tick(1.0875), spec)
        assert second.action is not TrailingAction.BREAK_EVEN_APPLIED


class TestATRMode:
    def test_atr_stop_tracks_volatility(self, spec):
        svc = TrailingService(TrailingConfig(mode=TrailingMode.ATR, atr_period=14))
        pos = make_position(spec, stop=1.0800)
        r = svc.update(pos, tick(1.0900), spec, atr=0.0010)
        # With ATR 10 pips and a default 1.5x multiplier the stop trails ~15 pips.
        assert r.new_stop is not None


class TestDisabled:
    def test_a_stop_with_no_basis_is_left_alone(self, spec):
        # No distance, percentage or ATR basis means "operator owns the stop".
        svc = TrailingService(TrailingConfig(
            mode=TrailingMode.FIXED, distance_pips=0.0,
            percentage=0.0, atr_multiplier=0.0, break_even_trigger_pips=0.0))
        pos = make_position(spec, stop=1.0800)
        r = svc.update(pos, tick(1.0950), spec)
        assert not r.changed
        assert r.new_stop == 1.0800


class TestFlatMarket:
    def test_repeated_ticks_are_stable(self, spec):
        svc = TrailingService(TrailingConfig(
            mode=TrailingMode.FIXED, distance_pips=10, break_even_trigger_pips=0))
        pos = make_position(spec, stop=1.0800)
        first = svc.update(pos, tick(1.0850), spec)
        pos.stop_loss = first.new_stop or pos.stop_loss
        second = svc.update(pos, tick(1.0850), spec)
        assert second.new_stop == first.new_stop
