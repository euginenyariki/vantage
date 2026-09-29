"""Strategy base class and indicator math.

Indicators are implemented in plain Python over plain lists so the exact same
code path runs in paper mode, in backtests and in live mode — no dependency on
a broker's indicator library, no divergence between what you backtested and
what you trade.
"""

from __future__ import annotations

import abc
import math
from collections.abc import Sequence
from dataclasses import dataclass

from trading_engine.brokers.base import Bar
from trading_engine.core.types import (
    SignalAction,
    StrategySignal,
    SymbolSpec,
    Tick,
    round_price,
)


# --------------------------------------------------------------------------- #
# Indicators
# --------------------------------------------------------------------------- #
def ema(values: Sequence[float], period: int) -> list[float]:
    if period <= 0 or len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[float]:
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)))
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    if not highs:
        return []
    out = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        prev_close = closes[i - 1]
        out.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            )
        )
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[float]:
    tr = true_range(highs, lows, closes)
    if len(tr) < period:
        return []
    value = sum(tr[:period]) / period
    out = [value]
    for i in range(period, len(tr)):
        value = (value * (period - 1) + tr[i]) / period
        out.append(value)
    return out


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> tuple[list[float], list[float], list[float]]:
    if len(closes) < period:
        return [], [], []
    mid = ema(closes, period)
    if not mid:
        return [], [], []
    upper, lower = [], []
    # mid[i] is the EMA of the window ending at closes[i + period - 1], so the
    # matching raw window for band i is closes[i : i + period].
    for i in range(len(mid)):
        window = closes[i: i + period]
        if len(window) < period:
            break
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        sd = math.sqrt(var)
        upper.append(mean + mult * sd)
        lower.append(mean - mult * sd)
    return upper, mid, lower


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]]:
    """Return (macd_line, signal_line, hist).

    The three series are warm-up aligned to their own most recent value, so they
    are NOT the same length and must not be zipped together positionally. Read
    the current value from the end of each list, or index hist against
    signal_line, which is the only pair that shares an alignment.
    """
    fast_e, slow_e = ema(closes, fast), ema(closes, slow)
    if not fast_e or not slow_e:
        return [], [], []
    offset = len(slow_e) - len(fast_e)
    macd_line = [fast_e[i + offset] - slow_e[i] for i in range(len(slow_e))]
    signal_line = ema(macd_line, signal)
    if not signal_line:
        return macd_line, [], []
    hist_offset = len(macd_line) - len(signal_line)
    hist = [macd_line[i + hist_offset] - signal_line[i] for i in range(len(signal_line))]
    return macd_line, signal_line, hist


# --------------------------------------------------------------------------- #
# Strategy contract
# --------------------------------------------------------------------------- #
@dataclass
class StrategyContext:
    symbol: str
    spec: SymbolSpec
    bars: list[Bar]
    tick: Tick
    equity: float
    open_positions: int
    last_price: float | None = None

    @property
    def closes(self) -> list[float]:
        return [b.close for b in self.bars]

    @property
    def highs(self) -> list[float]:
        return [b.high for b in self.bars]

    @property
    def lows(self) -> list[float]:
        return [b.low for b in self.bars]


class Strategy(abc.ABC):
    """Every strategy returns a signal. A signal NEVER executes directly —
    the execution engine routes it through the RiskEngine first (rule 5)."""

    name: str = "abstract"
    timeframe: str = "M1"
    min_bars: int = 60

    @abc.abstractmethod
    def evaluate(self, ctx: StrategyContext) -> StrategySignal: ...

    def hold(self, ctx: StrategyContext, reason: str = "") -> StrategySignal:
        return StrategySignal(
            action=SignalAction.HOLD,
            symbol=ctx.symbol,
            price=ctx.tick.mid,
            confidence=0.0,
            reason=reason or "No setup",
        )


