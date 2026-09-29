"""WebSocket event filtering.

The socket is authenticated, but authentication alone is not enough: events must
also be filtered so one user never receives another user's bot activity.

The real route handler is driven directly with a fake socket and a stubbed event
source, which keeps the production filtering code under test without depending on
loop-bound pools or live pub/sub timing.
"""

from datetime import datetime, timezone

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.core.security import create_access_token
from trading_engine.persistence.models import Bot, User


def _token(user_id: str) -> str:
    return create_access_token(user_id, settings=get_settings())


async def _seed() -> tuple[str, str, str]:
    """A user owning one bot, plus a second user who owns nothing."""
    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        now = datetime.now(timezone.utc)
        owner = User(email="ws-owner@example.com", password_hash="x", name="Owner",
                     country="US", terms_accepted_at=now)
        other = User(email="ws-other@example.com", password_hash="x", name="Other",
                     country="US", terms_accepted_at=now)
        session.add_all([owner, other])
        await session.flush()
        bot = Bot(user_id=owner.id, name="Owned", symbol="EURUSD",
                  strategy="hft_scalper", timeframe="M1", mode="PAPER")
        session.add(bot)
        await session.commit()
        return owner.id, other.id, bot.id


class FakeWebSocket:
    """Minimal stand-in for starlette's WebSocket."""

    def __init__(self, token: str | None):
        self.query_params: dict[str, str] = {"token": token} if token else {}
        self.sent: list[dict] = []
        self.accepted = False
        self.closed: int | None = None
        self._accept_error: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = code


@pytest.mark.asyncio
async def test_websocket_rejects_a_missing_token(monkeypatch):
    from app.routers import ws as ws_module

    monkeypatch.setattr(ws_module, "subscribe_events", _no_events)
    socket = FakeWebSocket(None)
    await ws_module.events_socket(socket)  # type: ignore[arg-type]

    assert socket.closed == 4401
    assert socket.accepted is False
    assert socket.sent == []


@pytest.mark.asyncio
async def test_websocket_rejects_a_bad_token(monkeypatch):
    from app.routers import ws as ws_module

    monkeypatch.setattr(ws_module, "subscribe_events", _no_events)
    socket = FakeWebSocket("garbage.token.here")
    await ws_module.events_socket(socket)  # type: ignore[arg-type]

    assert socket.closed == 4401
    assert socket.sent == []


@pytest.mark.asyncio
async def test_websocket_relays_own_events_and_drops_foreign_ones(monkeypatch):
    """The core guarantee: no cross-user leakage on the realtime channel."""
    from app.routers import ws as ws_module

    owner_id, other_id, bot_id = await _seed()

    events = [
        # Own bot: must be relayed.
        {"type": "bot.state", "bot_id": bot_id, "user_id": owner_id, "data": {"state": "RUNNING"}},
        # Own user, but somebody else's bot: must be dropped.
        {"type": "bot.state", "bot_id": "00000000-0000-0000-0000-000000000000",
         "user_id": owner_id, "data": {}},
        # Another user's account event: must be dropped.
        {"type": "account.balance", "bot_id": None, "user_id": other_id, "data": {}},
        # Own account-level event: must be relayed.
        {"type": "account.balance", "bot_id": None, "user_id": owner_id, "data": {"balance": 1}},
    ]
    monkeypatch.setattr(ws_module, "subscribe_events", lambda: _events(events))

    socket = FakeWebSocket(_token(owner_id))
    await ws_module.events_socket(socket)  # type: ignore[arg-type]

    assert socket.accepted is True
    relayed = [m for m in socket.sent if m.get("type") != "ping"]

    # Exactly the two own events.
    assert len(relayed) == 2, f"expected 2 relayed events, got {relayed}"
    assert relayed[0]["bot_id"] == bot_id
    assert relayed[0]["user_id"] == owner_id
    assert relayed[1]["user_id"] == owner_id

    # Nothing belonging to another user leaked through.
    assert all(m.get("user_id") == owner_id for m in relayed)
    assert all(m.get("bot_id") in (bot_id, None) for m in relayed)


