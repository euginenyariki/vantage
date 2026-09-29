"""Tick -> OHLC bar aggregation.

A strategy evaluates on closed bars, never on raw ticks. This folds a tick
stream into timeframe buckets so the indicator window reflects real bar
structure instead of one synthetic bar per tick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_engine.brokers.base import Bar
from trading_engine.core.types import SymbolSpec, round_price

# Minutes per supported timeframe.
TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M6": 6, "M10": 10, "M12": 12,
    "M15": 15, "M20": 20, "M30": 30,
    "H1": 60, "H2": 120, "H3": 180, "H4": 240, "H6": 360, "H8": 480, "H12": 720,
    "D1": 1440, "W1": 10080,
}


def timeframe_minutes(timeframe: str) -> int:
    try:
        return TIMEFRAME_MINUTES[timeframe.upper()]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}; expected one of "
            f"{sorted(TIMEFRAME_MINUTES)}"
        ) from exc


def bucket_start(ts: datetime, timeframe: str) -> datetime:
    """Floor a timestamp to the start of its bar bucket.

    Daily and weekly need calendar alignment (UTC), not a fixed minute offset.
    """
    ts = ts.astimezone(UTC)
    minutes = timeframe_minutes(timeframe)
    if minutes >= 1440:
        day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        if minutes == 1440:
            return day
        # Weekly: align to Monday.
        return day - timedelta(days=day.weekday())
    return ts.replace(second=0, microsecond=0) - timedelta(
        minutes=ts.minute % minutes
    )


class BarAggregator:
    """Folds ticks into closed bars for one symbol/timeframe.

    `feed()` returns the bar only when it closes, so strategies never evaluate
    on a partially formed candle. The in-progress bar stays internal and is
    updated in place.
    """

    def __init__(self, symbol: str, timeframe: str, spec: SymbolSpec) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.spec = spec
        self._bucket: datetime | None = None
        self._open = 0.0
        self._high = 0.0
        self._low = 0.0
        self._close = 0.0
        self._volume = 0.0

    @property
    def current_bucket(self) -> datetime | None:
        return self._bucket

    def feed(self, bid: float, ask: float, ts: datetime, volume: float = 0.0) -> Bar | None:
        """Add a tick. Returns the closed bar when the bucket rolls over."""
        mid = (bid + ask) / 2
        bucket = bucket_start(ts, self.timeframe)

        if self._bucket is None:
            self._begin(bucket, mid)
            return None
        if bucket > self._bucket:
            closed = self._close_out()
            self._begin(bucket, mid)
            return closed
        if bucket < self._bucket:
            # Out-of-order tick for an already closed window: ignore it rather
            # than corrupting the forming bar.
            return None

        self._high = max(self._high, bid, ask)
        self._low = min(self._low, bid, ask)
        self._close = mid
        self._volume += volume
        return None

    def flush(self) -> Bar | None:
        """Close the in-progress bar (used on shutdown or timeframe change)."""
        if self._bucket is None:
            return None
        return self._close_out()

    def _begin(self, bucket: datetime, mid: float) -> None:
        self._bucket = bucket
        self._open = self._high = self._low = self._close = mid
        self._volume = 0.0

    def _close_out(self) -> Bar:
        bar = Bar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open=round_price(self._open, self.spec.digits),
            high=round_price(self._high, self.spec.digits),
            low=round_price(self._low, self.spec.digits),
            close=round_price(self._close, self.spec.digits),
            volume=self._volume,
            timestamp=self._bucket,
        )
        self._bucket = None
        return bar
