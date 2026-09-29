"""Market data feed.

In DEMO/PAPER mode the engine runs a stochastic price simulator so the whole
pipeline can be exercised end to end without a broker connection. The output is
explicitly tagged `SAMPLE DATA` upstream — it is a simulator, never presented
as a real market feed.

In LIVE mode the engine subscribes to the broker adapter and simply forwards
real quotes, stamping the source as `BROKER`.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from trading_engine.core.types import Tick, get_spec, round_price, utcnow


class TickSource(str, Enum):
    SIMULATOR = "SIMULATOR"
    BROKER = "BROKER"
    HISTORICAL = "HISTORICAL"


@dataclass
class SymbolSimConfig:
    symbol: str
    base_price: float
    volatility_pips: float = 8.0
    base_spread_pips: float = 0.6
    spread_jitter_pips: float = 0.25
    drift_pips_per_tick: float = 0.0
    mean_reversion: float = 0.02


DEFAULT_SIM_CONFIG: dict[str, SymbolSimConfig] = {
    "EURUSD": SymbolSimConfig("EURUSD", 1.08500, volatility_pips=7.0, base_spread_pips=0.5),
    "GBPUSD": SymbolSimConfig("GBPUSD", 1.26800, volatility_pips=9.0, base_spread_pips=0.7),
    "USDJPY": SymbolSimConfig("USDJPY", 151.200, volatility_pips=9.0, base_spread_pips=0.6),
    "XAUUSD": SymbolSimConfig("XAUUSD", 2350.00, volatility_pips=150.0, base_spread_pips=2.5),
}


class MarketDataFeed(Protocol):
    source: TickSource

    async def stream(self, symbol: str) -> AsyncIterator[Tick]: ...

    async def latest(self, symbol: str) -> Tick | None: ...

    async def stop(self) -> None: ...


class ReplayMarketData:
    """Replays a fixed tick list, then optionally idles.

    Used by tests and deterministic forward-tests: the bot's real streaming loop
    consumes these ticks, so the code path under test is the production one.
    """

    source = TickSource.SIMULATOR

    def __init__(self, ticks: list[Tick], *, loop: bool = False) -> None:
        self._ticks = ticks
        self._loop = loop
        self._latest: dict[str, Tick] = {}
        self._stop = asyncio.Event()

    async def latest(self, symbol: str) -> Tick | None:
        return self._latest.get(symbol.upper())

    async def stop(self) -> None:
        self._stop.set()

    async def stream(self, symbol: str) -> AsyncIterator[Tick]:
        seq = self._ticks
        while True:
            for tick in seq:
                if self._stop.is_set():
                    return
                self._latest[tick.symbol] = tick
                yield tick
                await asyncio.sleep(0)
            if not self._loop:
                return
            await asyncio.sleep(0.05)


class SimulatedMarketData:
    """Geometric-random-walk quote generator with a mean-reverting component.

    Deterministic given a seed, which keeps tests reproducible while still
    producing realistic-looking spread/noise dynamics for the strategy.
    """

    source = TickSource.SIMULATOR

    def __init__(
        self,
        configs: dict[str, SymbolSimConfig] | None = None,
        *,
        seed: int | None = 11,
        interval_seconds: float = 1.0,
    ) -> None:
        self.configs = configs or DEFAULT_SIM_CONFIG
        self._rng = random.Random(seed)
        self._prices = {s: c.base_price for s, c in self.configs.items()}
        self._latest: dict[str, Tick] = {}
        self._interval = interval_seconds
        self._stop = asyncio.Event()

    async def latest(self, symbol: str) -> Tick | None:
        return self._latest.get(symbol.upper())

    async def stop(self) -> None:
        self._stop.set()

    def _next_tick(self, symbol: str) -> Tick:
        cfg = self.configs[symbol.upper()]
        spec = get_spec(symbol)
        price = self._prices[symbol.upper()]

        vol = spec.pips_to_price(cfg.volatility_pips)
        shock = self._rng.gauss(0, vol)
        # Mean reversion pulls price back toward the base so a long run drifts
        # nowhere, which keeps HFT strategies from grinding one direction.
        pull = (cfg.base_price - price) * cfg.mean_reversion
        drift = spec.pips_to_price(cfg.drift_pips_per_tick)
        price = price + shock + pull + drift
        self._prices[symbol.upper()] = price

        spread_pips = max(
            0.1, cfg.base_spread_pips + self._rng.gauss(0, cfg.spread_jitter_pips)
        )
        half = spec.pips_to_price(spread_pips / 2.0)
        digits = spec.digits
        tick = Tick(
            symbol=symbol.upper(),
            bid=round_price(price - half, digits),
            ask=round_price(price + half, digits),
            timestamp=utcnow(),
            volume=self._rng.uniform(20, 900),
        )
        self._latest[symbol.upper()] = tick
        return tick

    async def stream(self, symbol: str) -> AsyncIterator[Tick]:
        symbol = symbol.upper()
        while not self._stop.is_set():
            if symbol not in self.configs:
                self.configs[symbol] = SymbolSimConfig(symbol, 1.0)
            yield self._next_tick(symbol)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue


class BrokerMarketData:
    """Forwards real quotes from a live broker adapter."""

    source = TickSource.BROKER

    def __init__(self, broker, poll_interval: float = 1.0) -> None:
        self._broker = broker
        self._interval = poll_interval
        self._latest: dict[str, Tick] = {}
        self._stop = asyncio.Event()

    async def latest(self, symbol: str) -> Tick | None:
        return self._latest.get(symbol.upper())

    async def stop(self) -> None:
        self._stop.set()

    async def stream(self, symbol: str) -> AsyncIterator[Tick]:
        symbol = symbol.upper()
        while not self._stop.is_set():
            try:
                tick = await self._broker.get_tick(symbol)
                self._latest[symbol] = tick
                yield tick
            except Exception:
                # A feed error must not kill the engine; the RiskEngine's
                # stale-data check will halt trading on its own (rule 11).
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue


def build_feed(settings, broker=None) -> MarketDataFeed:
    if settings.mode.is_real and broker is not None and getattr(broker, "supports_live", False):
        return BrokerMarketData(broker, poll_interval=settings.engine_tick_seconds)
    return SimulatedMarketData(interval_seconds=settings.engine_tick_seconds)