@pytest.mark.asyncio
async def test_websocket_drops_bot_events_with_no_subject_and_unknown_bot(monkeypatch):
    """A bot event that cannot be tied to the user must not be relayed."""
    from app.routers import ws as ws_module

    owner_id, _other_id, _bot_id = await _seed()
    events = [
        # bot_id present, no user_id, and not the user's bot: drop.
        {"type": "bot.state", "bot_id": "11111111-1111-1111-1111-111111111111",
         "user_id": None, "data": {}},
    ]
    monkeypatch.setattr(ws_module, "subscribe_events", lambda: _events(events))

    socket = FakeWebSocket(_token(owner_id))
    await ws_module.events_socket(socket)  # type: ignore[arg-type]

    relayed = [m for m in socket.sent if m.get("type") != "ping"]
    assert relayed == []


async def _events(items: list[dict]):
    for item in items:
        yield item
    raise WebSocketDisconnect()


async def _no_events():
    raise WebSocketDisconnect()
    yield  # pragma: no cover - makes this an async generator


@pytest.mark.asyncio
async def test_websocket_drops_foreign_backtest_results(monkeypatch):
    """Backtest events carry no bot_id, so the only thing tying them to a
    tenant is user_id. A result published with a foreign subject must never
    reach another browser — it is their P&L data."""
    from app.routers import ws as ws_module

    owner_id, other_id, _bot_id = await _seed()

    events = [
        # Own backtest progress: relayed.
        {"type": "backtest.progress", "bot_id": None, "user_id": owner_id,
         "payload": {"job_id": "j1", "status": "RUNNING", "progress": 40.0}},
        # Somebody else's finished backtest: dropped.
        {"type": "backtest.progress", "bot_id": None, "user_id": other_id,
         "payload": {"job_id": "j2", "status": "COMPLETED", "net_pnl": 4200.0}},
    ]
    monkeypatch.setattr(ws_module, "subscribe_events", lambda: _events(events))

    socket = FakeWebSocket(_token(owner_id))
    await ws_module.events_socket(socket)  # type: ignore[arg-type]

    relayed = [m for m in socket.sent if m.get("type") != "ping"]
    assert len(relayed) == 1, f"expected 1 relayed event, got {relayed}"
    assert relayed[0]["payload"]["job_id"] == "j1"
    assert relayed[0]["user_id"] == owner_id


@pytest.mark.asyncio
async def test_websocket_drops_backtest_scoped_only_in_the_payload(monkeypatch):
    """A backtest event that omits the envelope user_id but names a foreign
    owner inside the payload must still be dropped."""
    from app.routers import ws as ws_module

    owner_id, other_id, _bot_id = await _seed()

    events = [
        {"type": "backtest.progress", "bot_id": None, "user_id": None,
         "payload": {"job_id": "j3", "user_id": other_id}},
    ]
    monkeypatch.setattr(ws_module, "subscribe_events", lambda: _events(events))

    socket = FakeWebSocket(_token(owner_id))
    await ws_module.events_socket(socket)  # type: ignore[arg-type]

    relayed = [m for m in socket.sent if m.get("type") != "ping"]
    assert relayed == []


def test_engine_event_puts_user_id_on_the_envelope():
    """Regression: user_id was passed as a payload key, so backtest events
    arrived with user_id=None and were broadcast to every connected socket."""
    from trading_engine.execution.events import EngineEvent

    event = EngineEvent.backtest("job-1", "RUNNING", user_id="user-1", progress=10.0)

    assert event.user_id == "user-1"
    assert "user_id" not in event.payload
    assert event.payload["job_id"] == "job-1"
    assert event.payload["progress"] == 10.0


def test_engine_event_cannot_pretend_user_id_is_a_payload_field():
    """The field is keyword-only, so it cannot be smuggled back inside
    **payload and shadow the envelope value."""
    import inspect

    from trading_engine.execution.events import EngineEvent

    params = inspect.signature(EngineEvent.backtest).parameters
    assert params["user_id"].kind is inspect.Parameter.KEYWORD_ONLY

