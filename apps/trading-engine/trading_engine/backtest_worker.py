"""Backtest worker — a separate process that consumes queued jobs from Redis.

Backtests never run inside an HTTP request (Phase 4). The API enqueues a job id;
this worker picks it up, ingests/loads history, runs the simulation, persists
the result and publishes progress events.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import signal
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy import select

from trading_engine.backtest.engine import BacktestConfig, run_backtest
from trading_engine.backtest.historical import HistoricalDataService
from trading_engine.core.settings import get_settings
from trading_engine.core.types import TrailingMode
from trading_engine.execution.events import CHANNEL_BACKTEST, EngineEvent, EventBus
from trading_engine.persistence.models import (
    BacktestJob,
    BacktestResult,
)
from trading_engine.persistence.repository import init_engine
from trading_engine.risk.engine import RiskConfig
from trading_engine.trailing.service import TrailingConfig

logger = logging.getLogger("vantage.backtest_worker")


class BacktestWorker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.sessionmaker = init_engine(self.settings.database_url)
        import redis.asyncio as aioredis
        self.redis = aioredis.from_url(self.settings.redis_url, decode_responses=True)
        self.events = EventBus(self.redis)
        self.history = HistoricalDataService(self.sessionmaker)
        self._stop = asyncio.Event()

    async def _recover_orphans(self) -> None:
        """Re-adopt jobs that were queued while this worker was down.

        Redis pub/sub is fire-and-forget: a job published before the worker
        subscribes is never delivered, so a restart (or a first deploy) would
        otherwise strand those rows in QUEUED forever with nothing running
        them. PostgreSQL is the source of truth for job state, so reconcile
        from it on startup instead.
        """
        async with self.sessionmaker() as session:
            orphans = list(
                await session.scalars(
                    select(BacktestJob).where(BacktestJob.status == "QUEUED")
                )
            )
        if not orphans:
            return
        logger.warning("Recovering %d orphaned backtest job(s) from a previous run", len(orphans))
        for job in orphans:
            try:
                await self.execute(job.id)
            except Exception as exc:
                logger.exception("Recovery of backtest %s failed", job.id)
                await self._fail(job.id, str(exc))

    async def run(self) -> None:
        logger.info("Backtest worker online (mode=%s)", self.settings.trading_mode)
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(CHANNEL_BACKTEST)
        # Reconcile before the live loop so a restart cannot lose queued work.
        await self._recover_orphans()
        try:
            async for message in pubsub.listen():
                if self._stop.is_set():
                    break
                if message.get("type") != "message":
                    continue
                await self._handle(message["data"])
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.unsubscribe(CHANNEL_BACKTEST)
            await pubsub.aclose()

    async def _handle(self, raw: str) -> None:
        import json
        try:
            job_id = json.loads(raw)["job_id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.debug("Ignoring malformed backtest job message: %s", raw[:120])
            return
        try:
            await self.execute(job_id)
        except Exception as exc:
            logger.exception("Backtest %s failed", job_id)
            await self._fail(job_id, str(exc))

    async def execute(self, job_id: str) -> None:
        async with self.sessionmaker() as session:
            job = await session.scalar(select(BacktestJob).where(BacktestJob.id == job_id))
            if job is None:
                logger.error("Backtest job %s not found", job_id)
                return
            if job.status not in ("QUEUED", "RETRY"):
                logger.info("Backtest %s already %s; skipping", job_id, job.status)
                return

            job.status = "RUNNING"
            job.started_at = datetime.now(UTC)
            job.progress = Decimal("0")
            await session.commit()

            user_id = job.user_id
            config = BacktestConfig(
                strategy_name=job.strategy,
                symbol=job.symbol,
                timeframe=job.timeframe,
                date_from=job.date_from,
                date_to=job.date_to,
                initial_capital=float(job.initial_capital),
                risk_percent=float(job.risk_percent),
                spread_pips=float(job.spread_pips or 0),
                slippage_pips=float(job.slippage_pips or 0),
                commission_per_lot=float(job.commission_per_lot or 0),
                risk=RiskConfig(
                    trading_mode=self.settings.mode,
                    max_risk_percent_per_trade=float(job.risk_percent),
                    require_broker_connected=False,
                ),
                trailing=TrailingConfig(
                    mode=TrailingMode.FIXED,
                    distance_pips=float((job.__dict__.get("trailing") or {}).get("distance_pips", 10.0))
                    if isinstance(job.__dict__.get("trailing"), dict) else 10.0,
                ),
            )
            start_dt = datetime.combine(job.date_from, time.min, tzinfo=UTC)
            end_dt = datetime.combine(job.date_to, time.max, tzinfo=UTC)

        await self.events.publish(EngineEvent.backtest(job_id, "RUNNING", user_id=user_id, progress=0))

        bars = await self.history.load_bars(job.symbol, job.timeframe, start_dt, end_dt)
        if len(bars) < 60:
            written = await self.history.ingest(job.symbol, job.timeframe, start_dt, end_dt)
            logger.info("Ingested %d historical bars for %s", written, job.symbol)
            bars = await self.history.load_bars(job.symbol, job.timeframe, start_dt, end_dt)

        if len(bars) < 60:
            await self._fail(
                job_id, f"Not enough historical data ({len(bars)} bars). "
                        f"Widen the date range or choose a shorter timeframe."
            )
            return

        # run_backtest() executes the simulation in a worker thread, so the
        # progress callback is a *synchronous* function invoked off the event
        # loop. Hand the coroutine back to the loop thread-safely and keep the
        # futures so every write lands before the final COMPLETED update below.
        loop = asyncio.get_running_loop()
        pending: list[concurrent.futures.Future] = []

        def _progress(pct: float) -> None:
            pending.append(
                asyncio.run_coroutine_threadsafe(
                    self._record_progress(job_id, user_id, pct), loop
                )
            )

        result = await run_backtest(config, bars, progress_cb=_progress)

        if pending:
            await asyncio.gather(*(asyncio.wrap_future(f) for f in pending))
        payload = result.as_dict()

        async with self.sessionmaker() as session:
            job = await session.get(BacktestJob, job_id)
            if job is not None:
                job.status = "COMPLETED"
                job.progress = Decimal("100.00")
                job.finished_at = datetime.now(UTC)
            session.add(
                BacktestResult(
                    job_id=job_id,
                    final_equity=Decimal(str(payload["final_equity"])),
                    net_pnl=Decimal(str(payload["net_pnl"])),
                    roi_percent=Decimal(str(payload["roi_percent"])),
                    total_trades=payload["total_trades"],
                    winning_trades=payload["winning_trades"],
                    losing_trades=payload["losing_trades"],
                    win_rate=Decimal(str(payload["win_rate"])),
                    profit_factor=(Decimal(str(payload["profit_factor"]))
                                   if payload["profit_factor"] is not None else None),
                    max_drawdown_percent=Decimal(str(payload["max_drawdown_percent"])),
                    max_drawdown_amount=Decimal(str(payload["max_drawdown_amount"])),
                    average_trade=Decimal(str(payload["average_trade"])),
                    best_trade=Decimal(str(payload["best_trade"])),
                    worst_trade=Decimal(str(payload["worst_trade"])),
                    avg_win=Decimal(str(payload["avg_win"])),
                    avg_loss=Decimal(str(payload["avg_loss"])),
                    total_commission=Decimal(str(payload["total_commission"])),
                    equity_curve=payload["equity_curve"],
                    drawdown_curve=payload["drawdown_curve"],
                    monthly_returns=payload["monthly_returns"],
                    trades=payload["trades"],
                    config=payload["config"],
                )
            )
            await session.commit()

        await self.events.publish(
            EngineEvent.backtest(
                job_id, "COMPLETED", user_id=user_id, progress=100.0,
                total_trades=payload["total_trades"], net_pnl=payload["net_pnl"],
            )
        )
        logger.info("Backtest %s completed: %d trades, net %.2f",
                    job_id, payload["total_trades"], payload["net_pnl"])

    async def _record_progress(
        self, job_id: str, user_id: str, pct: float
    ) -> None:
        """Persist a progress tick and mirror it to the realtime channel.

        Invoked on the event loop via ``run_coroutine_threadsafe`` because the
        simulation itself runs in a worker thread.
        """
        try:
            async with self.sessionmaker() as session:
                row = await session.get(BacktestJob, job_id)
                if row is not None:
                    row.progress = Decimal(str(round(pct, 2)))
                    await session.commit()
            await self.events.publish(
                EngineEvent.backtest(job_id, "RUNNING", user_id=user_id, progress=pct)
            )
        except Exception as exc:  # progress is cosmetic; never fail the run
            logger.warning("Could not record backtest progress for %s: %s", job_id, exc)

    async def _fail(self, job_id: str, message: str) -> None:
        async with self.sessionmaker() as session:
            job = await session.get(BacktestJob, job_id)
            if job is not None:
                job.status = "FAILED"
                job.error = message[:2000]
                job.finished_at = datetime.now(UTC)
                user_id = job.user_id
                await session.commit()
        await self.events.publish(
            EngineEvent.backtest(job_id, "FAILED", user_id=user_id, error=message)
        )


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    worker = BacktestWorker()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    task = asyncio.create_task(worker.run())
    await stop.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("Backtest worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
