"""MetaApi adapter mapping.

These exercise the response->domain mapping with a stubbed transport, so the
broker paths that need a real MetaApi account are still covered offline.
"""

from __future__ import annotations

import pytest

from trading_engine.brokers.base import BrokerRejectedError
from trading_engine.brokers.metaapi import MetaApiAdapter
from trading_engine.core.types import OrderRequest, OrderType, Side, TradingMode


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


def _live_adapter(monkeypatch: pytest.MonkeyPatch, info_payload) -> MetaApiAdapter:
    """Adapter in LIVE mode, already connected, with account-info stubbed.

    ``account_type`` is what the third live gate reads, so the payload supplied here
    is what decides whether a live order is allowed through.
    """
    adapter = MetaApiAdapter(
        token="test-token", account_login=12345, account_password="secret",
        account_server="MetaQuotes-Live", mode=TradingMode.LIVE, live_allowed=True,
    )
    adapter._account_id = "acct-live"
    adapter._state = adapter._state.__class__.CONNECTED

    async def _fake_request(method: str, path: str, **kwargs):
        if "account-info" in path:
            return info_payload
        return {"id": "order-1", "ticket": 555}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    return adapter


async def test_account_type_reads_a_live_account(monkeypatch: pytest.MonkeyPatch):
    adapter = _live_adapter(monkeypatch, {"accountType": "live"})
    assert await adapter.account_type() == "live"


async def test_account_type_reads_a_demo_account(monkeypatch: pytest.MonkeyPatch):
    adapter = _live_adapter(monkeypatch, {"accountType": "demo"})
    assert await adapter.account_type() == "demo"


async def test_account_type_fails_closed_when_the_field_is_absent(
    monkeypatch: pytest.MonkeyPatch,
):
    """An unknown account type must never be assumed live."""
    adapter = _live_adapter(monkeypatch, {"login": 12345})
    with pytest.raises(BrokerRejectedError):
        await adapter.account_type()


async def test_verify_live_account_rejects_a_demo_credential(
    monkeypatch: pytest.MonkeyPatch,
):
    """The regression this gate exists for: live flag and per-bot grant both set,
    but the credential is a demo account."""
    adapter = _live_adapter(monkeypatch, {"accountType": "demo"})
    with pytest.raises(BrokerRejectedError) as exc:
        await adapter.verify_live_account()
    assert "demo" in str(exc.value).lower()


async def test_verify_live_account_passes_a_live_credential(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = _live_adapter(monkeypatch, {"accountType": "live"})
    await adapter.verify_live_account()


async def test_verify_live_account_is_skipped_in_demo_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    """A demo order is a real order against a simulated balance, so the mode
    itself is the permission. It must not demand live-account proof."""
    adapter = MetaApiAdapter(
        token="t", account_login=1, account_password="p",
        account_server="s", mode=TradingMode.DEMO, live_allowed=False,
    )
    adapter._account_id = "acct-demo"
    await adapter.verify_live_account()


async def test_submit_order_refuses_a_demo_account_in_live_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end through submit_order: no live order reaches a demo account even
    with the server flag and the grant both switched on."""
    adapter = _live_adapter(monkeypatch, {"accountType": "demo"})
    submitted: list = []

    async def _fake_request(method: str, path: str, **kwargs):
        if "account-info" in path:
            return {"accountType": "demo"}
        submitted.append((method, path))
        return {"id": "order-1", "ticket": 555}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    with pytest.raises(BrokerRejectedError):
        await adapter.submit_order(
            OrderRequest(
                symbol="EURUSD", side=Side.BUY, order_type=OrderType.MARKET,
                volume=0.1, client_order_id="cid-1",
            )
        )
    assert not submitted, "an order was submitted to a demo account in live mode"


async def test_disconnect_invalidates_the_live_verification(
    monkeypatch: pytest.MonkeyPatch,
):
    """A cached 'verified live' must not survive into a different connection."""
    adapter = _live_adapter(monkeypatch, {"accountType": "live"})
    await adapter.verify_live_account()
    adapter._live_account_verified = True
    await adapter.disconnect()
    assert adapter._live_account_verified is False
