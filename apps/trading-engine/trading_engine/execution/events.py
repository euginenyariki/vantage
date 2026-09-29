"""Engine events + Redis pub/sub fan-out to the WebSocket layer.

The engine never talks to the browser. It publishes JSON events to Redis; the
API process subscribes and fans them out over WebSocket. That separation means
the trading engine keeps running when the browser closes (Phase 6), and the
API can be restarted without interrupting a bot.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from trading_engine.core.types import utcnow

logger = logging.getLogger("vantage.events")

CHANNEL_EVENTS = "vantage:events"
CHANNEL_BOT = "vantage:bot:commands"
CHANNEL_BACKTEST = "vantage:backtest:jobs"


class EventType(str, Enum):
    TICK = "tick"
    ORDER = "order"
    ORDER_STATE = "order_state"
    POSITION = "position"
    POSITION_UPDATED = "position_updated"
    TRADE = "trade"
    BOT_STATE = "bot_state"
    RISK = "risk"
    ACCOUNT = "account"
    EMERGENCY = "emergency"
    BACKTEST = "backtest"
    CONNECTION = "connection"
    LOG = "log"


@dataclass
class EngineEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    bot_id: str | None = None
    user_id: str | None = None
    timestamp: str = field(default_factory=lambda: utcnow().isoformat())
    seq: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> EngineEvent:
        data = json.loads(raw)
        return cls(**data)

    # ---- typed constructors ---------------------------------------- #
    @classmethod
    def tick(cls, symbol: str, bid: float, ask: float, source: str = "SIMULATOR", **kw) -> EngineEvent:
        return cls(type=EventType.TICK.value, payload={"symbol": symbol, "bid": bid, "ask": ask, "source": source}, **kw)

    @classmethod
    def order_state(cls, *, bot_id: str, symbol: str, state: str, action: str, **kw) -> EngineEvent:
        user_id = kw.pop("user_id", None)
        return cls(
            type=EventType.ORDER_STATE.value,
            bot_id=bot_id,
            user_id=user_id,
            payload={"symbol": symbol, "state": state, "action": action, **kw},
        )

    @classmethod
    def position(cls, *, bot_id: str | None, action: str, **payload) -> EngineEvent:
        return cls(type=EventType.POSITION.value, bot_id=bot_id, payload={"action": action, **payload})

    @classmethod
    def trade_closed(cls, *, bot_id: str | None, **payload) -> EngineEvent:
        return cls(type=EventType.TRADE.value, bot_id=bot_id, payload=payload)

    @classmethod
    def bot_state(cls, *, bot_id: str, state: str, reason: str = "") -> EngineEvent:
        return cls(type=EventType.BOT_STATE.value, bot_id=bot_id, payload={"state": state, "reason": reason})

    @classmethod
    def risk(cls, *, bot_id: str, **payload) -> EngineEvent:
        return cls(type=EventType.RISK.value, bot_id=bot_id, payload=payload)

    @classmethod
    def account(cls, **payload) -> EngineEvent:
        return cls(type=EventType.ACCOUNT.value, payload=payload)

    @classmethod
    def emergency(cls, action: str, reason: str, **payload) -> EngineEvent:
        return cls(
            type=EventType.EMERGENCY.value,
            payload={"action": action, "reason": reason, **payload},
        )

    @classmethod
    def backtest(cls, job_id: str, status: str, *, user_id: str | None = None, **payload) -> EngineEvent:
        # ``user_id`` must land on the event envelope, not inside payload: the
        # WebSocket relay in the API scopes every event by the top-level
        # user_id. Leaving it in payload would make backtest events unscoped
        # and broadcast one user's results to every connected browser.
        return cls(
            type=EventType.BACKTEST.value,
            user_id=user_id,
            payload={"job_id": job_id, "status": status, **payload},
        )

    @classmethod
    def manual(cls, type_: str, payload: dict[str, Any]) -> EngineEvent:
        return cls(type=type_, payload=payload)


class EventBus:
    """Thin wrapper over Redis pub/sub. Redis is never the source of truth for
    money — every event it carries is reproducible from PostgreSQL."""

    def __init__(self, redis, channel: str = CHANNEL_EVENTS) -> None:
        self._redis = redis
        self._channel = channel
        self._seq = 0

    async def publish(self, event: EngineEvent) -> None:
        self._seq += 1
        event.seq = self._seq
        try:
            await self._redis.publish(self._channel, event.to_json())
        except Exception as exc:  # never let telemetry break trading
            logger.warning("Event publish failed: %s", exc)

    async def publish_raw(self, channel: str, message: str) -> None:
        await self._redis.publish(channel, message)

    async def subscribe(self, channel: str = CHANNEL_EVENTS) -> AsyncIterator[EngineEvent]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    yield EngineEvent.from_json(message["data"])
                except (json.JSONDecodeError, TypeError):
                    logger.debug("Dropping malformed event on %s", channel)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()


class NullEventBus:
    """Used by the backtester, which persists results rather than streaming."""

    async def publish(self, event: EngineEvent) -> None:
        return None
