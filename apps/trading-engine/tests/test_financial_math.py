"""Financial math — the foundation everything else trusts."""


import pytest

from trading_engine.core.types import (
    Side,
    TradingMode,
    clamp,
    get_spec,
    max_drawdown,
    position_margin,
    position_pnl,
    profit_factor,
    round_price,
    round_volume,
    size_position,
    unrealized_pnl,
)


class TestPips:
    def test_eurusd_pip_is_0_0001(self):
        spec = get_spec("EURUSD")
        assert spec.price_to_pips(1.2350 - 1.2340) == pytest.approx(10.0)

    def test_xauusd_pip_is_0_01(self):
        # Metals quote to 2 decimals, so a "pip" there is 0.01.
        spec = get_spec("XAUUSD")
        assert spec.pip_size == 0.01
        assert spec.price_to_pips(2400.0 - 2399.5) == pytest.approx(50.0)

    def test_negative_direction(self):
        spec = get_spec("EURUSD")
        assert spec.price_to_pips(1.2340 - 1.2350) == pytest.approx(-10.0)

    def test_zero_delta(self):
        assert get_spec("EURUSD").price_to_pips(0.0) == 0.0


class TestRounding:
    def test_price_rounds_to_symbol_digits(self):
        assert round_price(1.23456789, 5) == 1.23457
        assert round_price(1.23456789, 3) == 1.235

    def test_volume_rounds_down_never_up(self):
        # Rounding up would silently increase risk past the approved budget.
        assert round_volume(0.0199, 0.01) == 0.01
        assert round_volume(0.999, 0.01) == 0.99
        assert round_volume(1.0, 0.01) == 1.0

    def test_volume_with_zero_step_is_zero(self):
        assert round_volume(1.0, 0.0) == 0.0

    def test_clamp(self):
        assert clamp(5, 1, 3) == 3
        assert clamp(0, 1, 3) == 1


class TestSymbolSpecValue:
    def test_value_per_price_unit_matches_pip_value(self):
        spec = get_spec("EURUSD")
        # 1.0 price unit = 10000 pips; a standard lot is $10/pip.
        assert spec.value_per_price_unit(1.0) == pytest.approx(100_000.0)

    def test_value_scales_linearly_with_volume(self):
        spec = get_spec("EURUSD")
        assert spec.value_per_price_unit(0.5) == pytest.approx(
            spec.value_per_price_unit(1.0) * 0.5
        )

    def test_pips_to_price_roundtrip(self):
        spec = get_spec("EURUSD")
        assert spec.price_to_pips(spec.pips_to_price(25)) == pytest.approx(25.0)

    def test_unknown_symbol_falls_back_to_default(self):
        assert get_spec("NOT_A_SYMBOL").digits == 5


class TestPositionPnl:
    def test_long_profit(self):
        spec = get_spec("EURUSD")
        # 0.1 lot, 50 pip move = $50
        pnl = position_pnl(Side.BUY, 0.1, 1.2000, 1.2050, spec)
        assert pnl == pytest.approx(50.0, abs=0.01)

    def test_short_profit(self):
        spec = get_spec("EURUSD")
        pnl = position_pnl(Side.SELL, 0.1, 1.2000, 1.1950, spec)
        assert pnl == pytest.approx(50.0, abs=0.01)

    def test_long_loss_is_negative(self):
        spec = get_spec("EURUSD")
        pnl = position_pnl(Side.BUY, 0.1, 1.2000, 1.1950, spec)
        assert pnl == pytest.approx(-50.0, abs=0.01)

    def test_commission_reduces_pnl(self):
        spec = get_spec("EURUSD")
        gross = position_pnl(Side.BUY, 1.0, 1.2000, 1.2100, spec)
        net = position_pnl(Side.BUY, 1.0, 1.2000, 1.2100, spec, commission=3.5)
        assert net == pytest.approx(gross - 3.5)

    def test_zero_volume_is_zero(self):
        spec = get_spec("EURUSD")
        assert position_pnl(Side.BUY, 0.0, 1.2, 1.3, spec) == 0.0

    def test_unrealized_matches_realized_at_close_price(self):
        from trading_engine.core.types import Position, utcnow

        spec = get_spec("EURUSD")
        pos = Position(
            id="t1", symbol="EURUSD", side=Side.BUY, volume=0.25, open_price=1.2000,
            current_price=1.2000, mode=TradingMode.PAPER, opened_at=utcnow(),
        )
        realized = position_pnl(Side.BUY, 0.25, 1.2000, 1.2100, spec)
        floating = unrealized_pnl(pos, 1.2100, spec)
        assert floating == pytest.approx(realized)

    def test_unrealized_is_negative_when_price_moves_against_a_long(self):
        from trading_engine.core.types import Position, utcnow

        pos = Position(
            id="t1", symbol="EURUSD", side=Side.BUY, volume=0.25, open_price=1.2000,
            current_price=1.2000, mode=TradingMode.PAPER, opened_at=utcnow(),
        )
        assert unrealized_pnl(pos, 1.1900, get_spec("EURUSD")) < 0


