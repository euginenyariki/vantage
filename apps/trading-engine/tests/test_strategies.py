"""Strategy smoke tests.

These strategies were never executed outside the paper-cycle script, which hid
live NameErrors on the signal-construction path (a band-fade signal referenced
renamed locals). Each test drives a strategy through a real setup so the whole
evaluate() body runs, not just the warm-up guard.
"""

import math

import pytest

from trading_engine.brokers.base import Bar
from trading_engine.core.types import SignalAction, Tick, get_spec, utcnow
from trading_engine.strategies.base import (
    HFTScalper,
    MeanReversionScalper,
    StrategyContext,
    bollinger,
    macd,
)

SPEC = get_spec("EURUSD")


def make_bars(closes: list[float]) -> list[Bar]:
    return [
        Bar(symbol="EURUSD", timeframe="M1", open=c, high=c + 0.0005,
            low=c - 0.0005, close=c, volume=1.0, timestamp=utcnow())
        for c in closes
    ]


def make_ctx(closes: list[float], price: float) -> StrategyContext:
    return StrategyContext(
        symbol="EURUSD", spec=SPEC, bars=make_bars(closes),
        tick=Tick("EURUSD", price, price, utcnow()),
        equity=10_000.0, open_positions=0,
    )


def sine_closes(n: int = 60) -> list[float]:
    return [1.1000 + 0.002 * math.sin(i / 4) for i in range(n)]


def band_edges(closes: list[float], period: int = 20, mult: float = 2.0):
    w = closes[-period:]
    mean = sum(w) / period
    sd = (sum((x - mean) ** 2 for x in w) / period) ** 0.5
    return mean - mult * sd, mean + mult * sd


class TestIndicators:
    def test_bollinger_bands_are_ordered(self):
        closes = sine_closes()
        upper, mid, lower = bollinger(closes, 20, 2.0)
        assert len(upper) == len(mid) == len(lower) > 0
        assert all(u >= m >= lo for u, m, lo in zip(upper, mid, lower, strict=True))

    def test_bollinger_last_band_matches_direct_math(self):
        closes = sine_closes()
        upper, _, lower = bollinger(closes, 20, 2.0)
        w = closes[-20:]
        mean = sum(w) / 20
        sd = (sum((x - mean) ** 2 for x in w) / 20) ** 0.5
        assert upper[-1] == pytest.approx(mean + 2 * sd, abs=1e-12)
        assert lower[-1] == pytest.approx(mean - 2 * sd, abs=1e-12)

    def test_bollinger_needs_enough_data(self):
        assert bollinger([1.0] * 5, period=20) == ([], [], [])

    def test_macd_warmup_alignment(self):
        # macd_line starts earlier than signal_line/hist, which need more
        # warm-up. Only hist and signal_line share an alignment.
        closes = sine_closes()
        macd_line, signal_line, hist = macd(closes)
        assert macd_line and signal_line and hist
        assert len(hist) == len(signal_line)
        assert len(macd_line) > len(signal_line)

    def test_macd_hist_is_line_minus_signal(self):
        closes = sine_closes()
        macd_line, signal_line, hist = macd(closes)
        assert hist[-1] == pytest.approx(macd_line[-1] - signal_line[-1], abs=1e-12)

    def test_macd_needs_enough_data(self):
        assert macd([1.0] * 5) == ([], [], [])


class TestMeanReversion:
    def test_buys_the_lower_band(self):
        closes = sine_closes()
        low, _ = band_edges(closes)
        sig = MeanReversionScalper().evaluate(make_ctx(closes, low - 0.0005))
        assert sig.action is SignalAction.BUY
        assert sig.stop_loss is not None

    def test_sells_the_upper_band(self):
        closes = sine_closes()
        _, high = band_edges(closes)
        sig = MeanReversionScalper().evaluate(make_ctx(closes, high + 0.0005))
        assert sig.action is SignalAction.SELL
        assert sig.stop_loss is not None

    def test_holds_inside_the_bands(self):
        closes = sine_closes()
        low, high = band_edges(closes)
        sig = MeanReversionScalper().evaluate(make_ctx(closes, (low + high) / 2))
        assert sig.action is SignalAction.HOLD
        assert sig.stop_loss is None

    def test_stop_is_on_the_protective_side(self):
        closes = sine_closes()
        low, high = band_edges(closes)
        price = low - 0.0005
        sig = MeanReversionScalper().evaluate(make_ctx(closes, price))
        # A long's stop must sit below the entry.
        assert sig.stop_loss < sig.price

    def test_signal_carries_bands_and_atr(self):
        closes = sine_closes()
        low, _ = band_edges(closes)
        sig = MeanReversionScalper().evaluate(make_ctx(closes, low - 0.0005))
        assert sig.atr is not None and sig.atr > 0
        assert sig.meta["bb_lower"] == pytest.approx(low, abs=1e-9)
        assert sig.meta["bb_upper"] > sig.meta["bb_lower"]

    def test_cold_start_holds(self):
        sig = MeanReversionScalper().evaluate(make_ctx([1.1] * 5, 1.1))
        assert sig.action is SignalAction.HOLD


class TestHFTScalper:
    def test_warm_up_holds(self):
        sig = HFTScalper().evaluate(make_ctx([1.1] * 5, 1.1))
        assert sig.action is SignalAction.HOLD

    def test_never_raises_on_a_trend(self):
        # A strong directional run should produce a signal or a hold, but must
        # not blow up on the signal-construction path.
        closes = [1.1000 + 0.0005 * i for i in range(80)]
        for price in (closes[-1], closes[-1] * 1.001, closes[-1] * 0.999):
            sig = HFTScalper().evaluate(make_ctx(closes, price))
            assert sig.action in tuple(SignalAction)

    def test_signal_always_has_a_price(self):
        closes = [1.1000 + 0.0005 * i for i in range(80)]
        sig = HFTScalper().evaluate(make_ctx(closes, closes[-1]))
        assert sig.price > 0
        if sig.action is not SignalAction.HOLD:
            assert sig.stop_loss is not None
