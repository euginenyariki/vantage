"""Performance analytics.

Every figure here is computed from closed trades in the database. Nothing is
estimated, smoothed or optimistically rounded, and a metric that cannot be
computed from available data is reported as ``None`` rather than as a
favourable default.

Note on Sharpe: the platform does not keep a daily account-value series for
paper accounts that never linked a broker, so this is a *PnL-based* Sharpe
(mean daily PnL / stdev daily PnL, annualised by sqrt(252)). It is labelled as
such in the API and UI so it is not mistaken for a return-on-equity Sharpe.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from trading_engine.persistence.models import Bot, Trade

# 252 trading days in a conventional year, used to annualise the PnL Sharpe.
TRADING_DAYS_PER_YEAR = 252.0


def _f(value) -> float:
    """Decimal/None -> float, treating None as 0."""
    return float(value) if value is not None else 0.0


def sharpe_from_daily_pnl(daily: list[float]) -> float | None:
    """Annualised PnL-based Sharpe ratio, or None with insufficient data.

    Requires at least 2 observations and a non-zero standard deviation;
    a flat PnL series has no risk-adjusted return to report.
    """
    if len(daily) < 2:
        return None
    n = len(daily)
    mean = sum(daily) / n
    variance = sum((x - mean) ** 2 for x in daily) / (n - 1)  # sample stdev
    if variance <= 0:
        return None
    stdev = math.sqrt(variance)
    return round(mean / stdev * math.sqrt(TRADING_DAYS_PER_YEAR), 3)


def max_drawdown(equity: list[float]) -> tuple[float, float]:
    """Largest peak-to-trough decline in the cumulative PnL curve.

    Returns ``(absolute, percent_of_peak)``. Percent is measured against the
    peak, which is the standard convention; a curve that never rises has no
    meaningful percentage, so it is reported as 0.0.
    """
    peak = float("-inf")
    worst_abs = 0.0
    worst_pct = 0.0
    for value in equity:
        peak = max(peak, value)
        decline = peak - value
        if decline > worst_abs:
            worst_abs = decline
            worst_pct = (decline / peak * 100.0) if peak > 0 else 0.0
    return round(worst_abs, 2), round(worst_pct, 2)


def streaks(pnls: list[float]) -> tuple[int, int]:
    """Longest winning and longest losing run, in consecutive trades."""
    best_win = best_loss = 0
    cur_win = cur_loss = 0
    for pnl in pnls:
        if pnl > 0:
            cur_win, cur_loss = cur_win + 1, 0
            best_win = max(best_win, cur_win)
        elif pnl < 0:
            cur_loss, cur_win = cur_loss + 1, 0
            best_loss = max(best_loss, cur_loss)
        else:
            # A flat trade breaks both runs; it is neither a win nor a loss.
            cur_win = cur_loss = 0
    return best_win, best_loss


async def _closed_trades(
    session: AsyncSession, user_id: str, since: datetime | None
) -> list[Trade]:
    stmt = select(Trade).where(Trade.user_id == user_id)
    if since is not None:
        stmt = stmt.where(Trade.closed_at >= since)
    return list((await session.execute(stmt.order_by(Trade.closed_at.asc()))).scalars())


def _core_metrics(trades: list[Trade]) -> dict:
    """Win/loss/expectancy style metrics from a list of closed trades."""
    pnls = [_f(t.pnl) for t in trades]
    total = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(pnls)

    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0

    # Expectancy is the average result per trade and is the single most
    # informative number: it is what decides whether a strategy is worth
    # running, independently of how flattering the win rate looks.
    expectancy = net / total if total else 0.0
    win_rate = len(wins) / total * 100.0 if total else 0.0

    # A losing system can show a high win rate, so surface what a win is worth
    # relative to what a loss costs. None when there are no losses to compare.
    payoff = (avg_win / avg_loss) if avg_loss > 0 else None
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    best_streak, worst_streak = streaks(pnls)

    return {
        "total_trades": total,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": total - len(wins) - len(losses),
        "win_rate": round(win_rate, 2),
        "net_pnl": round(net, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-avg_loss, 2),
        "largest_win": round(max(pnls), 2) if pnls else 0.0,
        "largest_loss": round(min(pnls), 2) if pnls else 0.0,
        "expectancy": round(expectancy, 4),
        "payoff_ratio": round(payoff, 3) if payoff is not None else None,
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "longest_win_streak": best_streak,
        "longest_loss_streak": worst_streak,
    }


async def get_performance(
    session: AsyncSession, user_id: str, days: int = 90
) -> dict:
    """Full performance report for the trailing ``days`` window."""
    since = datetime.now(UTC) - timedelta(days=days)
    trades = await _closed_trades(session, user_id, since)
    metrics = _core_metrics(trades)

    # Daily PnL series, oldest first, for the equity curve and Sharpe.
    daily_map: dict = {}
    for trade in trades:
        if trade.closed_at is None:
            continue
        day = trade.closed_at.astimezone(UTC).date()
        daily_map[day] = daily_map.get(day, 0.0) + _f(trade.pnl)

    days_sorted = sorted(daily_map)
    daily_series = [daily_map[d] for d in days_sorted]

    equity: list[float] = []
    running = 0.0
    for value in daily_series:
        running += value
        equity.append(round(running, 2))

    dd_abs, dd_pct = max_drawdown(equity)
    sharpe = sharpe_from_daily_pnl(daily_series)

    metrics.update(
        {
            "period_days": days,
            "trading_days": len(daily_series),
            "max_drawdown": dd_abs,
            "max_drawdown_percent": dd_pct,
            # PnL-based, not return-on-equity: see the module docstring.
            "sharpe_pnl_ratio": sharpe,
            "best_day": round(max(daily_series), 2) if daily_series else 0.0,
            "worst_day": round(min(daily_series), 2) if daily_series else 0.0,
            "profitable_days": sum(1 for v in daily_series if v > 0),
        }
    )
    return metrics


async def get_equity_series(
    session: AsyncSession, user_id: str, days: int = 90
) -> list[dict]:
    """Daily cumulative PnL points for charting, oldest first."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                func.date_trunc("day", Trade.closed_at).label("day"),
                func.coalesce(func.sum(Trade.pnl), 0).label("pnl"),
            )
            .where(Trade.user_id == user_id, Trade.closed_at >= since)
            .group_by("day")
            .order_by("day")
        )
    ).all()

    points: list[dict] = []
    running = 0.0
    for day, pnl in rows:
        daily = _f(pnl)
        running += daily
        points.append(
            {
                "date": day.date().isoformat() if day is not None else None,
                "daily_pnl": round(daily, 2),
                "cumulative_pnl": round(running, 2),
            }
        )
    return points


