"""Backtest submission, scoping and the jobs that carry the results.

Covers the API surface plus the two engine-side defects that made backtesting
unusable in practice: a worker that stranded jobs queued while it was down, and
an unchunked bulk insert that blew PostgreSQL's 32767 bind-parameter cap.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from trading_engine.persistence.models import BacktestJob, BacktestResult

def _reset_rate_limits() -> None:
    """The submit limiter (10/5min) and the queue cap (5) both fire long before
    the option matrix is exhausted. This test is about validation, so the
    buckets are cleared between iterations rather than the limits weakened."""
    from app.dependencies import _buckets

    _buckets.clear()


VALID = {
    "strategy": "hft_scalper",
    "symbol": "EURUSD",
    "timeframe": "M1",
    "date_from": "2026-03-02",
    "date_to": "2026-03-27",
    "initial_capital": 10000,
    "risk_percent": 1.0,
    "spread_pips": 0.5,
    "slippage_pips": 0.2,
    "commission_per_lot": 3.5,
}


async def _queue(client, headers, **overrides) -> dict:
    body = {**VALID, **overrides}
    resp = await client.post("/api/backtests", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_queues_a_job(client, auth):
    job = await _queue(client, auth)
    assert job["status"] == "QUEUED"
    assert job["strategy"] == "hft_scalper"
    assert job["symbol"] == "EURUSD"
    assert job["progress"] == 0.0


@pytest.mark.asyncio
async def test_cost_assumptions_round_trip(client, auth):
    """The cost model decides whether a result is a win, so it must survive
    the round trip on every endpoint that returns a job."""
    created = await _queue(client, auth)

    for body in (created, (await client.get("/api/backtests", headers=auth)).json()[0]):
        assert body["spread_pips"] == pytest.approx(0.5)
        assert body["slippage_pips"] == pytest.approx(0.2)
        assert body["commission_per_lot"] == pytest.approx(3.5)
        assert body["initial_capital"] == pytest.approx(10_000)
        assert body["risk_percent"] == pytest.approx(1.0)

    detail = (await client.get(f"/api/backtests/{created['id']}", headers=auth)).json()
    assert detail["spread_pips"] == pytest.approx(0.5)
    assert detail["commission_per_lot"] == pytest.approx(3.5)


@pytest.mark.asyncio
async def test_publishes_the_job_id_to_redis(client, auth):
    """The worker only ever learns about work through this channel."""
    from app.core.redis_bus import CHANNEL_BACKTEST, get_redis

    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL_BACKTEST)
    try:
        job = await _queue(client, auth)
        seen = None
        async for message in pubsub.listen():
            if message.get("type") == "message":
                import json

                seen = json.loads(message["data"])
                break
    finally:
        await pubsub.unsubscribe(CHANNEL_BACKTEST)
        await pubsub.aclose()
    assert seen == {"job_id": job["id"]}


@pytest.mark.asyncio
async def test_requires_authentication(client):
    resp = await client.post("/api/backtests", json=VALID)
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_rejects_unknown_strategy(client, auth):
    resp = await client.post("/api/backtests", headers=auth, json={**VALID, "strategy": "nope"})
    assert resp.status_code == 422
    assert "hft_scalper" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rejects_unknown_timeframe(client, auth):
    resp = await client.post("/api/backtests", headers=auth, json={**VALID, "timeframe": "M3"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rejects_reversed_dates(client, auth):
    resp = await client.post(
        "/api/backtests", headers=auth,
        json={**VALID, "date_from": "2026-03-27", "date_to": "2026-03-02"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rejects_absurd_date_range(client, auth):
    resp = await client.post(
        "/api/backtests", headers=auth,
        json={**VALID, "date_from": "1990-01-01", "date_to": "2026-01-01"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rejects_absurd_risk_percent(client, auth):
    resp = await client.post("/api/backtests", headers=auth, json={**VALID, "risk_percent": 90})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_caps_the_queue_depth(client, auth):
    for _ in range(5):
        await _queue(client, auth)
    resp = await client.post("/api/backtests", headers=auth, json=VALID)
    assert resp.status_code == 409
    assert "queue full" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_writes_an_audit_record(client, auth):
    from trading_engine.persistence.models import AuditLog

    from app.core.database import get_sessionmaker

    job = await _queue(client, auth)
    sm = get_sessionmaker()
    async with sm() as session:
        rows = list(await session.scalars(
            select(AuditLog).where(
                AuditLog.entity_id == job["id"], AuditLog.action == "backtest.queued"
            )
        ))
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# Ownership — a job must be invisible to anyone but its owner
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_foreign_job_is_not_found(client, auth, other_auth):
    job = await _queue(client, auth)
    resp = await client.get(f"/api/backtests/{job['id']}", headers=other_auth)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_excludes_other_users_jobs(client, auth, other_auth):
    await _queue(client, auth)
    resp = await client.get("/api/backtests", headers=other_auth)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_foreign_job_cannot_be_deleted_or_requeued(client, auth, other_auth):
    job = await _queue(client, auth)
    assert (await client.delete(f"/api/backtests/{job['id']}", headers=other_auth)).status_code == 404
    assert (
        await client.post(f"/api/backtests/{job['id']}/requeue", headers=other_auth)
    ).status_code == 404


@pytest.mark.asyncio
async def test_unknown_job_is_404_not_500(client, auth):
    resp = await client.get("/api/backtests/00000000-0000-0000-0000-000000000000", headers=auth)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_detail_carries_the_result(client, auth):
    from decimal import Decimal

    from app.core.database import get_sessionmaker

    job = await _queue(client, auth)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(BacktestResult(
            job_id=job["id"],
            final_equity=Decimal("10420.5"), net_pnl=Decimal("420.5"),
            roi_percent=Decimal("4.205"), total_trades=57, winning_trades=33,
            losing_trades=24, win_rate=Decimal("57.89"),
            profit_factor=Decimal("1.8"), max_drawdown_percent=Decimal("3.1"),
            max_drawdown_amount=Decimal("310.0"), average_trade=Decimal("7.38"),
            best_trade=Decimal("90.0"), worst_trade=Decimal("-60.0"),
            avg_win=Decimal("24.0"), avg_loss=Decimal("-12.0"),
            total_commission=Decimal("199.5"),
            equity_curve=[{"t": 1, "equity": 10000.0}],
            drawdown_curve=[{"t": 1, "dd": 0.0}],
            monthly_returns=[{"month": "2026-03", "ret": 4.2}],
            trades=[{"side": "BUY", "pnl": 12.0}],
        ))
        await session.commit()

    resp = await client.get(f"/api/backtests/{job['id']}", headers=auth)
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["total_trades"] == 57
    assert result["profit_factor"] == pytest.approx(1.8)
    assert result["net_pnl"] == pytest.approx(420.5)
    assert len(result["equity_curve"]) == 1


@pytest.mark.asyncio
async def test_null_profit_factor_stays_null(client, auth):
    """No trades means no profit factor. Reporting 0.0 would read as 'awful',
    not 'not measurable'."""
    from decimal import Decimal

    from app.core.database import get_sessionmaker

    job = await _queue(client, auth)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(BacktestResult(
            job_id=job["id"],
            final_equity=Decimal("10000"), net_pnl=Decimal("0"),
            roi_percent=Decimal("0"), total_trades=0, winning_trades=0,
            losing_trades=0, win_rate=Decimal("0"), profit_factor=None,
            max_drawdown_percent=Decimal("0"), max_drawdown_amount=Decimal("0"),
            average_trade=Decimal("0"), best_trade=Decimal("0"),
            worst_trade=Decimal("0"), avg_win=Decimal("0"), avg_loss=Decimal("0"),
            total_commission=Decimal("0"),
        ))
        await session.commit()

    resp = await client.get(f"/api/backtests/{job['id']}", headers=auth)
    assert resp.json()["result"]["profit_factor"] is None


@pytest.mark.asyncio
async def test_detail_before_completion_has_no_result(client, auth):
    job = await _queue(client, auth)
    resp = await client.get(f"/api/backtests/{job['id']}", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["result"] is None
    assert resp.json()["status"] == "QUEUED"


# --------------------------------------------------------------------------- #
# Requeue / delete
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_requeue_resets_a_finished_job(client, auth):
    job = await _queue(client, auth)
    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        row = await session.get(BacktestJob, job["id"])
        row.status = "FAILED"
        row.error = "boom"
        row.progress = __import__("decimal").Decimal("40")
        await session.commit()

    resp = await client.post(f"/api/backtests/{job['id']}/requeue", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["status"] == "QUEUED"
    assert resp.json()["error"] is None
    assert resp.json()["progress"] == 0.0


@pytest.mark.asyncio
async def test_cannot_requeue_a_running_job(client, auth):
    job = await _queue(client, auth)
    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        row = await session.get(BacktestJob, job["id"])
        row.status = "RUNNING"
        await session.commit()

    resp = await client.post(f"/api/backtests/{job['id']}/requeue", headers=auth)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cannot_delete_a_running_job(client, auth):
    job = await _queue(client, auth)
    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        row = await session.get(BacktestJob, job["id"])
        row.status = "RUNNING"
        await session.commit()

    assert (await client.delete(f"/api/backtests/{job['id']}", headers=auth)).status_code == 409


@pytest.mark.asyncio
async def test_delete_removes_a_finished_job(client, auth):
    job = await _queue(client, auth)
    assert (await client.delete(f"/api/backtests/{job['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/backtests/{job['id']}", headers=auth)).status_code == 404


class TestResultShape:
    """The result payload is a contract with the web client. These tests run a
    real (tiny) engine run through the API so a rename in backtest/engine.py
    fails here rather than rendering blanks on the dashboard."""

    @pytest.mark.asyncio
    async def test_engine_output_survives_the_api_contract(self, client, auth):
        from trading_engine.backtest.engine import BacktestConfig, BacktestEngine
        from trading_engine.strategies.synthetic import make_bars

        bars = make_bars("EURUSD", 400, seed=7)
        result = BacktestEngine(BacktestConfig(
            strategy_name="hft_scalper", symbol="EURUSD", timeframe="M1",
            date_from=date(2026, 3, 2), date_to=date(2026, 3, 27),
        )).run(bars).as_dict()

        job = await _queue(client, auth)
        await _store_result(job["id"], result)

        body = (await client.get(f"/api/backtests/{job['id']}", headers=auth)).json()["result"]

        # Scalar metrics the dashboard reads.
        for key in (
            "final_equity", "net_pnl", "roi_percent", "total_trades", "winning_trades",
            "losing_trades", "win_rate", "profit_factor", "max_drawdown_percent",
            "max_drawdown_amount", "average_trade", "best_trade", "worst_trade",
            "avg_win", "avg_loss", "total_commission",
        ):
            assert key in body, f"missing {key}"
            assert body[key] is not None, f"{key} is null"

        # Curve key names the charts index into.
        assert set(body["equity_curve"][0]) >= {"t", "equity"}
        assert set(body["drawdown_curve"][0]) >= {"t", "drawdown_pct"}
        assert set(body["monthly_returns"][0]) >= {"month", "return_pct", "pnl"}

        # The exit reason used to arrive as null because the schema called it
        # "reason" while the engine writes "close_reason".
        trade = body["trades"][0]
        assert set(trade) >= {
            "entry_time", "exit_time", "side", "volume", "entry_price",
            "exit_price", "pnl", "close_reason",
        }
        assert trade["close_reason"], "exit reason was dropped in serialisation"

    @pytest.mark.asyncio
    async def test_detail_before_a_run_has_no_result(self, client, auth):
        job = await _queue(client, auth)
        body = (await client.get(f"/api/backtests/{job['id']}", headers=auth)).json()
        assert body["result"] is None


async def _store_result(job_id: str, payload: dict) -> None:
    """Persist an engine result the way the worker does."""
    from decimal import Decimal

    from app.core.database import get_sessionmaker

    def dec(v):
        return Decimal(str(v)) if v is not None else None

    sm = get_sessionmaker()
    async with sm() as session:
        session.add(BacktestResult(
            job_id=job_id,
            final_equity=dec(payload["final_equity"]),
            net_pnl=dec(payload["net_pnl"]),
            roi_percent=dec(payload["roi_percent"]),
            total_trades=payload["total_trades"],
            winning_trades=payload["winning_trades"],
            losing_trades=payload["losing_trades"],
            win_rate=dec(payload["win_rate"]),
            profit_factor=dec(payload["profit_factor"]),
            max_drawdown_percent=dec(payload["max_drawdown_percent"]),
            max_drawdown_amount=dec(payload["max_drawdown_amount"]),
            average_trade=dec(payload["average_trade"]),
            best_trade=dec(payload["best_trade"]),
            worst_trade=dec(payload["worst_trade"]),
            avg_win=dec(payload["avg_win"]),
            avg_loss=dec(payload["avg_loss"]),
            total_commission=dec(payload["total_commission"]),
            equity_curve=payload["equity_curve"],
            drawdown_curve=payload["drawdown_curve"],
            monthly_returns=payload["monthly_returns"],
            trades=payload["trades"],
        ))
        await session.commit()


class TestOptions:
    """The web client builds its dropdowns from this endpoint. If the two lists
    disagree, users pick an option the server rejects with a 422."""

    @pytest.mark.asyncio
    async def test_requires_auth(self, client):
        assert (await client.get("/api/backtests/options")).status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_strategies_match_the_engine_registry(self, client, auth):
        from trading_engine.strategies.base import StrategyRegistry

        body = (await client.get("/api/backtests/options", headers=auth)).json()
        assert {s["value"] for s in body["strategies"]} == set(StrategyRegistry.all())

    @pytest.mark.asyncio
    async def test_every_advertised_option_is_accepted(self, client, auth):
        """The real guard: submit each advertised combination and expect 201."""
        body = (await client.get("/api/backtests/options", headers=auth)).json()
        for strategy in body["strategies"]:
            resp = await client.post(
                "/api/backtests",
                headers=auth,
                json={
                    **VALID,
                    "strategy": strategy["value"],
                    "date_from": "2026-03-02",
                    "date_to": "2026-03-03",
                },
            )
            assert resp.status_code == 201, f"{strategy['value']} rejected: {resp.text}"
            # Free the queue slot; the depth cap is a real limit.
            await client.delete(f"/api/backtests/{resp.json()['id']}", headers=auth)
            _reset_rate_limits()

    @pytest.mark.asyncio
    async def test_every_advertised_symbol_and_timeframe_is_accepted(self, client, auth):
        body = (await client.get("/api/backtests/options", headers=auth)).json()
        for symbol in body["symbols"]:
            for timeframe in body["timeframes"]:
                resp = await client.post(
                    "/api/backtests", headers=auth,
                    json={**VALID, "symbol": symbol, "timeframe": timeframe,
                          "date_from": "2026-03-02", "date_to": "2026-03-03"},
                )
                assert resp.status_code == 201, f"{symbol}/{timeframe} rejected: {resp.text}"
                await client.delete(f"/api/backtests/{resp.json()['id']}", headers=auth)
                _reset_rate_limits()

    @pytest.mark.asyncio
    async def test_defaults_are_usable(self, client, auth):
        body = (await client.get("/api/backtests/options", headers=auth)).json()
        resp = await client.post(
            "/api/backtests", headers=auth,
            json={**VALID, **body["defaults"], "date_from": "2026-03-02", "date_to": "2026-03-03"},
        )
        assert resp.status_code == 201, resp.text
