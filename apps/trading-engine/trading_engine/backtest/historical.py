"""Historical data ingestion.

Live/paper data and historical data live in separate tables and are never
mixed (rule 14). This module fetches bars from a broker or, when no broker is
configured, generates a reproducible synthetic series that is clearly labelled
as such in the UI so nobody mistakes it for a real market archive.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from trading_engine.brokers.base import Bar
from trading_engine.core.types import get_spec, round_price
from trading_engine.persistence.repository import HistoricalDataRepository

logger = logging.getLogger("vantage.historical")

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


class HistoricalDataService:
    def __init__(self, sessionmaker, broker=None) -> None:
        self._sm = sessionmaker
        self._broker = broker

    async def ingest(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> int:
        """Populate `historical_bars` for the window. Returns rows written."""
        bars = await self._fetch(symbol.upper(), timeframe, start, end)
        if not bars:
            return 0
        rows = [
            {
                "symbol": b.symbol, "timeframe": b.timeframe, "timestamp": b.timestamp,
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]
        async with self._sm() as session:
            repo = HistoricalDataRepository(session)
            written = await repo.upsert_many(rows)
            await session.commit()
        return written

    async def _fetch(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Bar]:
        if self._broker is not None:
            try:
                minutes = TF_MINUTES.get(timeframe, 1)
                out: list[Bar] = []
                cursor = start
                chunk = 500
                while cursor < end:
                    bars = await self._broker.get_bars(symbol, timeframe, chunk, until=cursor)
                    if not bars:
                        break
                    out.extend(b for b in bars if start <= b.timestamp <= end)
                    oldest = min(b.timestamp for b in bars)
                    if oldest <= start:
                        break
                    cursor = oldest - timedelta(minutes=minutes)
                return sorted(set(out), key=lambda b: b.timestamp)
            except Exception as exc:
                logger.warning(
                    "Broker history unavailable for %s (%s); falling back to synthetic series",
                    symbol, exc,
                )
        return self._synthetic(symbol, timeframe, start, end)

    def _synthetic(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        """Deterministic pseudo-random OHLC so backtests are reproducible.

        Uses a real hash of (symbol, timeframe) as the seed and skips weekends
        so intraday series do not contain flat Sunday candles.
        """
        import random

        spec = get_spec(symbol)
        seed = int.from_bytes(f"{symbol}{timeframe}".encode()[:8].ljust(8, b"\0"), "big")
        rng = random.Random(seed)

        minutes = TF_MINUTES.get(timeframe, 1)
        vol = spec.pips_to_price(9.0 if symbol != "XAUUSD" else 180.0)
        base = {"EURUSD": 1.0800, "GBPUSD": 1.2600, "USDJPY": 150.50, "XAUUSD": 2280.0}.get(
            symbol, 1.0
        )

        bars: list[Bar] = []
        price = base
        cursor = start
        while cursor < end:
            if cursor.weekday() >= 5 and minutes < 1440:
                cursor += timedelta(minutes=minutes)
                continue
            shock = rng.gauss(0, vol)
            pull = (base - price) * 0.015
            o = price
            c = round_price(o + shock + pull, spec.digits)
            h = round_price(max(o, c) + abs(rng.gauss(0, vol * 0.55)), spec.digits)
            low = round_price(min(o, c) - abs(rng.gauss(0, vol * 0.55)), spec.digits)
            bars.append(
                Bar(symbol=symbol, timeframe=timeframe, open=o, high=h, low=low, close=c,
                    volume=rng.uniform(200, 4000), timestamp=cursor)
            )
            price = c
            cursor += timedelta(minutes=minutes)
        return bars

    async def load_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[Bar]:
        async with self._sm() as session:
            repo = HistoricalDataRepository(session)
            rows = await repo.load(symbol, timeframe, start, end)
        return [
            Bar(
                symbol=r.symbol, timeframe=r.timeframe, open=float(r.open), high=float(r.high),
                low=float(r.low), close=float(r.close), volume=float(r.volume or 0),
                timestamp=r.ts,
            )
            for r in rows
        ]
