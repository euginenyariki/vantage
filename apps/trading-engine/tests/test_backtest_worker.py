"""Regression tests for the backtest worker's durability guarantees.

Two defects made backtesting unusable in practice, and both only show up at
realistic volumes, which is why the suite never caught them:

1. Redis pub/sub is fire-and-forget. A job queued while the worker was down was
   never delivered, so the row sat in QUEUED forever with nothing running it.
   A deploy restart silently killed every queued backtest.
2. ``upsert_many`` built a single multi-row INSERT. PostgreSQL's wire protocol
   caps a statement at 32767 bind parameters, so a month of M1 data (~37k bars)
   raised InterfaceError before a single bar was stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from trading_engine.persistence.repository import HistoricalDataRepository


# --------------------------------------------------------------------------- #
# Bulk insert chunking
# --------------------------------------------------------------------------- #
class _RecordingSession:
    """Captures the statements a repository would have executed."""

    def __init__(self) -> None:
        self.executed: list[object] = []
        self.flushed = 0

    async def execute(self, stmt) -> None:
        self.executed.append(stmt)

    async def flush(self) -> None:
        self.flushed += 1


def _bar(i: int) -> dict:
    ts = datetime(2026, 3, 2, tzinfo=UTC) + timedelta(minutes=i)
    return {
        "symbol": "eurusd", "timeframe": "M1", "timestamp": ts,
        "open": Decimal("1.0800"), "high": Decimal("1.0810"),
        "low": Decimal("1.0790"), "close": Decimal("1.0805"),
        "volume": Decimal("1000"),
    }


def _repo(session) -> HistoricalDataRepository:
    repo = HistoricalDataRepository.__new__(HistoricalDataRepository)
    repo.session = session
    return repo


class TestUpsertChunking:
    @pytest.mark.asyncio
    async def test_empty_input_is_a_noop(self):
        session = _RecordingSession()
        assert await _repo(session).upsert_many([]) == 0
        assert session.executed == []

    @pytest.mark.asyncio
    async def test_small_batch_is_one_statement(self):
        session = _RecordingSession()
        written = await _repo(session).upsert_many([_bar(i) for i in range(10)])
        assert written == 10
        assert len(session.executed) == 1

    @pytest.mark.asyncio
    async def test_a_month_of_m1_data_is_split(self):
        """~37k bars is the range a user actually asks for; it must not raise."""
        session = _RecordingSession()
        rows = [_bar(i) for i in range(37_000)]
        written = await _repo(session).upsert_many(rows)

        assert written == 37_000
        assert len(session.executed) > 1

    @pytest.mark.asyncio
    async def test_no_statement_exceeds_the_bind_parameter_cap(self):
        """8 columns per row, PostgreSQL allows 32767 parameters per statement."""
        session = _RecordingSession()
        await _repo(session).upsert_many([_bar(i) for i in range(37_000)])

        chunk = HistoricalDataRepository._UPSERT_CHUNK_ROWS
        assert chunk * 8 <= 32767
        # Every emitted statement carries exactly one chunk's worth of rows.
        assert len(session.executed) == -(-37_000 // chunk)

    @pytest.mark.asyncio
    async def test_boundaries_are_exact(self):
        """A batch sitting exactly on a chunk boundary must not emit a
        trailing empty statement."""
        chunk = HistoricalDataRepository._UPSERT_CHUNK_ROWS
        session = _RecordingSession()
        await _repo(session).upsert_many([_bar(i) for i in range(chunk * 2)])
        assert len(session.executed) == 2

    @pytest.mark.asyncio
    async def test_remainder_batch_still_lands(self):
        chunk = HistoricalDataRepository._UPSERT_CHUNK_ROWS
        session = _RecordingSession()
        written = await _repo(session).upsert_many(
            [_bar(i) for i in range(chunk * 2 + 7)]
        )
        assert written == chunk * 2 + 7
        assert len(session.executed) == 3

    @pytest.mark.asyncio
    async def test_flushed_once_not_per_chunk(self):
        """Flush once so the whole ingest commits as a single unit of work."""
        session = _RecordingSession()
        await _repo(session).upsert_many([_bar(i) for i in range(2_000)])
        assert session.flushed == 1

    @pytest.mark.asyncio
    async def test_symbols_are_normalised(self):
        session = _RecordingSession()
        await _repo(session).upsert_many([_bar(0)])
        stmt = session.executed[0]
        assert "EURUSD" in str(stmt.compile().params)


# --------------------------------------------------------------------------- #
# Startup recovery of stranded jobs
# --------------------------------------------------------------------------- #
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def scalars(self, stmt):
        return _FakeResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _worker_with(session, *, run=None, fail=None):
    from trading_engine.backtest_worker import BacktestWorker

    worker = BacktestWorker.__new__(BacktestWorker)
    worker.sessionmaker = lambda: session
    worker.executed: list[str] = []
    worker.execute = run or (lambda job_id: worker.executed.append(job_id) or _noop())
    worker._fail = fail or _noop_async
    return worker


async def _noop() -> None:
    return None


async def _noop_async(*_a, **_k) -> None:
    return None


class TestRecoverOrphans:
    @pytest.mark.asyncio
    async def test_nothing_queued_is_quiet(self):
        worker = _worker_with(_FakeSession([]))
        await worker._recover_orphans()
        assert worker.executed == []

    @pytest.mark.asyncio
    async def test_stranded_jobs_are_re_executed(self):
        """The job was published while the worker was offline and lost. The
        row is the only surviving record, so startup must reconcile from it."""
        rows = [SimpleNamespace(id="job-a"), SimpleNamespace(id="job-b")]
        worker = _worker_with(_FakeSession(rows))
        await worker._recover_orphans()
        assert worker.executed == ["job-a", "job-b"]

    @pytest.mark.asyncio
    async def test_one_failure_does_not_abort_the_sweep(self):
        """A poisoned job must not prevent the rest of the queue from running."""

        async def run(job_id: str) -> None:
            worker.executed.append(job_id)
            if job_id == "job-a":
                raise RuntimeError("corrupt parameters")

        failed: list[tuple[str, str]] = []

        async def fail(job_id: str, message: str) -> None:
            failed.append((job_id, message))

        worker = _worker_with(
            _FakeSession([SimpleNamespace(id="job-a"), SimpleNamespace(id="job-b")]),
            run=run, fail=fail,
        )
        await worker._recover_orphans()

        assert worker.executed == ["job-a", "job-b"]
        assert failed == [("job-a", "corrupt parameters")]
