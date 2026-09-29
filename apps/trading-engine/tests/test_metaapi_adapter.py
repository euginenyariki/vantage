"""MetaApi adapter mapping.

These exercise the response->domain mapping with a stubbed transport, so the
broker paths that need a real MetaApi account are still covered offline.
"""

from __future__ import annotations

import pytest

from trading_engine.brokers.metaapi import MetaApiAdapter
from trading_engine.core.types import Side


def _adapter(monkeypatch: pytest.MonkeyPatch, payload) -> MetaApiAdapter:
    adapter = MetaApiAdapter(
        token="test-token", account_login=12345, account_password="secret",
        account_server="MetaQuotes-Demo", mode="PAPER",
    )

    async def _fake_request(method: str, path: str, **kwargs):
        return payload

    # Pretend connect() already provisioned the account, so the request path
    # (which is what we are testing) can run offline.
    adapter._account_id = "acct-1"
    monkeypatch.setattr(adapter, "_request", _fake_request)
    return adapter


async def test_get_positions_maps_a_metaapi_payload(monkeypatch: pytest.MonkeyPatch):
    """Regression: Position has no ``profit`` field, so passing one raised
    TypeError and broke every real account's position reconciliation."""
    payload = {
        "positions": [
            {
                "id": "7788",
                "symbol": "eurusd",
                "side": "BUY",
                "volume": 0.5,
                "openPrice": 1.0850,
                "currentPrice": 1.0900,
                "profit": 25.0,
                "commission": -0.7,
                "swap": -1.2,
            }
        ]
    }
    adapter = _adapter(monkeypatch, payload)
    positions = await adapter.get_positions()

    assert len(positions) == 1
    pos = positions[0]
    assert pos.id == "7788"
    assert pos.symbol == "EURUSD"
    assert pos.side is Side.BUY
    assert pos.volume == pytest.approx(0.5)
    assert pos.open_price == pytest.approx(1.0850)
    assert pos.current_price == pytest.approx(1.0900)
    # MetaApi reports open P&L as "profit"; it maps to unrealized_pnl.
    assert pos.unrealized_pnl == pytest.approx(25.0)
    assert pos.commission == pytest.approx(-0.7)
    assert pos.swap == pytest.approx(-1.2)


async def test_get_positions_accepts_a_bare_list(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(
        monkeypatch,
        [{"id": "1", "symbol": "GBPUSD", "side": "SELL", "volume": 1.0,
          "openPrice": 1.25, "profit": -3.0}],
    )
    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "GBPUSD"
    assert positions[0].side is Side.SELL
    assert positions[0].unrealized_pnl == pytest.approx(-3.0)


async def test_get_positions_handles_an_empty_response(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(monkeypatch, {"positions": []})
    assert await adapter.get_positions() == []


async def test_get_positions_tolerates_missing_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = _adapter(monkeypatch, {"positions": [{"symbol": "USDJPY"}]})
    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].unrealized_pnl == pytest.approx(0.0)
    assert positions[0].current_price == pytest.approx(0.0)


async def test_ensure_account_provisions_from_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression: the credential guard read ``self._account_login`` and friends,
    which __init__ never sets, so every real connection raised AttributeError."""
    adapter = MetaApiAdapter(
        token="test-token", account_login=12345, account_password="secret",
        account_server="MetaQuotes-Demo", mode="PAPER",
    )
    calls: list[tuple[str, str]] = []

    async def _fake_request(method: str, path: str, **kwargs):
        calls.append((method, path))
        if method == "POST" and path == "/api/accounts":
            return {"id": "new-acct"}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter._ensure_account()

    assert adapter._account_id == "new-acct"
    assert calls == [("POST", "/api/accounts")]


async def test_ensure_account_falls_back_to_existing_accounts(
    monkeypatch: pytest.MonkeyPatch,
):
    """Without credentials it should adopt an existing account, not crash."""
    adapter = MetaApiAdapter(token="test-token", mode="PAPER")

    async def _fake_request(method: str, path: str, **kwargs):
        return {"accounts": [{"id": "existing-1"}]}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter._ensure_account()
    assert adapter._account_id == "existing-1"


async def test_get_tick_requires_a_mapping_payload(monkeypatch: pytest.MonkeyPatch):
    """A non-dict payload must raise SymbolNotFoundError, not TypeError."""
    adapter = _adapter(monkeypatch, [{"bid": 1.1}])

    from trading_engine.brokers.base import SymbolNotFoundError

    with pytest.raises(SymbolNotFoundError):
        await adapter.get_tick("EURUSD")


async def test_get_tick_maps_a_symbol_payload(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(
        monkeypatch,
        {"symbol": "EURUSD", "bid": 1.0850, "ask": 1.0852, "volume": 3},
    )
    tick = await adapter.get_tick("EURUSD")
    assert tick.symbol == "EURUSD"
    assert tick.bid == pytest.approx(1.0850)
    assert tick.ask == pytest.approx(1.0852)
    assert tick.volume == pytest.approx(3.0)
