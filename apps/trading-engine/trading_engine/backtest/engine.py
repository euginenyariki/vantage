"""BacktestEngine.

Runs as a queued background job, never inside an HTTP request (Phase 4).

The critical design point: this engine imports and calls the SAME
RiskEngine, PositionSizer, TrailingService and strategy classes that paper/live
use. A backtest that diverges from live behaviour is worse than no backtest.

Historical data lives in its own tables (HistoricalBar) and is never mixed with
operational data. Every result is labelled HISTORICAL SIMULATION.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from trading_engine.brokers.base import Bar
from trading_engine.core.types import (
    Position,
    Side,
    SignalAction,
    SymbolSpec,
    Tick,
    get_spec,
    max_drawdown,
    position_pnl,
    profit_factor,
    round_price,
    utcnow,
)
from trading_engine.risk.engine import RiskConfig, RiskContext, RiskEngine
from trading_engine.risk.sizing import PositionSizer, SizingRequest
from trading_engine.strategies.base import Strategy, StrategyContext, StrategyRegistry
from trading_engine.trailing.service import TrailingConfig, TrailingService

logger = logging.getLogger("vantage.backtest")

RESULT_LABEL = "HISTORICAL SIMULATION"


@dataclass
class BacktestConfig:
    strategy_name: str
    symbol: str
    timeframe: str
    date_from: date
    date_to: date
    initial_capital: float = 10_000.0
    risk_percent: float = 1.0
    spread_pips: float = 0.6
    slippage_pips: float = 0.4
    commission_per_lot: float = 3.5
    max_concurrent_positions: int = 1
    risk: RiskConfig = field(default_factory=RiskConfig)
    trailing: TrailingConfig = field(default_factory=TrailingConfig)

    @property
    def spec(self) -> SymbolSpec:
        return get_spec(self.symbol)

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy_name, "symbol": self.symbol, "timeframe": self.timeframe,
            "date_from": self.date_from.isoformat(), "date_to": self.date_to.isoformat(),
            "initial_capital": self.initial_capital, "risk_percent": self.risk_percent,
            "spread_pips": self.spread_pips, "slippage_pips": self.slippage_pips,
            "commission_per_lot": self.commission_per_lot,
            "max_concurrent_positions": self.max_concurrent_positions,
            "trailing": {
                "mode": self.trailing.mode.value, "distance_pips": self.trailing.distance_pips,
                "step_pips": self.trailing.step_pips,
                "break_even_trigger_pips": self.trailing.break_even_trigger_pips,
                "break_even_offset_pips": self.trailing.break_even_offset_pips,
            },
        }


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    volume: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    pnl: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    result: str = "OPEN"
    r_multiple: float = 0.0
    close_reason: str = ""
    bars_held: int = 0
    max_favorable_pips: float = 0.0
    max_adverse_pips: float = 0.0

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "side": self.side, "volume": round(self.volume, 2),
            "entry_time": self.entry_time.isoformat(), "entry_price": self.entry_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_price": self.exit_price, "stop_loss": self.stop_loss,
            "take_profit": self.take_profit, "pnl": round(self.pnl, 2),
            "commission": round(self.commission, 2), "swap": round(self.swap, 2),
            "result": self.result, "r_multiple": round(self.r_multiple, 2),
            "close_reason": self.close_reason, "bars_held": self.bars_held,
            "max_favorable_pips": round(self.max_favorable_pips, 1),
            "max_adverse_pips": round(self.max_adverse_pips, 1),
        }


@dataclass
class BacktestResultData:
    config: dict
    final_equity: float
    net_pnl: float
    roi_percent: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown_percent: float
    max_drawdown_amount: float
    average_trade: float
    best_trade: float
    worst_trade: float
    avg_win: float
    avg_loss: float
    total_commission: float
    equity_curve: list[dict]
    drawdown_curve: list[dict]
    monthly_returns: list[dict]
    trades: list[dict]
    label: str = RESULT_LABEL
    computed_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict:
        return {
            "label": self.label, "config": self.config,
            "final_equity": round(self.final_equity, 2), "net_pnl": round(self.net_pnl, 2),
            "roi_percent": round(self.roi_percent, 4), "total_trades": self.total_trades,
            "winning_trades": self.winning_trades, "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 2),
            "profit_factor": (None if math.isinf(self.profit_factor) or math.isnan(self.profit_factor)
                              else round(self.profit_factor, 4)),
            "max_drawdown_percent": round(self.max_drawdown_percent, 4),
            "max_drawdown_amount": round(self.max_drawdown_amount, 2),
            "average_trade": round(self.average_trade, 2),
            "best_trade": round(self.best_trade, 2), "worst_trade": round(self.worst_trade, 2),
            "avg_win": round(self.avg_win, 2), "avg_loss": round(self.avg_loss, 2),
            "total_commission": round(self.total_commission, 2),
            "equity_curve": self.equity_curve, "drawdown_curve": self.drawdown_curve,
            "monthly_returns": self.monthly_returns, "trades": self.trades,
            "computed_at": self.computed_at.isoformat(),
        }


class BacktestEngine:
    """Bar-by-bar simulation over historical OHLC.

    Intra-bar stop/target handling is deliberately conservative: when a single
    bar's range contains BOTH the stop and the target, the STOP is assumed to
    have been hit first. Optimistic fill assumptions are how backtests start
    lying to people.
    """

    def __init__(
        self,
        config: BacktestConfig,
        *,
        progress_cb: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self.progress_cb = progress_cb
        self.strategy_cls = StrategyRegistry.get(config.strategy_name)
        if self.strategy_cls is None:
            raise ValueError(
                f"Unknown strategy {config.strategy_name!r}. "
                f"Available: {sorted(StrategyRegistry.all())}"
            )
        self.strategy: Strategy = self.strategy_cls()
        self.sizer = PositionSizer()
        self.trailing = TrailingService(config.trailing)
        self.risk = RiskEngine(config.risk)
        self.spec = config.spec

    # ------------------------------------------------------------------ #
    def run(self, bars: Sequence[Bar]) -> BacktestResultData:
        if len(bars) < max(self.strategy.min_bars, 30):
            raise ValueError(
                f"Not enough bars ({len(bars)}). Need at least "
                f"{max(self.strategy.min_bars, 30)} for {self.strategy.name}."
            )

        cfg = self.config
        cash = cfg.initial_capital
        positions: list[Position] = []
        trades: list[BacktestTrade] = []
        equity_curve: list[dict] = []
        drawdown_curve: list[dict] = []
        peak = cash
        total_commission = 0.0
        warmup = self.strategy.min_bars
        window: list[Bar] = list(bars[:warmup])

        for i, bar in enumerate(bars):
            ts = bar.timestamp

            # --- 1. Manage open positions against this bar --------------- #
            still_open: list[Position] = []
            for pos in positions:
                outcome = self._process_position(pos, bar, cfg)
                if outcome is None:
                    still_open.append(pos)
                    continue
                exit_price, reason, stop_price, tp_price = outcome
                closed = self._close(cash, pos, exit_price, reason, ts, stop_price, tp_price, i)
                cash += closed.pnl
                total_commission += closed.commission
                trades.append(closed)
            positions = still_open

            equity = cash + sum(
                position_pnl(p.side, p.volume, p.open_price, bar.close, self.spec,
                             commission=p.commission, swap=p.swap)
                for p in positions
            )
            equity_curve.append({"t": ts.isoformat(), "equity": round(equity, 2)})
            peak = max(peak, equity)
            dd_pct = ((peak - equity) / peak * 100.0) if peak > 0 else 0.0
            drawdown_curve.append({"t": ts.isoformat(), "drawdown_pct": round(dd_pct, 4)})

            if i < warmup:
                continue

            window.append(bar)
            if len(window) > warmup + 60:
                window.pop(0)

            # --- 2. Entries ---------------------------------------------- #
            if len(positions) >= cfg.max_concurrent_positions:
                continue

            tick = self._bar_tick(bar, cfg.spread_pips, cfg.slippage_pips)
            strategy_ctx = StrategyContext(
                symbol=cfg.symbol, spec=self.spec, bars=list(window), tick=tick,
                equity=equity, open_positions=len(positions),
            )
            signal = self.strategy.evaluate(strategy_ctx)
            if signal.action is SignalAction.HOLD:
                continue

            side = Side.BUY if signal.action is SignalAction.BUY else Side.SELL
            ctx = RiskContext(
                equity=equity, balance=cash, daily_pnl=0.0, open_positions=positions,
                now=ts, broker_connected=True, live_enabled=True,
            )
            decision = self.risk.evaluate(
                symbol=cfg.symbol, side=side, volume=self.spec.min_volume,
                tick=tick, spec=self.spec, ctx=ctx,
            )
            if decision.blocked:
                continue

            sizing = self.sizer.compute(
                SizingRequest(
                    symbol=cfg.symbol, side=side, equity=equity,
                    risk_percent=cfg.risk_percent, stop_loss=signal.stop_loss,
                    entry_price=tick.price_for(side), atr=signal.atr, spec=self.spec,
                )
            )
            if not sizing.ok or sizing.stop_loss is None:
                continue

            fill = tick.price_for(side) + (
                self.spec.pips_to_price(cfg.slippage_pips)
                * (1 if side is Side.BUY else -1)
            )
            fill = round_price(fill, self.spec.digits)
            commission = self.spec.commission_per_lot * sizing.volume

            positions.append(
                Position(
                    id=f"bt-{i}", symbol=cfg.symbol, side=side, volume=sizing.volume,
                    open_price=fill, current_price=fill, stop_loss=sizing.stop_loss,
                    take_profit=sizing.take_profit, commission=commission,
                    initial_stop_distance_pips=sizing.stop_distance_pips,
                    trailing_mode=cfg.trailing.mode,
                    trailing_distance_pips=cfg.trailing.distance_pips,
                    trailing_step_pips=cfg.trailing.step_pips,
                    break_even_trigger_pips=cfg.trailing.break_even_trigger_pips,
                    break_even_offset_pips=cfg.trailing.break_even_offset_pips,
                    meta={"atr": signal.atr},
                    opened_at=ts,
                )
            )

            if self.progress_cb and i % 500 == 0:
                self.progress_cb(min(99.0, 100.0 * i / len(bars)))

        # --- 3. Close anything still open at the final price -------------- #
        final_bar = bars[-1]
        for pos in positions:
            closed = self._close(
                cash, pos, final_bar.close, "end_of_backtest", final_bar.timestamp,
                pos.stop_loss, pos.take_profit, len(bars),
            )
            cash += closed.pnl
            total_commission += closed.commission
            trades.append(closed)

        final_equity = cash
        values = [e["equity"] for e in equity_curve]
        dd_frac, dd_amount = max_drawdown(values)

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        pf = profit_factor(gross_profit, gross_loss)

        return BacktestResultData(
            config=cfg.as_dict(),
            final_equity=final_equity,
            net_pnl=final_equity - cfg.initial_capital,
            roi_percent=((final_equity - cfg.initial_capital) / cfg.initial_capital * 100.0)
            if cfg.initial_capital > 0 else 0.0,
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=(len(wins) / len(trades) * 100.0) if trades else 0.0,
            profit_factor=pf,
            max_drawdown_percent=dd_frac * 100.0,
            max_drawdown_amount=dd_amount,
            average_trade=(sum(t.pnl for t in trades) / len(trades)) if trades else 0.0,
            best_trade=max((t.pnl for t in trades), default=0.0),
            worst_trade=min((t.pnl for t in trades), default=0.0),
            avg_win=(sum(t.pnl for t in wins) / len(wins)) if wins else 0.0,
            avg_loss=(sum(t.pnl for t in losses) / len(losses)) if losses else 0.0,
            total_commission=total_commission,
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            monthly_returns=self._monthly(trades, cfg.initial_capital),
            trades=[t.as_dict() for t in trades],
        )

    # ------------------------------------------------------------------ #
    def _process_position(self, pos: Position, bar: Bar, cfg: BacktestConfig):
        """Returns None to keep the position open, else an exit tuple."""
        spec = self.spec

        # Trailing first, using this bar's high/low as the "current price".
        # Probe in the pessimistic order: a long most likely visits its low
        # before its high (and vice versa for a short), so updating the stop
        # from the favourable extreme first would ratchet it with a move that
        # may only have happened after the position was already stopped out.
        if pos.side is Side.BUY:
            probe_prices = (bar.low, bar.high)
        else:
            probe_prices = (bar.high, bar.low)
        for price in probe_prices:
            probe = Tick(symbol=cfg.symbol, bid=price, ask=price, timestamp=bar.timestamp)
            result = self.trailing.update(pos, probe, spec)
            if result.changed and result.new_stop is not None:
                pos.stop_loss = result.new_stop
                if result.break_even_done:
                    pos.break_even_done = True

        hit_sl = pos.stop_loss is not None and (
            bar.low <= pos.stop_loss if pos.side is Side.BUY else bar.high >= pos.stop_loss
        )
        hit_tp = pos.take_profit is not None and (
            bar.high >= pos.take_profit if pos.side is Side.BUY else bar.low <= pos.take_profit
        )

        if hit_sl and hit_tp:
            # Conservative: assume the stop filled first.
            return pos.stop_loss, "stop_loss", pos.stop_loss, pos.take_profit
        if hit_sl:
            return pos.stop_loss, "stop_loss", pos.stop_loss, pos.take_profit
        if hit_tp:
            return pos.take_profit, "take_profit", pos.stop_loss, pos.take_profit

        # Open-bar gap through the stop: fill at the open, not at the stop.
        if pos.side is Side.BUY and bar.open < pos.stop_loss:
            return bar.open, "stop_loss_gap", pos.stop_loss, pos.take_profit
        if pos.side is Side.SELL and bar.open > pos.stop_loss:
            return bar.open, "stop_loss_gap", pos.stop_loss, pos.take_profit
        return None

    def _close(
        self, cash: float, pos: Position, exit_price: float, reason: str,
        ts: datetime, stop: float | None, target: float | None, index: int,
    ) -> BacktestTrade:
        spec = self.spec
        pnl = position_pnl(
            pos.side, pos.volume, pos.open_price, exit_price, spec,
            commission=pos.commission, swap=pos.swap,
        )
        risk_distance = abs(pos.open_price - stop) if stop else 0.0
        r = 0.0
        if risk_distance > 0:
            r = (
                (exit_price - pos.open_price) * pos.side.sign
            ) / risk_distance

        trade = BacktestTrade(
            symbol=pos.symbol, side=pos.side.value, volume=pos.volume,
            entry_time=pos.opened_at, entry_price=pos.open_price,
            exit_time=ts, exit_price=round_price(exit_price, spec.digits),
            stop_loss=stop, take_profit=target, pnl=pnl,
            commission=pos.commission, swap=pos.swap,
            result="WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN"),
            r_multiple=r, close_reason=reason, bars_held=max(0, index),
        )
        return trade

    def _bar_tick(self, bar: Bar, spread_pips: float, slippage_pips: float) -> Tick:
        spec = self.spec
        half = spec.pips_to_price(spread_pips / 2.0)
        return Tick(
            symbol=bar.symbol,
            bid=round_price(bar.close - half, spec.digits),
            ask=round_price(bar.close + half, spec.digits),
            timestamp=bar.timestamp,
            volume=bar.volume,
        )

    def _monthly(self, trades: list[BacktestTrade], initial: float) -> list[dict]:
        buckets: dict[str, float] = {}
        for t in trades:
            key = t.exit_time.strftime("%Y-%m") if t.exit_time else "unknown"
            buckets[key] = buckets.get(key, 0.0) + t.pnl
        out = []
        for key in sorted(buckets):
            pnl = buckets[key]
            out.append({
                "month": key, "pnl": round(pnl, 2),
                "return_pct": round(pnl / initial * 100.0, 4) if initial else 0.0,
            })
        return out


async def run_backtest(
    config: BacktestConfig,
    bars: Sequence[Bar],
    *,
    progress_cb: Callable[[float], None] | None = None,
) -> BacktestResultData:
    """Async wrapper so long runs can be moved to a thread pool."""
    def _work() -> BacktestResultData:
        return BacktestEngine(config, progress_cb=progress_cb).run(bars)

    return await asyncio.to_thread(_work)
