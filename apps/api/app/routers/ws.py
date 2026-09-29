"""Realtime event stream over WebSocket.

The API does not poll anything: the trading engine publishes domain events to
Redis and this endpoint relays them to connected browsers, filtered to the
authenticated user's own bots and trades.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_sessionmaker
from app.core.redis_bus import subscribe_events
from app.core.security import decode_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["realtime"])

HEARTBEAT_SECONDS = 25.0


async def _authenticate(token: str | None) -> str | None:
    """Return the user id for a valid token, else None."""
    if not token:
        return None
    try:
        payload = decode_token(token, expected="access", settings=get_settings())
    except Exception:
        return None
    return payload.get("sub")


@router.websocket("/ws")
async def events_socket(websocket: WebSocket) -> None:
    """Stream engine events for the authenticated user.

    The token is taken from the ``?token=`` query parameter because browsers
    cannot set headers on a WebSocket handshake.
    """
    user_id = await _authenticate(websocket.query_params.get("token"))
    if user_id is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await websocket.accept()
    logger.info("WebSocket opened for user %s", user_id)

    async def _heartbeat() -> None:
        # Without this, idle proxies close the socket and the client reconnects
        # in a loop.
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await websocket.send_json({"type": "ping", "ts": None})

    async def _is_owner(bot_id: str | None, subject: str | None) -> bool:
        """Only relay events belonging to this user.

        Three independent checks, because any one alone leaks: a user-scoped
        event with a foreign ``subject`` must be dropped, a bot event with a
        foreign ``bot_id`` must be dropped even if the subject is absent, and
        a ``user_id`` buried in the payload must count even when the envelope
        omits it.
        """
        if subject and subject != user_id:
            return False
        payload_subject = event_payload.get("user_id")
        if payload_subject and payload_subject != user_id:
            return False
        if not bot_id:
            # Account-level event with no owner field: only safe if the engine
            # did not scope it to another user.
            return True
        from trading_engine.persistence.models import Bot

        async with get_sessionmaker()() as session:
            return bool(
                await session.scalar(
                    select(Bot.id).where(Bot.id == bot_id, Bot.user_id == user_id)
                )
            )

    hb = asyncio.create_task(_heartbeat())
    event_payload: dict = {}
    try:
        async for event in subscribe_events():
            # Payload is re-read on every iteration; _is_owner closes over it.
            event_payload = event.get("payload") or {}
            if not await _is_owner(event.get("bot_id"), event.get("user_id")):
                continue
            await websocket.send_json(event)
    except WebSocketDisconnect:
        logger.info("WebSocket closed for user %s", user_id)
    except Exception as exc:  # pragma: no cover - transport failures
        logger.warning("WebSocket relay error: %s", exc)
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb
        with contextlib.suppress(Exception):
            await websocket.close()
