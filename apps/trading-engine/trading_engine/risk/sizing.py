"""PositionSizer — turns a risk budget and a stop distance into lots."""

from __future__ import annotations

from dataclasses import dataclass

from trading_engine.core.types import (
    Side,
    SymbolSpec,
    get_spec,
    round_price,
    size_position,
)


@dataclass
class SizingRequest:
    symbol: str
    side: Side
    equity: float
    risk_percent: float
    stop_loss: float | None
    entry_price: float
    atr: float | None = None
    max_lots: float | None = None
    pip_stop_distance: float | None = None
    spec: SymbolSpec | None = None


@dataclass
class SizingResult:
    volume: float
    stop_loss: float | None
    take_profit: float | None
    risk_amount: float
    stop_distance_price: float
    stop_distance_pips: float
    risk_reward_ratio: float | None
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.skipped_reason is None and self.volume > 0


class PositionSizer:
    """Stateless. The same instance is used by paper, backtest and live paths."""

    def __init__(self, default_rr: float = 1.5) -> None:
        self.default_rr = default_rr

    def compute(self, req: SizingRequest) -> SizingResult:
        spec = req.spec or get_spec(req.symbol)

        stop = req.stop_loss
        if stop is None and req.pip_stop_distance is not None:
            distance = spec.pips_to_price(req.pip_stop_distance)
            stop = (
                req.entry_price - distance
                if req.side is Side.BUY
                else req.entry_price + distance
            )
            stop = round_price(stop, spec.digits)
        if stop is None and req.atr is not None and req.atr > 0:
            stop = round_price(
                req.entry_price - req.atr * 1.5 * (1 if req.side is Side.BUY else -1),
                spec.digits,
            )

        if stop is None:
            return SizingResult(
                volume=0.0, stop_loss=None, take_profit=None, risk_amount=0.0,
                stop_distance_price=0.0, stop_distance_pips=0.0,
                risk_reward_ratio=None, skipped_reason="No stop loss distance could be derived",
            )

        stop_distance = abs(req.entry_price - stop)
        if stop_distance <= 0:
            return SizingResult(
                volume=0.0, stop_loss=stop, take_profit=None, risk_amount=0.0,
                stop_distance_price=0.0, stop_distance_pips=0.0,
                risk_reward_ratio=None, skipped_reason="Stop loss equals entry price",
            )

        volume = size_position(
            equity=req.equity,
            risk_percent=req.risk_percent,
            stop_distance_price=stop_distance,
            spec=spec,
            max_volume=req.max_lots,
        )

        if volume <= 0:
            return SizingResult(
                volume=0.0, stop_loss=stop, take_profit=None, risk_amount=0.0,
                stop_distance_price=stop_distance,
                stop_distance_pips=spec.price_to_pips(stop_distance),
                risk_reward_ratio=None,
                skipped_reason=(
                    f"Risk {req.risk_percent:.2f}% of {req.equity:,.2f} is below the "
                    f"broker minimum volume ({spec.min_volume} lots) for this stop distance"
                ),
            )

        take_profit = self._target(req.entry_price, stop, req.side, spec)

        return SizingResult(
            volume=volume,
            stop_loss=stop,
            take_profit=take_profit,
            risk_amount=req.equity * (req.risk_percent / 100.0),
            stop_distance_price=stop_distance,
            stop_distance_pips=spec.price_to_pips(stop_distance),
            risk_reward_ratio=self.default_rr,
        )

    def _target(
        self, entry: float, stop: float, side: Side, spec: SymbolSpec
    ) -> float | None:
        if stop is None:
            return None
        risk = abs(entry - stop)
        if risk <= 0:
            return None
        tp = (
            entry + risk * self.default_rr
            if side is Side.BUY
            else entry - risk * self.default_rr
        )
        return round_price(tp, spec.digits)
