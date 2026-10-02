"""MetaApi adapter mapping.

These exercise the response->domain mapping with a stubbed transport, so the
broker paths that need a real MetaApi account are still covered offline.

The payloads and paths here are taken from MetaApi's published OpenAPI spec
(`/api-docs.json` on the client host), not from guesswork. An earlier version of
this adapter was written against an API that did not exist and had never been
run against the real service, so these tests also pin the wire format: a change
in field names or paths should fail here rather than silently at 3am against a
live account.
"""

from __future__ import annotations

import pytest

from trading_engine.brokers import metaapi as metaapi_module
from trading_engine.brokers.base import (
    BrokerAuthError,
    BrokerRejectedError,
    ConnectionState,
    SymbolNotFoundError,
)
from trading_engine.brokers.metaapi import MetaApiAdapter
from trading_engine.core.types import (
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TradingMode,
)


def _adapter(monkeypatch: pytest.MonkeyPatch, payload) -> MetaApiAdapter:
    adapter = MetaApiAdapter(
        token="test-token", account_login=12345, account_password="secret",
        account_server="ICMarketsSC-Demo", mode=TradingMode.PAPER,
    )

    async def _fake_request(method: str, path: str, **kwargs):
        return payload

    # Pretend connect() already provisioned the account, so the request path
    # (which is what we are testing) can run offline.
    adapter._account_id = "acct-1"
    monkeypatch.setattr(adapter, "_request", _fake_request)
    return adapter


def _live_adapter(monkeypatch: pytest.MonkeyPatch, info_payload) -> MetaApiAdapter:
    """Adapter in LIVE mode, already connected, with account-information stubbed.

    The account ``type`` field is what the third live gate reads, so the payload
    supplied here is what decides whether a live order is allowed through.
    """
    adapter = MetaApiAdapter(
        token="test-token", account_login=12345, account_password="secret",
        account_server="MetaQuotes-Live", mode=TradingMode.LIVE, live_allowed=True,
    )
    adapter._account_id = "acct-live"
    adapter._state = ConnectionState.CONNECTED

    async def _fake_request(method: str, path: str, **kwargs):
        if "account-information" in path:
            return info_payload
        return {"orderId": "47137555", "stringCode": "TRADE_RETCODE_DONE"}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    return adapter


# --------------------------------------------------------------------------- #
# Transport contract
# --------------------------------------------------------------------------- #
def test_region_specific_hosts_are_used():
    """MetaApi serves each API from its own region-specific host. Getting this
    wrong yields a DNS or TLS failure that looks like a bad token."""
    a = MetaApiAdapter(token="t", region="london")
    assert a.client_url == "https://mt-client-api-v1.london.agiliumtrade.ai"
    assert a.provisioning_url == "https://mt-provisioning-api-v1.london.agiliumtrade.ai"
    assert a.market_data_url == (
        "https://mt-market-data-client-api-v1.london.agiliumtrade.ai"
    )


def test_unpublished_region_is_rejected():
    """MetaApi only offers london and new-york. Silently building a host for an
    imaginary region produces a confusing connection error much later."""
    with pytest.raises(BrokerAuthError):
        MetaApiAdapter(token="t", region="eu-central-1")


def test_auth_uses_the_auth_token_header():
    """Regression: this was `Authorization: Bearer`, which MetaApi ignores, so
    every request returned 401 regardless of token validity."""
    headers = MetaApiAdapter(token="abc123").headers
    assert headers["auth-token"] == "abc123"
    assert "Authorization" not in headers


# --------------------------------------------------------------------------- #
# Position mapping
# --------------------------------------------------------------------------- #
async def test_get_positions_maps_a_metaapi_payload(monkeypatch: pytest.MonkeyPatch):
    """Regression: Position has no ``profit`` field, so passing one raised
    TypeError and broke every real account's position reconciliation."""
    payload = {
        "positions": [
            {
                "id": "46648037",
                "symbol": "EURUSD",
                "side": "buy",
                "volume": 0.1,
                "openPrice": 1.0850,
                "currentPrice": 1.0900,
                "profit": 50.0,
            }
        ]
    }
    adapter = _adapter(monkeypatch, payload)
    positions = await adapter.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "EURUSD"
    assert p.side is Side.BUY
    assert p.unrealized_pnl == pytest.approx(50.0)


async def test_get_positions_accepts_a_bare_list(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(monkeypatch, [{"id": "1", "symbol": "GBPUSD", "side": "sell"}])
    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].side is Side.SELL


