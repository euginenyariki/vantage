"""Redis connection and the event/command bridge to the trading engine.

The engine runs as a separate process. It publishes every domain event on
``vantage:events`` and accepts control commands on ``vantage:bot:commands``;
this module is the only place that knows those channel names.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CHANNEL_EVENTS = "vantage:events"
CHANNEL_BOT = "vantage:bot:commands"
CHANNEL_BACKTEST = "vantage:backtest:jobs"

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def ping_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # pragma: no cover - depends on infra
        logger.warning("Redis ping failed: %s", exc)
        return False


async def publish_bot_command(action: str, *, bot_id: str | None = None,
                              user_id: str | None = None, **payload: Any) -> None:
    """Send a control command to the trading engine.

    Commands are fire-and-forget: the engine acknowledges over the event
    channel, so callers should not treat a successful publish as confirmation
    that the bot acted.
    """
    message = {"action": action, "bot_id": bot_id, "user_id": user_id, **payload}
    await get_redis().publish(CHANNEL_BOT, json.dumps(message, default=str))
    logger.info("Published bot command %s for bot=%s", action, bot_id)


async def subscribe_events():
    """Async iterator over engine events as parsed dicts."""
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(CHANNEL_EVENTS)
    try:
        async for raw in pubsub.listen():
            if raw.get("type") != "message":
                continue
            data = raw.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", "replace")
            try:
                yield json.loads(data)
            except (TypeError, ValueError):
                logger.debug("Dropping malformed event: %r", data)
    finally:
        try:
            await pubsub.unsubscribe(CHANNEL_EVENTS)
            await pubsub.aclose()
        except Exception:  # pragma: no cover
            pass
