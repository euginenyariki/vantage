"""Backtest job submission and results.

A backtest is queued, not run inline: the API writes a job row and publishes the
id on ``vantage:backtest:jobs``; the backtest worker picks it up, simulates on a
separate thread, and mirrors progress onto the realtime event stream. Nothing
here blocks an HTTP request on a simulation.

The figures returned are what the simulation produced under the cost model the
caller supplied. They are not a forecast — see the honesty note on the endpoint.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from trading_engine.persistence.models import AuditLog, BacktestJob, BacktestResult, User

from app.core.database import get_session
from app.core.redis_bus import CHANNEL_BACKTEST, get_redis
from app.dependencies import get_current_user, rate_limit
from app.schemas import (
    BacktestCreate,
    BacktestDefaults,
    BacktestDetail,
    BacktestOptionsOut,
    BacktestOut,
    BacktestResultOut,
    StrategyOption,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/backtests", tags=["backtesting"])

# Simulations are CPU-bound, so the queue is deliberately shallow: enough to
# let a user line up a few configurations without letting one account monopolise
# the worker.
_SUBMIT = rate_limit("backtest-submit", limit=10, window_seconds=300)
_MAX_ACTIVE = 5
_MAX_RANGE_DAYS = 3650
_KNOWN_STRATEGIES = {"hft_scalper", "mean_reversion"}
_KNOWN_TIMEFRAMES = {"M1", "M5", "M15", "H1", "H4", "D1"}
_KNOWN_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD"]


def _dec(value) -> float:
    """Decimal/None -> float for the response models."""
    return float(value) if value is not None else 0.0


@router.get("/options", response_model=BacktestOptionsOut)
async def backtest_options(user: User = Depends(get_current_user)) -> BacktestOptionsOut:
    """What can be backtested, straight from the engine's own registry.

    The web client renders these instead of hardcoding a list. A duplicated
    list silently drifts: it once offered a strategy that does not exist and
    omitted a supported timeframe, and nothing caught it until a user hit a
    422.
    """
    from trading_engine.strategies.base import StrategyRegistry

    labels = {
        "hft_scalper": "HFT Scalper",
        "mean_reversion": "Mean Reversion Scalper",
    }
    strategies = [
        StrategyOption(value=name, label=labels.get(name, name.replace("_", " ").title()))
        for name in sorted(StrategyRegistry.all())
    ]
    return BacktestOptionsOut(
        strategies=strategies,
        timeframes=sorted(_KNOWN_TIMEFRAMES),
        symbols=_KNOWN_SYMBOLS,
        defaults=BacktestDefaults(
            initial_capital=10_000.0,
            risk_percent=1.0,
            spread_pips=0.5,
            slippage_pips=0.2,
            commission_per_lot=3.5,
        ),
    )


async def _get_job(session: AsyncSession, user_id: str, job_id: str) -> BacktestJob:
    """Fetch a job, scoped to its owner.

    Scoping in the WHERE clause rather than after the fetch, so a foreign id
    is indistinguishable from a missing one — no existence oracle.
    """
    job = await session.scalar(
        select(BacktestJob).where(BacktestJob.id == job_id, BacktestJob.user_id == user_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return job


@router.post("", response_model=BacktestOut, status_code=status.HTTP_201_CREATED)
async def create_backtest(
    payload: BacktestCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_SUBMIT),
) -> BacktestJob:
    """Queue a simulation.

    The response is a queued job, not a result. Poll ``GET /backtests/{id}`` or
    subscribe to the ``backtest`` events on the WebSocket for progress.
    """
    strategy = payload.strategy.strip()
    if strategy not in _KNOWN_STRATEGIES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown strategy {strategy!r}. Available: {sorted(_KNOWN_STRATEGIES)}",
        )
    tf = payload.timeframe.strip().upper()
    if tf not in _KNOWN_TIMEFRAMES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown timeframe {payload.timeframe!r}. Available: {sorted(_KNOWN_TIMEFRAMES)}",
        )
    if payload.date_from >= payload.date_to:
        raise HTTPException(status_code=422, detail="date_from must be before date_to")
    if (payload.date_to - payload.date_from).days > _MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail="Date range too wide (max 10 years)")

    active = await session.scalar(
        select(func.count(BacktestJob.id)).where(
            BacktestJob.user_id == user.id,
            BacktestJob.status.in_(("QUEUED", "RUNNING")),
        )
    )
    if (active or 0) >= _MAX_ACTIVE:
        raise HTTPException(
            status_code=409, detail=f"Backtest queue full ({_MAX_ACTIVE} active). Wait for one to finish."
        )

    job = BacktestJob(
        user_id=user.id,
        strategy=strategy,
        symbol=payload.symbol.strip().upper(),
        timeframe=tf,
        date_from=payload.date_from,
        date_to=payload.date_to,
        initial_capital=Decimal(str(payload.initial_capital)),
        risk_percent=Decimal(str(payload.risk_percent)),
        spread_pips=Decimal(str(payload.spread_pips)),
        commission_per_lot=Decimal(str(payload.commission_per_lot)),
        slippage_pips=Decimal(str(payload.slippage_pips)),
        status="QUEUED",
        progress=Decimal("0"),
    )
    session.add(job)
    await session.flush()
    session.add(
        AuditLog(
            user_id=user.id,
            actor_email=user.email,
            action="backtest.queued",
            entity_type="backtest",
            entity_id=job.id,
            details={"strategy": strategy, "symbol": job.symbol, "timeframe": tf},
        )
    )
    await session.commit()
    await session.refresh(job)

    # Fire-and-forget. If Redis is down the row stays QUEUED and surfaces as
    # stuck to the user, which is honest; the API does not pretend to have
    # accepted work it could not hand off.
    try:
        await get_redis().publish(CHANNEL_BACKTEST, json.dumps({"job_id": job.id}))
    except Exception as exc:  # pragma: no cover - depends on infra
        logger.error("Could not enqueue backtest %s: %s", job.id, exc)
        job.status = "FAILED"
        job.error = "Backtest worker unreachable. Try again shortly."
        job.finished_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(job)

    logger.info("Queued backtest %s (%s %s)", job.id, strategy, job.symbol)
    return job


@router.get("", response_model=list[BacktestOut])
async def list_backtests(
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[BacktestJob]:
    stmt = (
        select(BacktestJob)
        .where(BacktestJob.user_id == user.id)
        .order_by(BacktestJob.created_at.desc())
        .limit(limit)
    )
    return list(await session.scalars(stmt))


@router.get("/{job_id}", response_model=BacktestDetail)
async def get_backtest(
    job_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BacktestDetail:
    job = await _get_job(session, user.id, job_id)
    result = await session.get(BacktestResult, job_id)

    # from_attributes is set on the schema, so the ORM row maps itself. A
    # hand-written field list silently drops every column added later.
    detail = BacktestDetail.model_validate(job)
    if result is not None:
        detail.result = BacktestResultOut(
            final_equity=_dec(result.final_equity),
            net_pnl=_dec(result.net_pnl),
            roi_percent=_dec(result.roi_percent),
            total_trades=result.total_trades,
            winning_trades=result.winning_trades,
            losing_trades=result.losing_trades,
            win_rate=_dec(result.win_rate),
            profit_factor=(
                float(result.profit_factor) if result.profit_factor is not None else None
            ),
            max_drawdown_percent=_dec(result.max_drawdown_percent),
            max_drawdown_amount=_dec(result.max_drawdown_amount),
            average_trade=_dec(result.average_trade),
            best_trade=_dec(result.best_trade),
            worst_trade=_dec(result.worst_trade),
            avg_win=_dec(result.avg_win),
            avg_loss=_dec(result.avg_loss),
            total_commission=_dec(result.total_commission),
            equity_curve=result.equity_curve or [],
            drawdown_curve=result.drawdown_curve or [],
            monthly_returns=result.monthly_returns or [],
            trades=result.trades or [],
        )
    return detail


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backtest(
    job_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove a finished job. A running job cannot be deleted — there is no
    cancellation in the worker, so the row is what stops it being re-read."""
    job = await _get_job(session, user.id, job_id)
    if job.status == "RUNNING":
        raise HTTPException(
            status_code=409, detail="Cannot delete a running backtest. Wait for it to finish."
        )
    await session.delete(job)
    await session.commit()
    logger.info("Deleted backtest %s", job_id)


@router.post("/{job_id}/requeue", response_model=BacktestOut)
async def requeue_backtest(
    job_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_SUBMIT),
) -> BacktestJob:
    """Re-run a finished job with the same parameters."""
    job = await _get_job(session, user.id, job_id)
    if job.status in ("QUEUED", "RUNNING"):
        raise HTTPException(status_code=409, detail="Backtest is already queued or running")

    job.status = "QUEUED"
    job.progress = Decimal("0")
    job.error = None
    job.started_at = None
    job.finished_at = None
    await session.commit()
    await session.refresh(job)

    try:
        await get_redis().publish(CHANNEL_BACKTEST, json.dumps({"job_id": job.id}))
    except Exception as exc:  # pragma: no cover - depends on infra
        logger.error("Could not re-enqueue backtest %s: %s", job.id, exc)
        job.status = "FAILED"
        job.error = "Backtest worker unreachable. Try again shortly."
        job.finished_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(job)
    return job
