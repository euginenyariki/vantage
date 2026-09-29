"""Synthetic-but-realistic OHLC generator for tests and demos.

A random walk with per-bar noise, so ATR sits in the range an HFT scalper
actually trades in. Deterministic given a seed.

The drift is ZERO by default on purpose. A generator with a built-in upward
bias hands a long-biased strategy a free profit that no real market gave it,
which is exactly how a demo starts looking like a validated edge. Callers that
genuinely need a trend (a smoke test that must force an entry, a scenario with
a known direction) pass ``trend_pips_per_bar`` explicitly.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from trading_engine.brokers.base import Bar
from trading_engine.core.types import get_spec, round_price


def make_bars(
    symbol: str,
    count: int,
    *,
    start: float = 1.0850,
    seed: int = 3,
    bar_minutes: int = 1,
    noise_pips: float = 7.0,
    trend_pips_per_bar: float = 0.0,
    start_time: datetime | None = None,
) -> list[Bar]:
    spec = get_spec(symbol)
    rng = random.Random(seed)
    vol = spec.pips_to_price(noise_pips)
    drift = spec.pips_to_price(trend_pips_per_bar)
    t = start_time or datetime(2026, 1, 5, 0, 0, tzinfo=UTC)

    bars: list[Bar] = []
    price = start
    for i in range(count):
        o = price
        c = round_price(o + drift + rng.gauss(0, vol), spec.digits)
        h = round_price(max(o, c) + abs(rng.gauss(0, vol * 0.8)), spec.digits)
        low = round_price(min(o, c) - abs(rng.gauss(0, vol * 0.8)), spec.digits)
        bars.append(
            Bar(symbol=symbol, timeframe="M1", open=o, high=h, low=low, close=c,
                volume=rng.uniform(150, 2500),
                timestamp=t + timedelta(minutes=bar_minutes * i))
        )
        price = c
    return bars
