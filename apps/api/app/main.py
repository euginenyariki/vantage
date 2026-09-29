"""FastAPI application factory and lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import assert_production_ready, get_settings
from app.core.database import dispose_database, init_database
from app.core.redis_bus import close_redis, ping_redis
from app.routers import (
    admin,
    analytics,
    auth,
    backtest,
    bots,
    brokers,
    health,
    market,
    positions,
    trading,
    ws,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api"


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.is_production:
        assert_production_ready(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=logging.DEBUG if settings.debug else logging.INFO,
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        )
        init_database()
        try:
            from app.core.database import get_sessionmaker

            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1"))
            logger.info("Database reachable")
        except Exception as exc:
            # Not fatal: /ready will report degraded and the orchestrator can
            # wait for PostgreSQL to finish starting.
            logger.error("Database not reachable at startup: %s", exc)
        logger.info("Redis reachable: %s", await ping_redis())
        logger.info(
            "Vantage API %s starting (env=%s mode=%s live=%s)",
            settings.api_version, settings.environment,
            settings.trading_mode, settings.live_trading_enabled,
        )
        yield
        await close_redis()
        await dispose_database()
        logger.info("Vantage API stopped")

    app = FastAPI(
        title=settings.api_title,
        version=settings.api_version,
        description=(
            "REST and realtime API for the Vantage automated trading platform. "
            "All order handling is delegated to the trading engine; the API "
            "never places orders itself."
        ),
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
        expose_headers=["X-Request-ID"],
    )

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(analytics.router, prefix=API_PREFIX)
    app.include_router(admin.router, prefix=API_PREFIX)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(backtest.router, prefix=API_PREFIX)
    app.include_router(bots.router, prefix=API_PREFIX)
    app.include_router(brokers.router, prefix=API_PREFIX)
    app.include_router(positions.router, prefix=API_PREFIX)
    app.include_router(trading.router, prefix=API_PREFIX)
    app.include_router(market.router, prefix=API_PREFIX)
    app.include_router(ws.router, prefix=API_PREFIX)

    @app.exception_handler(ValueError)
    async def value_error_handler(request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.api_title,
            "version": settings.api_version,
            "docs": f"{API_PREFIX}/docs",
            "health": f"{API_PREFIX}/health",
        }

    return app


app = create_app()
