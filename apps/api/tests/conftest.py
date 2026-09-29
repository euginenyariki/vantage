"""Shared fixtures for the API test suite.

The whole app is pointed at an isolated ``vantage_test`` database and a separate
Redis logical DB, so tests exercise the real dependency graph (routers, ORM
models, session handling) rather than mocked stubs. Only ``METAAPI_TOKEN`` and
the broker HTTP call itself are avoided: no test may talk to a real broker.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

# --- environment must be set before any app module is imported --------------- #
_ADMIN = os.environ.get("VANTAGE_TEST_DB_ADMIN", "postgresql://vantage:vantage@127.0.0.1:5432")
TEST_DB = os.environ.get("VANTAGE_TEST_DB", "vantage_test")

os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = f"postgresql+asyncpg://vantage:vantage@127.0.0.1:5432/{TEST_DB}"
os.environ["REDIS_URL"] = os.environ.get("VANTAGE_TEST_REDIS", "redis://127.0.0.1:6379/1")
os.environ["JWT_SECRET"] = "test-jwt-secret-that-is-long-enough-to-pass-validation"
os.environ["REFRESH_PEPPER"] = "test-refresh-pepper-that-is-long-enough-x"
os.environ["CREDENTIAL_MASTER_KEY"] = "test-credential-master-key-0123456789"
os.environ["CREDENTIAL_KEY_VERSION"] = "1"
os.environ["METAAPI_TOKEN"] = ""  # keeps the broker test on the 503 path
os.environ["TRADING_MODE"] = "PAPER"
os.environ["LIVE_TRADING_ENABLED"] = "false"
os.environ["DEBUG"] = "false"

import asyncpg
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from trading_engine.persistence.models import Base


def _dsn(db: str) -> str:
    return _ADMIN + "/" + db


async def _bootstrap() -> None:
    """Create the test database and schema once, in a throwaway event loop.

    Done at import time on purpose: the app's engine is a module singleton, and
    a pooled engine cannot be shared across the per-test event loops that
    pytest-asyncio creates. Each test therefore gets a fresh engine below.
    """
    from app.core.database import dispose_database, get_sessionmaker, init_database

    conn = await asyncpg.connect(_dsn("postgres"))
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await conn.close()

    init_database()
    engine = get_sessionmaker().kw["bind"]
    async with engine.begin() as conn:
        await conn.execute(text('CREATE SCHEMA IF NOT EXISTS "vantage"'))
        await conn.run_sync(Base.metadata.create_all)
    await dispose_database()


asyncio.run(_bootstrap())


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine():
    """Bind the app's pooled clients to this test's event loop, then release."""
    from app.core.database import dispose_database, get_sessionmaker
    from app.core.redis_bus import close_redis

    await dispose_database()
    await close_redis()
    get_sessionmaker()  # create the engine inside the running loop
    yield
    await dispose_database()
    await close_redis()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Rate limiting is per-process and keyed by client IP; every test shares
    one address, so the counters must be reset between cases."""
    from app.dependencies import _buckets

    _buckets.clear()
    yield
    _buckets.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Truncate between tests so cases cannot leak state into each other."""
    from app.core.database import get_sessionmaker

    yield
    sm = get_sessionmaker()
    async with sm() as session:
        await session.execute(text(
            "TRUNCATE vantage.audit_logs, vantage.bots, vantage.user_sessions, "
            "vantage.users, vantage.broker_connections, vantage.orders, "
            "vantage.positions, vantage.trades, "
            "vantage.backtest_results, vantage.backtest_jobs, "
            "vantage.historical_bars RESTART IDENTITY CASCADE"
        ))
        await session.commit()


@pytest_asyncio.fixture
async def client():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register(client: AsyncClient, **overrides) -> dict:
    body = {
        "email": f"user-{uuid.uuid4().hex[:8]}@example.com",
        "name": "Test User",
        "password": "Str0ngPass!2026",
        "country": "US",
        "accepted_terms": True,
    }
    body.update(overrides)
    resp = await client.post("/api/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest_asyncio.fixture
async def user(client) -> dict:
    return await _register(client)


@pytest_asyncio.fixture
async def auth(client, user) -> dict:
    """Headers for an authenticated user."""
    return {"Authorization": f"Bearer {user['tokens']['access_token']}"}


@pytest_asyncio.fixture
async def other_auth(client) -> dict:
    """Headers for a second, unrelated user."""
    other = await _register(client)
    return {"Authorization": f"Bearer {other['tokens']['access_token']}"}


@pytest_asyncio.fixture
async def admin_auth(client) -> dict:
    """Headers for a user who holds the ADMIN role.

    The role is granted straight in the database and a fresh access token is
    then issued, because the role is embedded in the token at issue time and
    there is deliberately no API for self-promotion.
    """
    from sqlalchemy import select
    from trading_engine.persistence.models import Role, User

    from app.core.database import get_sessionmaker

    reg = await _register(client, name="Admin User")
    email = reg["user"]["email"]
    sm = get_sessionmaker()
    async with sm() as session:
        user = await session.scalar(select(User).where(User.email == email))
        user.role = Role.ADMIN
        await session.commit()

    login = await client.post(
        "/api/auth/login", json={"email": email, "password": "Str0ngPass!2026"}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}