class TestPositionSizing:
    @pytest.mark.parametrize("pips_stop", [10, 20, 50, 100, 200, 1000])
    def test_risk_is_exact_across_stop_distances(self, pips_stop):
        spec = get_spec("EURUSD")
        equity, risk_pct = 10_000.0, 1.0
        stop = spec.pips_to_price(pips_stop)
        lots = size_position(equity, risk_pct, stop, spec)
        actual_risk = lots * spec.value_per_price_unit(1.0) * stop
        assert actual_risk == pytest.approx(equity * risk_pct / 100, rel=0.02)

    def test_never_exceeds_budget(self):
        spec = get_spec("EURUSD")
        stop = spec.pips_to_price(37)
        lots = size_position(10_000, 1.0, stop, spec)
        risk = lots * spec.value_per_price_unit(1.0) * stop
        assert risk <= 100.0 + 1e-6

    def test_respects_max_volume(self):
        spec = get_spec("EURUSD")
        lots = size_position(10_000_000, 50.0, spec.pips_to_price(5), spec,
                            max_volume=1.0)
        assert lots == 1.0

    def test_zero_equity_is_rejected(self):
        spec = get_spec("EURUSD")
        assert size_position(0, 1.0, 0.001, spec) == 0.0

    def test_zero_risk_percent_is_rejected(self):
        spec = get_spec("EURUSD")
        assert size_position(10_000, 0.0, 0.001, spec) == 0.0

    def test_zero_stop_distance_is_rejected(self):
        spec = get_spec("EURUSD")
        assert size_position(10_000, 1.0, 0.0, spec) == 0.0


class TestMargin:
    def test_margin_scales_with_leverage(self):
        spec = get_spec("EURUSD")
        notional = spec.contract_size * 1.2
        assert position_margin(1.0, 1.2, spec, 100) == pytest.approx(notional / 100)
        assert position_margin(1.0, 1.2, spec, 30) == pytest.approx(notional / 30)

    def test_margin_is_linear_in_volume(self):
        spec = get_spec("EURUSD")
        assert position_margin(0.5, 1.2, spec, 100) == pytest.approx(
            position_margin(1.0, 1.2, spec, 100) / 2
        )


class TestPerformanceMetrics:
    def test_profit_factor(self):
        assert profit_factor(300, 150) == pytest.approx(2.0)

    def test_profit_factor_no_losses(self):
        assert profit_factor(300, 0) == float("inf")

    def test_profit_factor_no_wins(self):
        assert profit_factor(0, 150) == 0.0

    def test_max_drawdown(self):
        dd, peak = max_drawdown([100, 120, 90, 150])
        assert dd == pytest.approx(0.25)
        assert peak == 30

    def test_max_drawdown_monotonic_growth_is_zero(self):
        assert max_drawdown([100, 110, 120, 130])[0] == 0.0

    def test_max_drawdown_empty_series(self):
        assert max_drawdown([]) == (0.0, 0)
