"""Liveness/readiness probe and runtime status."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_session
from app.core.redis_bus import ping_redis
from app.dependencies import get_app_settings
from app.schemas import HealthResponse

router = APIRouter(tags=["system"])


async def _db_ok(session: AsyncSession) -> bool:
    try:
        await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/health", response_model=HealthResponse)
async def health(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> HealthResponse:
    """Reports dependency health. Always 200 so probes can read the body."""
    db_ok = await _db_ok(session)
    redis_ok = await ping_redis()
    return HealthResponse(
        status="ok" if (db_ok and redis_ok) else "degraded",
        version=settings.api_version,
        environment=settings.environment,
        database=db_ok,
        redis=redis_ok,
        trading_mode=settings.trading_mode.upper(),
        live_trading_enabled=settings.live_trading_enabled,
        time=datetime.now(timezone.utc),
    )


@router.get("/ready")
async def ready(
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> dict[str, object]:
    """Readiness: 503 unless both PostgreSQL and Redis answer.

    Keep this separate from /health so an orchestrator can restart the API
    without page-reload noise from a transient Redis hiccup.
    """
    db_ok = await _db_ok(session)
    redis_ok = await ping_redis()
    ready_state = db_ok and redis_ok
    if not ready_state:
        # The docstring promises 503, so make the status line match it.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ready": ready_state,
        "database": db_ok,
        "redis": redis_ok,
        "environment": settings.environment,
    }
