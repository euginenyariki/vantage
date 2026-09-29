"""Broker connection management.

Credentials are encrypted on write and never returned by any endpoint. The
connection test runs the real MetaApi adapter in-process, so a user learns
whether their MT4/MT5 credentials work before a bot is ever pointed at them.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.database import get_session
from app.core.redis_bus import publish_bot_command
from app.dependencies import get_app_settings, get_current_user, rate_limit
from app.schemas import (
    BrokerConnectionCreate, BrokerConnectionOut, ConnectionTestResult,
)
from trading_engine.persistence.models import AuditLog, BrokerConnection, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/brokers", tags=["brokers"])

_TEST_LIMIT = rate_limit("broker-test", limit=10, window_seconds=300)
_CREATE_LIMIT = rate_limit("broker-create", limit=10, window_seconds=3600)


def _mask_login(login: str) -> str:
    if len(login) <= 3:
        return "*" * len(login)
    return f"{'*' * (len(login) - 3)}{login[-3:]}"


def _to_out(conn: BrokerConnection) -> BrokerConnectionOut:
    return BrokerConnectionOut(
        id=conn.id,
        name=conn.name,
        broker=conn.broker,
        is_demo=bool(conn.is_demo),
        masked_login=_mask_login(decrypt_secret(conn.login_encrypted or "")),
        connection_state=conn.connection_state,
        last_error=conn.last_error,
        last_synced_at=conn.last_synced_at,
        balance=float(conn.balance) if conn.balance is not None else None,
        equity=float(conn.equity) if conn.equity is not None else None,
        currency=conn.currency or "USD",
        created_at=conn.created_at,
    )


async def _get_conn(session: AsyncSession, user_id: str, conn_id: str) -> BrokerConnection:
    conn = await session.scalar(
        select(BrokerConnection).where(
            BrokerConnection.id == conn_id, BrokerConnection.user_id == user_id
        )
    )
    if conn is None:
        raise HTTPException(status_code=404, detail="Broker connection not found")
    return conn


@router.get("", response_model=list[BrokerConnectionOut])
async def list_connections(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[BrokerConnectionOut]:
    rows = await session.scalars(
        select(BrokerConnection)
        .where(BrokerConnection.user_id == user.id)
        .order_by(BrokerConnection.created_at.desc())
    )
    return [_to_out(c) for c in rows]


@router.post("", response_model=BrokerConnectionOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(_CREATE_LIMIT)])
async def create_connection(
    payload: BrokerConnectionCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> BrokerConnectionOut:
    """Store an encrypted MT4/MT5 credential set.

    Demo accounts are the default and live accounts are not blocked outright,
    but an ``is_demo=False`` connection is only ever used by a bot that has an
    explicit live grant, so credentials sitting here are inert on their own.
    """
    existing = await session.scalar(
        select(BrokerConnection).where(
            BrokerConnection.user_id == user.id,
            BrokerConnection.name == payload.name,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="A connection with that name exists")

    conn = BrokerConnection(
        user_id=user.id,
        name=payload.name,
        broker=payload.broker,
        is_demo=payload.is_demo,
        login_encrypted=encrypt_secret(payload.login),
        password_encrypted=encrypt_secret(payload.password),
        server_encrypted=encrypt_secret(payload.server),
        key_version=str(settings.credential_version_number),
        connection_state="DISCONNECTED",
        currency=payload.currency,
    )
    session.add(conn)
    await session.flush()
    session.add(
        AuditLog(
            user_id=user.id, actor_email=user.email, action="broker.connection_created",
            entity_type="broker_connection", entity_id=conn.id,
            details={"broker": payload.broker, "is_demo": payload.is_demo},
        )
    )
    await session.commit()
    await session.refresh(conn)
    logger.info("Broker connection %s created for user %s (demo=%s)",
                conn.id, user.id, payload.is_demo)
    return _to_out(conn)


@router.get("/{conn_id}", response_model=BrokerConnectionOut)
async def get_connection(
    conn_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BrokerConnectionOut:
    return _to_out(await _get_conn(session, user.id, conn_id))


@router.delete("/{conn_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    conn_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    conn = await _get_conn(session, user.id, conn_id)

    # Refuse if a bot still points at it, otherwise starting that bot later
    # would fail in a much more confusing way.
    from trading_engine.persistence.models import Bot

    in_use = await session.scalar(
        select(Bot.id).where(Bot.broker_connection_id == conn_id).limit(1)
    )
    if in_use:
        raise HTTPException(
            status_code=409,
            detail="This connection is used by one or more bots. Detach it first.",
        )

    await session.delete(conn)
    await session.commit()
    logger.info("Broker connection %s deleted by %s", conn_id, user.id)


@router.post("/{conn_id}/test", response_model=ConnectionTestResult,
             dependencies=[Depends(_TEST_LIMIT)])
async def test_connection(
    conn_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> ConnectionTestResult:
    """Authenticate against MetaApi and read the account snapshot.

    Uses the sandbox API URL unless production has explicitly pointed
    ``METAAPI_API_URL`` at the live endpoint.
    """
    conn = await _get_conn(session, user.id, conn_id)

    if not settings.metaapi_token:
        conn.connection_state = "ERROR"
        conn.last_error = "MetaApi is not configured on this server (METAAPI_TOKEN unset)"
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail="Broker integration is not configured on this server yet",
        )

    from trading_engine.brokers.metaapi import MetaApiAdapter
    from trading_engine.core.types import TradingMode

    adapter = MetaApiAdapter(
        token=settings.metaapi_token,
        api_url=settings.metaapi_api_url,
        account_login=int(decrypt_secret(conn.login_encrypted or "") or 0),
        account_password=decrypt_secret(conn.password_encrypted or ""),
        account_server=decrypt_secret(conn.server_encrypted or ""),
        live_allowed=not conn.is_demo and settings.live_trading_enabled,
        mode=TradingMode.DEMO if conn.is_demo else TradingMode.LIVE,
    )

    started = time.perf_counter()
    try:
        await adapter.connect()
        snapshot = await adapter.get_account()
    except Exception as exc:
        latency = (time.perf_counter() - started) * 1000
        conn.connection_state = "ERROR"
        conn.last_error = f"{type(exc).__name__}: {exc}"[:500]
        await session.commit()
        logger.warning("Broker test failed for %s: %s", conn_id, exc)
        return ConnectionTestResult(
            ok=False, message=_friendly_broker_error(exc), latency_ms=round(latency, 1)
        )
    finally:
        await adapter.disconnect()

    latency = (time.perf_counter() - started) * 1000
    conn.connection_state = "CONNECTED"
    conn.last_error = None
    conn.balance = _to_numeric(snapshot.balance)
    conn.equity = _to_numeric(snapshot.equity)
    conn.currency = snapshot.currency or conn.currency
    conn.last_synced_at = datetime.now(timezone.utc)
    await session.commit()

    return ConnectionTestResult(
        ok=True,
        message="Connected",
        balance=snapshot.balance,
        equity=snapshot.equity,
        currency=snapshot.currency,
        latency_ms=round(latency, 1),
    )


@router.post("/{conn_id}/disconnect", response_model=BrokerConnectionOut)
async def disconnect(
    conn_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BrokerConnectionOut:
    conn = await _get_conn(session, user.id, conn_id)
    await publish_bot_command("disconnect_broker", user_id=user.id, connection_id=conn.id)
    conn.connection_state = "DISCONNECTED"
    await session.commit()
    return _to_out(conn)


# BrokerConnection.balance/equity are Numeric(18, 4).
_MONEY = Decimal("0.0001")


def _to_numeric(value: float | int | Decimal | None) -> Decimal | None:
    """Convert broker-reported money to the stored Numeric(18, 4) scale.

    ``str()`` is used on purpose: ``Decimal(0.1)`` captures binary floating
    point noise, while ``Decimal(str(0.1))`` captures the reported figure.
    """
    if value is None:
        return None
    return Decimal(str(value)).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _friendly_broker_error(exc: Exception) -> str:
    """Map broker exceptions to something a user can act on."""
    from trading_engine.brokers.base import (
        BrokerAuthError, BrokerConnectionError, BrokerRejectedError,
        BrokerTimeoutError, SymbolNotFoundError,
    )

    if isinstance(exc, BrokerAuthError):
        return "Authentication failed. Check the login, password, and server name."
    if isinstance(exc, SymbolNotFoundError):
        return "Symbol not available on this account."
    if isinstance(exc, BrokerTimeoutError):
        return "The broker did not respond in time. Try again shortly."
    if isinstance(exc, BrokerRejectedError):
        return f"The broker rejected the request: {exc}"
    if isinstance(exc, BrokerConnectionError):
        return f"Could not reach the broker: {exc}"
    return f"Unexpected error: {type(exc).__name__}"
