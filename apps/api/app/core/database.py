"""Database engine and session management.

The API owns its own SQLAlchemy engine (separate from the trading engine's
process-local one) and imports the shared ORM models from the trading engine so
both processes read and write the same ``vantage`` schema.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_database(url: str | None = None) -> async_sessionmaker[AsyncSession]:
    """Create the global engine/sessionmaker. Idempotent."""
    global _engine, _sessionmaker
    if _sessionmaker is not None:
        return _sessionmaker
    dsn = url or get_settings().database_url
    _engine = create_async_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
        echo=False,
        future=True,
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    logger.info("Database engine initialised")
    return _sessionmaker


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        return init_database()
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session with rollback-on-error semantics."""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_database() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed")
    _engine = None
    _sessionmaker = None
