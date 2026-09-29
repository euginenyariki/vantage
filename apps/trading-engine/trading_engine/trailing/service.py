"""TrailingService — advanced trailing logic (the "Video 2" feature).

Design rules that are not negotiable:

1. A stop loss only ever moves in the PROTECTIVE direction. For a long that
   means up, for a short that means down. We assert this, not assume it.
2. A trailing stop never moves above/below current market price in a way that
   would trigger an immediate fill, and never tightens past the broker's
   minimum stop distance.
3. Break-even is applied first once the trigger level is reached, and only
   counts as "done" once it has actually moved the stop.
4. Every decision returns the new stop, the reason, and whether it changed —
   so the UI can show a trailing audit trail instead of a mystery number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from trading_engine.core.types import (
    Position,
    Side,
    SymbolSpec,
    Tick,
    TrailingMode,
    get_spec,
    round_price,
    utcnow,
)


class TrailingAction(str, Enum):
    NONE = "NONE"
    INITIALISED = "INITIALISED"
    MOVED = "MOVED"
    BREAK_EVEN_APPLIED = "BREAK_EVEN_APPLIED"
    BREACHED = "BREACHED"
    SKIPPED_MIN_DISTANCE = "SKIPPED_MIN_DISTANCE"
    SKIPPED_NOT_PROFITABLE = "SKIPPED_NOT_PROFITABLE"


@dataclass
class TrailingResult:
    action: TrailingAction
    new_stop: float | None
    previous_stop: float | None
    reason: str
    distance_pips: float = 0.0
    break_even_done: bool = False

    @property
    def changed(self) -> bool:
        return self.new_stop is not None and self.new_stop != self.previous_stop


@dataclass
class TrailingConfig:
    mode: TrailingMode = TrailingMode.FIXED
    distance_pips: float = 10.0          # FIXED / STEP / BREAK_EVEN / PERCENTAGE basis
    step_pips: float = 2.0               # STEP: minimum improvement to bother
    break_even_trigger_pips: float = 5.0
    break_even_offset_pips: float = 1.0  # lock in a little profit, not just flat
    atr_period: int = 14
    atr_multiplier: float = 2.0
    percentage: float = 0.5              # PERCENTAGE: percent of current price
    activation_pips: float = 0.0         # profit required before trailing arms


class TrailingService:
    def __init__(self, config: TrailingConfig | None = None) -> None:
        self.config = config or TrailingConfig()

    # ------------------------------------------------------------------ #
    def initialise(self, position: Position, spec: SymbolSpec) -> TrailingResult:
        """Arm the trailing config onto a freshly opened position."""
        self.config.mode = self.config.mode
        return TrailingResult(
            action=TrailingAction.INITIALISED,
            new_stop=position.stop_loss,
            previous_stop=position.stop_loss,
            reason=f"Trailing armed in {self.config.mode.value} mode",
        )

    # ------------------------------------------------------------------ #
    def update(
        self,
        position: Position,
        tick: Tick,
        spec: SymbolSpec | None = None,
        atr: float | None = None,
        now: datetime | None = None,
    ) -> TrailingResult:
        cfg = self.config
        spec = spec or get_spec(position.symbol)
        now = now or utcnow()
        prev = position.stop_loss

        # A BREAK_EVEN-only configuration still manages the stop (it arms
        # break-even and then holds). Every other mode is expected to trail, so
        # an unrecognised/disabled mode must leave the stop untouched rather
        # than quietly managing the position against the operator's intent.
        if cfg.mode is not TrailingMode.BREAK_EVEN and cfg.distance_pips <= 0 \
                and cfg.percentage <= 0 and cfg.atr_multiplier <= 0:
            return TrailingResult(
                action=TrailingAction.NONE,
                new_stop=prev, previous_stop=prev,
                reason="Trailing disabled (no distance, percentage or ATR basis)",
            )

        current = tick.price_for(position.side)
        profit_distance = (current - position.open_price) * position.side.sign
        if profit_distance <= 0:
            return TrailingResult(
                action=TrailingAction.SKIPPED_NOT_PROFITABLE,
                new_stop=prev, previous_stop=prev,
                reason="Position is not in profit; trailing not applied",
            )

        profit_pips = spec.price_to_pips(profit_distance)
        if profit_pips < cfg.activation_pips:
            return TrailingResult(
                action=TrailingAction.SKIPPED_NOT_PROFITABLE,
                new_stop=prev, previous_stop=prev,
                reason=f"Profit {profit_pips:.1f} pips below activation {cfg.activation_pips:.1f}",
            )

        # --- Step 1: break-even (highest priority, runs before other modes) ---
        if (
            not position.break_even_done
            and cfg.break_even_trigger_pips > 0
            and profit_pips >= cfg.break_even_trigger_pips
        ):
            be_stop = self._break_even_stop(position, spec, current)
            if be_stop is not None and self._is_protective(position.side, prev, be_stop):
                if self._clears_min_distance(current, be_stop, spec, tick):
                    position.break_even_done = True
                    return TrailingResult(
                        action=TrailingAction.BREAK_EVEN_APPLIED,
                        new_stop=be_stop, previous_stop=prev,
                        reason=(
                            f"Break-even armed at {profit_pips:.1f} pips profit; "
                            f"stop moved to {be_stop} (+{cfg.break_even_offset_pips:.1f} pips lock-in)"
                        ),
                        distance_pips=spec.price_to_pips(
                            abs(be_stop - (position.open_price if prev is None else prev))
                        ),
                        break_even_done=True,
                    )

        # --- Step 2: mode-specific candidate ---
        candidate = self._candidate_stop(position, cfg, spec, current, atr)

        if candidate is None:
            return TrailingResult(
                action=TrailingAction.NONE, new_stop=prev, previous_stop=prev,
                reason="No trailing candidate for this mode",
            )

        if not self._is_protective(position.side, prev, candidate):
            return TrailingResult(
                action=TrailingAction.NONE, new_stop=prev, previous_stop=prev,
                reason=(
                    f"Candidate {candidate} would loosen the stop (current {prev}); ignored"
                ),
            )

        if candidate == prev:
            return TrailingResult(
                action=TrailingAction.NONE, new_stop=prev, previous_stop=prev,
                reason="Stop already optimal",
            )

        if cfg.mode is TrailingMode.STEP and prev is not None:
            improvement = abs(candidate - prev)
            if improvement < cfg.step_pips * spec.pip_size - 1e-12:
                return TrailingResult(
                    action=TrailingAction.NONE, new_stop=prev, previous_stop=prev,
                    reason=(
                        f"Improvement {spec.price_to_pips(improvement):.1f} pips below "
                        f"step {cfg.step_pips:.1f} pips"
                    ),
                )

        if not self._clears_min_distance(current, candidate, spec, tick):
            return TrailingResult(
                action=TrailingAction.SKIPPED_MIN_DISTANCE, new_stop=prev, previous_stop=prev,
                reason=(
                    f"Stop {candidate} is within {spec.min_stop_distance} of market "
                    f"price; broker would reject"
                ),
            )

        moved = round_price(candidate, spec.digits)
        return TrailingResult(
            action=TrailingAction.MOVED, new_stop=moved, previous_stop=prev,
            reason=f"{cfg.mode.value} trailing moved stop to {moved}",
            distance_pips=spec.price_to_pips(
                abs(moved - (position.open_price if prev is None else prev))
            ),
            break_even_done=position.break_even_done,
        )

    # ------------------------------------------------------------------ #
    def _candidate_stop(
        self,
        position: Position,
        cfg: TrailingConfig,
        spec: SymbolSpec,
        current: float,
        atr: float | None,
    ) -> float | None:
        sign = position.side.sign

        if cfg.mode is TrailingMode.FIXED:
            return current - sign * spec.pips_to_price(cfg.distance_pips)

        if cfg.mode is TrailingMode.STEP:
            base = position.stop_loss
            if base is None:
                return current - sign * spec.pips_to_price(cfg.distance_pips)
            step = spec.pips_to_price(max(cfg.step_pips, cfg.distance_pips))
            return current - sign * step if (current - sign * step) != base else base

        if cfg.mode is TrailingMode.BREAK_EVEN:
            be = self._break_even_stop(position, spec, current)
            if be is not None:
                return be
            return current - sign * spec.pips_to_price(cfg.distance_pips)

        if cfg.mode is TrailingMode.PERCENTAGE:
            pct = max(cfg.percentage, 0.01) / 100.0
            return current * (1.0 - sign * pct)

        if cfg.mode is TrailingMode.ATR:
            a = atr if atr is not None else position.meta.get("atr")
            if not a or a <= 0:
                return None
            return current - sign * (a * cfg.atr_multiplier)

        return None

    def _break_even_stop(
        self, position: Position, spec: SymbolSpec, current: float
    ) -> float | None:
        cfg = self.config
        entry = position.open_price
        if cfg.break_even_offset_pips <= 0:
            # Exactly break-even: stop at entry plus spread cover for a long.
            return round_price(entry, spec.digits)
        offset = spec.pips_to_price(cfg.break_even_offset_pips)
        return round_price(entry + position.side.sign * offset, spec.digits)

    @staticmethod
    def _is_protective(side: Side, previous: float | None, candidate: float | None) -> bool:
        if candidate is None:
            return False
        if previous is None:
            return True
        if side is Side.BUY:
            return candidate > previous
        return candidate < previous

    def _clears_min_distance(
        self, current: float, candidate: float, spec: SymbolSpec, tick: Tick
    ) -> bool:
        min_dist = max(spec.min_stop_distance, tick.spread * 1.5)
        return abs(current - candidate) >= min_dist - 1e-12