async def get_by_symbol(session: AsyncSession, user_id: str, days: int = 90) -> list[dict]:
    """Per-symbol breakdown so weak markets can be spotted and disabled."""
    since = datetime.now(UTC) - timedelta(days=days)
    trades = await _closed_trades(session, user_id, since)

    buckets: dict[str, list[float]] = {}
    for trade in trades:
        buckets.setdefault(trade.symbol, []).append(_f(trade.pnl))

    out: list[dict] = []
    for symbol, pnls in buckets.items():
        stats = _core_metrics([t for t in trades if t.symbol == symbol])
        out.append(
            {
                "symbol": symbol,
                "trades": stats["total_trades"],
                "win_rate": stats["win_rate"],
                "net_pnl": stats["net_pnl"],
                "expectancy": stats["expectancy"],
                "profit_factor": stats["profit_factor"],
            }
        )
    out.sort(key=lambda r: r["net_pnl"], reverse=True)
    return out


async def get_by_strategy(session: AsyncSession, user_id: str, days: int = 90) -> list[dict]:
    """Per-strategy breakdown, joined through the owning bot."""
    since = datetime.now(UTC) - timedelta(days=days)
    trades = await _closed_trades(session, user_id, since)

    # Trades carry bot_id; resolve each distinct bot to its strategy name once.
    bot_ids = {t.bot_id for t in trades if t.bot_id}
    names: dict[str, str] = {}
    if bot_ids:
        rows = (
            await session.execute(select(Bot.id, Bot.strategy).where(Bot.id.in_(bot_ids)))
        ).all()
        names = {bid: strategy for bid, strategy in rows}

    buckets: dict[str, list[float]] = {}
    for trade in trades:
        key = names.get(trade.bot_id or "", "unattributed")
        buckets.setdefault(key, []).append(_f(trade.pnl))

    out: list[dict] = []
    for strategy, pnls in buckets.items():
        subset = [t for t in trades
                  if names.get(t.bot_id or "", "unattributed") == strategy]
        stats = _core_metrics(subset)
        out.append(
            {
                "strategy": strategy,
                "trades": stats["total_trades"],
                "win_rate": stats["win_rate"],
                "net_pnl": stats["net_pnl"],
                "expectancy": stats["expectancy"],
                "profit_factor": stats["profit_factor"],
            }
        )
    out.sort(key=lambda r: r["net_pnl"], reverse=True)
    return out