async def test_get_positions_handles_an_empty_response(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(monkeypatch, [])
    assert await adapter.get_positions() == []


async def test_get_positions_tolerates_missing_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = _adapter(monkeypatch, [{"id": "1", "symbol": "EURUSD", "side": "buy"}])
    positions = await adapter.get_positions()
    assert positions[0].unrealized_pnl == pytest.approx(0.0)
    assert positions[0].stop_loss is None


# --------------------------------------------------------------------------- #
# Account provisioning
# --------------------------------------------------------------------------- #
async def test_ensure_account_provisions_from_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    """With credentials, the adapter provisions a terminal rather than guessing."""
    adapter = MetaApiAdapter(
        token="t", account_login=12345, account_password="p", account_server="s",
    )
    seen: list = []

    async def _fake_request(method: str, path: str, **kwargs):
        seen.append((method, path))
        if path == "/users/current/accounts" and method == "POST":
            return {"id": "provisioned-1", "state": "DEPLOYED"}
        return {}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter._ensure_account()
    assert adapter._account_id == "provisioned-1"
    assert seen[0] == ("POST", "/users/current/accounts")


async def test_ensure_account_falls_back_to_existing_accounts(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = MetaApiAdapter(token="t")
    seen: list = []

    async def _fake_request(method: str, path: str, **kwargs):
        seen.append((method, path))
        return {"accounts": [{"id": "acct-a", "login": "111"}, {"id": "acct-b", "login": "222"}]}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter._ensure_account()
    assert adapter._account_id == "acct-a"
    assert seen[0] == ("GET", "/users/current/accounts")


async def test_ensure_account_matches_our_login_not_the_first(
    monkeypatch: pytest.MonkeyPatch,
):
    """With several terminals on one token, picking whichever is listed first
    would trade the wrong account."""
    adapter = MetaApiAdapter(token="t", account_login=222)

    async def _fake_request(method: str, path: str, **kwargs):
        return {"accounts": [{"id": "acct-a", "login": "111"}, {"id": "acct-b", "login": "222"}]}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter._ensure_account()
    assert adapter._account_id == "acct-b"


async def test_provisioning_post_reuses_the_transaction_id(
    monkeypatch: pytest.MonkeyPatch,
):
    """202 means 'still starting'. MetaApi deduplicates retries by transaction-id,
    so a fresh id per attempt would create duplicate accounts."""
    adapter = MetaApiAdapter(token="t")
    seen_ids: list = []

    async def _fake_request(method: str, path: str, *, headers=None, **kwargs):
        seen_ids.append((headers or {}).get("transaction-id"))
        if len(seen_ids) < 3:
            return {"message": "in progress, retry in 60 seconds"}
        return {"id": "acct-1", "state": "DEPLOYED"}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    # Backoff is real but slow; the test is about id reuse, not timing.
    monkeypatch.setattr(metaapi_module.asyncio, "sleep", _noop)

    result = await adapter._provisioning_post("/users/current/accounts", {})
    assert result["id"] == "acct-1"
    assert len(seen_ids) == 3
    assert len(set(seen_ids)) == 1, "transaction-id changed between retries"


async def _noop(*_a, **_k):
    return None


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
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


async def test_get_tick_uses_the_current_tick_route(monkeypatch: pytest.MonkeyPatch):
    """Regression: the route is /current-tick, not /tick."""
    adapter = _adapter(monkeypatch, {"bid": 1.0, "ask": 1.1})
    seen: list = []

    async def _fake_request(method: str, path: str, **kwargs):
        seen.append(path)
        return {"bid": 1.0, "ask": 1.1}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter.get_tick("EURUSD")
    assert seen[0].endswith("/symbols/EURUSD/current-tick")


async def test_get_bars_parses_iso_timestamps(monkeypatch: pytest.MonkeyPatch):
    """MetaApi returns `time` as an ISO string. Parsing it as epoch millis is
    what produced candle dates in 1970."""
    from datetime import UTC, datetime

    adapter = _adapter(
        monkeypatch,
        [{"time": "2024-03-05T10:15:00.000Z", "open": 1.08, "high": 1.09,
          "low": 1.07, "close": 1.085, "volume": 120, "tickVolume": 400}],
    )
    bars = await adapter.get_bars("EURUSD", "M15", 1)
    assert len(bars) == 1
    assert bars[0].timeframe == "M15"
    assert bars[0].volume == pytest.approx(120.0)
    assert bars[0].timestamp == datetime(2024, 3, 5, 10, 15, tzinfo=UTC)


async def test_get_bars_uses_mt5_timeframe_codes_and_market_data_host(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = _adapter(monkeypatch, [])
    seen: dict = {}

    async def _fake_request(method: str, path: str, *, api="client", **kwargs):
        seen.update(path=path, api=api, params=kwargs.get("params"))
        return []

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter.get_bars("EURUSD", "H4", 10)
    assert "/timeframes/4h/candles" in seen["path"]
    assert "/historical-market-data/" in seen["path"]
    assert seen["api"] == "market_data"
    assert seen["params"]["limit"] == "10"


async def test_get_bars_caps_the_limit(monkeypatch: pytest.MonkeyPatch):
    """MetaApi rejects limit > 1000 outright."""
    adapter = _adapter(monkeypatch, [])
    seen: dict = {}

    async def _fake_request(method: str, path: str, *, params=None, **kwargs):
        seen.update(params=params or {})
        return []

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter.get_bars("EURUSD", "M1", 5000)
    assert seen["params"]["limit"] == "1000"


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
async def test_submit_order_uses_action_type_and_maps_retcode(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = _adapter(monkeypatch, {})
    adapter._state = ConnectionState.CONNECTED
    seen: dict = {}

    async def _fake_request(method: str, path: str, **kwargs):
        seen.update(path=path, body=kwargs.get("json"))
        return {"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE",
                "message": "Request completed", "orderId": "47137555"}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    result = await adapter.submit_order(
        OrderRequest(
            symbol="EURUSD", side=Side.BUY, order_type=OrderType.MARKET,
            volume=0.1, client_order_id="cid-1",
        )
    )
    assert result.accepted is True
    assert result.broker_order_id == "47137555"
    # Regression: MetaApi does not understand {"type": "trade", "side": "buy"}.
    assert seen["body"]["actionType"] == "ORDER_TYPE_BUY"
    assert seen["body"]["symbol"] == "EURUSD"
    assert seen["body"]["volume"] == 0.1
    assert seen["path"].endswith("/trade")


async def test_submit_order_reports_a_broker_rejection(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(monkeypatch, {})
    adapter._state = ConnectionState.CONNECTED

    async def _fake_request(method: str, path: str, **kwargs):
        return {"numericCode": 10006, "stringCode": "TRADE_RETCODE_REJECT",
                "message": "Request rejected"}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    result = await adapter.submit_order(
        OrderRequest(
            symbol="EURUSD", side=Side.SELL, order_type=OrderType.MARKET,
            volume=0.1, client_order_id="cid-2",
        )
    )
    assert result.accepted is False


async def test_limit_order_uses_open_price(monkeypatch: pytest.MonkeyPatch):
    """Pending orders call the entry price `openPrice`, not `price`."""
    adapter = _adapter(monkeypatch, {})
    adapter._state = ConnectionState.CONNECTED
    seen: dict = {}

    async def _fake_request(method: str, path: str, **kwargs):
        seen.update(body=kwargs.get("json"))
        return {"stringCode": "TRADE_RETCODE_DONE", "orderId": "1"}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter.submit_order(
        OrderRequest(
            symbol="EURUSD", side=Side.BUY, order_type=OrderType.LIMIT,
            volume=0.1, limit_price=1.0800, client_order_id="cid-3",
        )
    )
    assert seen["body"]["actionType"] == "ORDER_TYPE_BUY_LIMIT"
    assert seen["body"]["openPrice"] == 1.0800
    assert "price" not in seen["body"]


async def test_cancel_order_posts_order_cancel_action(monkeypatch: pytest.MonkeyPatch):
    """There is no /orders/{id}/cancel route; cancellation goes through /trade."""
    adapter = _adapter(monkeypatch, {})
    seen: dict = {}

    async def _fake_request(method: str, path: str, **kwargs):
        seen.update(path=path, body=kwargs.get("json"))
        return {"stringCode": "TRADE_RETCODE_DONE"}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    assert await adapter.cancel_order("47137555") is True
    assert seen["path"].endswith("/trade")
    assert seen["body"] == {"actionType": "ORDER_CANCEL", "orderId": "47137555"}


async def test_close_position_uses_position_close_id(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(monkeypatch, {})
    seen: dict = {}

    async def _fake_request(method: str, path: str, **kwargs):
        seen.update(path=path, body=kwargs.get("json"))
        return {"stringCode": "TRADE_RETCODE_DONE", "orderId": "9"}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    result = await adapter.close_position("46648037")
    assert result.accepted is True
    assert seen["body"]["actionType"] == "POSITION_CLOSE_ID"
    assert seen["path"].endswith("/trade")


async def test_partial_close_uses_a_different_action(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(monkeypatch, {})
    seen: dict = {}

    async def _fake_request(method: str, path: str, **kwargs):
        seen.update(body=kwargs.get("json"))
        return {"stringCode": "TRADE_RETCODE_DONE", "orderId": "9"}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    await adapter.close_position("46648037", volume=0.05)
    assert seen["body"]["actionType"] == "POSITION_PARTIAL"
    assert seen["body"]["volume"] == 0.05


async def test_order_status_mapping_uses_metaapi_state_names(
    monkeypatch: pytest.MonkeyPatch,
):
    """MetaApi reports ORDER_STATE_*; the previous map keyed on values like
    'filled' that this API never sends, so every order looked PENDING."""
    adapter = _adapter(monkeypatch, [
        {"orderId": "1", "symbol": "EURUSD", "side": "buy", "volume": 0.1,
         "type": "ORDER_TYPE_BUY", "status": "ORDER_STATE_FILLED"},
        {"orderId": "2", "symbol": "EURUSD", "side": "sell", "volume": 0.1,
         "type": "ORDER_TYPE_SELL_LIMIT", "status": "ORDER_STATE_PLACED"},
        {"orderId": "3", "symbol": "EURUSD", "side": "buy", "volume": 0.1,
         "type": "ORDER_TYPE_BUY", "status": "ORDER_STATE_CANCELED"},
    ])
    orders = await adapter.get_orders()
    assert orders[0].status is OrderStatus.FILLED
    assert orders[1].status is OrderStatus.PENDING
    assert orders[1].order_type is OrderType.LIMIT
    assert orders[2].status is OrderStatus.CANCELLED


async def test_get_orders_can_filter_to_open(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(monkeypatch, [
        {"orderId": "1", "symbol": "EURUSD", "side": "buy", "volume": 0.1,
         "type": "ORDER_TYPE_BUY", "status": "ORDER_STATE_PLACED"},
        {"orderId": "2", "symbol": "EURUSD", "side": "buy", "volume": 0.1,
         "type": "ORDER_TYPE_BUY", "status": "ORDER_STATE_FILLED"},
    ])
    assert len(await adapter.get_orders(only_open=True)) == 1


async def test_get_order_uses_the_order_by_id_route(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(monkeypatch, {"orderId": "1", "symbol": "EURUSD"})
    seen: list = []

    async def _fake_request(method: str, path: str, **kwargs):
        seen.append(path)
        return {"orderId": "1", "symbol": "EURUSD", "side": "buy", "status": "ORDER_STATE_FILLED"}

    monkeypatch.setattr(adapter, "_request", _fake_request)
    order = await adapter.get_order("1")
    assert order is not None
    assert seen[0].endswith("/orders/1")


async def test_get_order_returns_none_when_missing(monkeypatch: pytest.MonkeyPatch):
    adapter = _adapter(monkeypatch, {})

    async def _fake_request(method: str, path: str, **kwargs):
        raise SymbolNotFoundError("404")

    monkeypatch.setattr(adapter, "_request", _fake_request)
    assert await adapter.get_order("nope") is None


# --------------------------------------------------------------------------- #
# Live safety
# --------------------------------------------------------------------------- #
async def test_account_type_reads_a_live_account(monkeypatch: pytest.MonkeyPatch):
    """MetaApi's enum is ACCOUNT_TRADE_MODE_REAL, not 'live'. The earlier
    implementation expected 'live'/'demo' and so rejected every real order."""
    adapter = _live_adapter(monkeypatch, {"type": "ACCOUNT_TRADE_MODE_REAL"})
    assert await adapter.account_type() == "live"


async def test_account_type_reads_a_demo_account(monkeypatch: pytest.MonkeyPatch):
    adapter = _live_adapter(monkeypatch, {"type": "ACCOUNT_TRADE_MODE_DEMO"})
    assert await adapter.account_type() == "demo"


async def test_contest_account_is_not_treated_as_live(monkeypatch: pytest.MonkeyPatch):
    """A contest account is not a funded one."""
    adapter = _live_adapter(monkeypatch, {"type": "ACCOUNT_TRADE_MODE_CONTEST"})
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
    adapter = _live_adapter(monkeypatch, {"type": "ACCOUNT_TRADE_MODE_DEMO"})
    with pytest.raises(BrokerRejectedError) as exc:
        await adapter.verify_live_account()
    assert "demo" in str(exc.value).lower()


async def test_verify_live_account_passes_a_live_credential(
    monkeypatch: pytest.MonkeyPatch,
):
    adapter = _live_adapter(monkeypatch, {"type": "ACCOUNT_TRADE_MODE_REAL"})
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
    adapter = _live_adapter(monkeypatch, {"type": "ACCOUNT_TRADE_MODE_DEMO"})
    submitted: list = []

    async def _fake_request(method: str, path: str, **kwargs):
        if "account-information" in path:
            return {"type": "ACCOUNT_TRADE_MODE_DEMO"}
        submitted.append((method, path))
        return {"stringCode": "TRADE_RETCODE_DONE", "orderId": "1"}

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
    adapter = _live_adapter(monkeypatch, {"type": "ACCOUNT_TRADE_MODE_REAL"})
    await adapter.verify_live_account()
    adapter._live_account_verified = True
    await adapter.disconnect()
    assert adapter._live_account_verified is False