class StrategyRegistry:
    _registry: dict[str, type[Strategy]] = {}

    @classmethod
    def register(cls, strategy_cls: type[Strategy]) -> type[Strategy]:
        cls._registry[strategy_cls.name] = strategy_cls
        return strategy_cls

    @classmethod
    def get(cls, name: str) -> type[Strategy] | None:
        return cls._registry.get(name)

    @classmethod
    def all(cls) -> dict[str, type[Strategy]]:
        return dict(cls._registry)


@StrategyRegistry.register
class HFTScalper(Strategy):
    """High-frequency scalping strategy (the video's core idea).

    Confluence of four filters, each of which must agree:
      1. Trend: fast EMA above slow EMA (long) / below (short)
      2. Momentum: MACD histogram agrees with the trend direction
      3. Mean reversion guard: price not already over-extended from the EMA
      4. Volatility: ATR above a floor (a dead market cannot be scalped)

    Exits are handled by the TrailingService rather than by fixed targets, so
    the strategy's only job is to decide entry and initial protective levels.
    """

    name = "hft_scalper"
    timeframe = "M1"
    min_bars = 60

    def __init__(
        self,
        *,
        ema_fast: int = 9,
        ema_slow: int = 21,
        rsi_period: int = 14,
        rsi_oversold: float = 32.0,
        rsi_overbought: float = 68.0,
        atr_period: int = 14,
        min_atr_pips: float = 3.0,
        max_atr_pips: float = 120.0,
        stop_atr_mult: float = 1.2,
        confidence_threshold: float = 0.55,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.atr_period = atr_period
        self.min_atr_pips = min_atr_pips
        self.max_atr_pips = max_atr_pips
        self.stop_atr_mult = stop_atr_mult
        self.confidence_threshold = confidence_threshold

    def evaluate(self, ctx: StrategyContext) -> StrategySignal:
        if len(ctx.bars) < self.min_bars:
            return self.hold(ctx, f"Need {self.min_bars} bars, have {len(ctx.bars)}")

        spec = ctx.spec
        closes = ctx.closes
        price = ctx.tick.mid

        fast = ema(closes, self.ema_fast)
        slow = ema(closes, self.ema_slow)
        rsi_vals = rsi(closes, self.rsi_period)
        atr_vals = atr(ctx.highs, ctx.lows, closes, self.atr_period)
        macd_line, macd_sig, macd_hist = macd(closes)

        if not fast or not slow or not rsi_vals or not atr_vals:
            return self.hold(ctx, "Indicators not warmed up")

        ema_f, ema_s = fast[-1], slow[-1]
        rsi_v = rsi_vals[-1]
        atr_v = atr_vals[-1]
        atr_pips = spec.price_to_pips(atr_v)
        hist = macd_hist[-1] if macd_hist else 0.0

        meta = {
            "ema_fast": round_price(ema_f, spec.digits),
            "ema_slow": round_price(ema_s, spec.digits),
            "rsi": round_price(rsi_v, 2),
            "atr": round_price(atr_v, spec.digits),
            "atr_pips": round_price(atr_pips, 2),
            "macd_hist": round_price(hist, spec.digits),
        }

        # Volatility gate — a scalper needs movement, and a dead market is
        # where a tight stop gets hunted.
        if atr_pips < self.min_atr_pips:
            return self.hold(ctx, f"ATR {atr_pips:.1f} pips below floor {self.min_atr_pips:.1f}", )
        if atr_pips > self.max_atr_pips:
            return self.hold(ctx, f"ATR {atr_pips:.1f} pips above ceiling {self.max_atr_pips:.1f}")

        # Trend + momentum alignment
        bullish = ema_f > ema_s and hist > 0
        bearish = ema_f < ema_s and hist < 0

        if not bullish and not bearish:
            return self.hold(ctx, f"No EMA/MACD confluence (hist {hist:+.6f})")

        side_sign = 1 if bullish else -1

        # RSI confirmation: we want to be early, not late. Buy pullbacks in an
        # uptrend rather than chasing an overbought spike.
        if bullish and rsi_v > self.rsi_overbought:
            return self.hold(ctx, f"RSI {rsi_v:.1f} overbought for a long entry")
        if bearish and rsi_v < self.rsi_oversold:
            return self.hold(ctx, f"RSI {rsi_v:.1f} oversold for a short entry")

        # Extension guard — don't enter when price is already stretched from the
        # fast EMA; that is where the trailing stop would get hit immediately.
        extension_pips = spec.price_to_pips(abs(price - ema_f))
        if extension_pips > atr_pips * 1.5:
            return self.hold(
                ctx,
                f"Price extended {extension_pips:.1f} pips from EMA — waiting for a pullback",
            )

        # Confidence: how strongly do the inputs agree?
        trend_gap = spec.price_to_pips(abs(ema_f - ema_s))
        trend_score = min(trend_gap / max(atr_pips, 1e-9), 1.0)
        rsi_score = (
            min(abs(rsi_v - 50.0) / 30.0, 1.0) if bullish else min(abs(50.0 - rsi_v) / 30.0, 1.0)
        )
        momentum_score = min(abs(hist) / max(atr_v * 0.15, 1e-12), 1.0)
        confidence = round_price(0.45 * trend_score + 0.3 * rsi_score + 0.25 * momentum_score, 3)

        if confidence < self.confidence_threshold:
            return self.hold(ctx, f"Confidence {confidence:.2f} below threshold", )

        stop_distance = max(atr_v * self.stop_atr_mult, spec.pips_to_price(3.0))
        stop_loss = round_price(price - side_sign * stop_distance, spec.digits)

        return StrategySignal(
            action=SignalAction.BUY if bullish else SignalAction.SELL,
            symbol=ctx.symbol,
            price=price,
            confidence=confidence,
            reason=(
                f"{'Bull' if bullish else 'Bear'} EMA{self.ema_fast}/{self.ema_slow} "
                f"confluence + MACD {hist:+.6f}, RSI {rsi_v:.1f}, ATR {atr_pips:.1f} pips"
            ),
            stop_loss=stop_loss,
            atr=atr_v,
            meta=meta,
        )


@StrategyRegistry.register
class MeanReversionScalper(Strategy):
    """Bollinger-band fade — the counter-strategy, useful for comparison."""

    name = "mean_reversion"
    timeframe = "M1"
    min_bars = 40

    def __init__(self, period: int = 20, mult: float = 2.0, stop_atr_mult: float = 1.5) -> None:
        self.period = period
        self.mult = mult
        self.stop_atr_mult = stop_atr_mult

    def evaluate(self, ctx: StrategyContext) -> StrategySignal:
        if len(ctx.bars) < self.min_bars:
            return self.hold(ctx, "Not enough bars")
        spec = ctx.spec
        closes = ctx.closes
        upper, mid, lower = bollinger(closes, self.period, self.mult)
        atr_vals = atr(ctx.highs, ctx.lows, closes, 14)
        if not upper or not lower or not atr_vals:
            return self.hold(ctx, "Indicators not warmed up")

        price = ctx.tick.mid
        upper_band, lower_band, atr_now = upper[-1], lower[-1], atr_vals[-1]
        atr_pips = spec.price_to_pips(atr_now)

        if price <= lower_band:
            side_sign, label = 1, "Buy lower band fade"
        elif price >= upper_band:
            side_sign, label = -1, "Sell upper band fade"
        else:
            return self.hold(
                ctx, f"Price {price} inside bands ({lower_band:.5f}-{upper_band:.5f})"
            )

        stop = round_price(
            price - side_sign * max(atr_now * self.stop_atr_mult, spec.pips_to_price(3.0)),
            spec.digits,
        )
        return StrategySignal(
            action=SignalAction.BUY if side_sign > 0 else SignalAction.SELL,
            symbol=ctx.symbol,
            price=price,
            confidence=0.6,
            reason=f"{label}; ATR {atr_pips:.1f} pips",
            stop_loss=stop,
            atr=atr_now,
            meta={
                "bb_upper": upper_band,
                "bb_lower": lower_band,
                "atr_pips": round_price(atr_pips, 2),
            },
        )
