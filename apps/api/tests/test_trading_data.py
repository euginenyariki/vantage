"""Trading data, market data, and the WebSocket ownership filter."""

import pytest


@pytest.mark.asyncio
async def test_positions_orders_trades_require_auth(client):
    for path in ("/api/positions", "/api/orders", "/api/trades"):
        assert (await client.get(path)).status_code == 401


@pytest.mark.asyncio
async def test_empty_account_returns_empty_collections(client, auth):
    """A brand-new account must not see another account's rows."""
    for path in ("/api/positions", "/api/orders", "/api/trades"):
        body = (await client.get(path, headers=auth)).json()
        assert body == []


@pytest.mark.asyncio
async def test_positions_are_scoped_to_the_owner(client, auth, other_auth):

    from trading_engine.persistence.models import Position

    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        me = (await client.get("/api/auth/me", headers=auth)).json()
        someone = (await client.get("/api/auth/me", headers=other_auth)).json()
        session.add(Position(
            user_id=someone["id"], bot_id=None, symbol="EURUSD", side="BUY",
            volume=0.01, open_price=1.10, current_price=1.10, status="OPEN",
        ))
        await session.commit()

    visible = (await client.get("/api/positions", headers=auth)).json()
    assert visible == []
    other_visible = (await client.get("/api/positions", headers=other_auth)).json()
    assert len(other_visible) == 1
    assert me["id"] != someone["id"]


@pytest.mark.asyncio
async def test_cannot_close_another_users_position(client, auth, other_auth):
    from trading_engine.persistence.models import Position

    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        someone = (await client.get("/api/auth/me", headers=other_auth)).json()
        pos = Position(
            user_id=someone["id"], bot_id=None, symbol="EURUSD", side="BUY",
            volume=0.01, open_price=1.10, current_price=1.10, status="OPEN",
        )
        session.add(pos)
        await session.commit()
        pos_id = pos.id

    resp = await client.post(f"/api/positions/{pos_id}/close", headers=auth)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_market_requires_auth(client):
    # Prices are the protected part of the market API. The instrument catalogue
    # is public reference data and is intentionally open (see
    # TestPublicSymbolCatalogue in test_admin_analytics.py).
    for path in ("/api/market/quotes", "/api/market/quotes/EURUSD"):
        assert (await client.get(path)).status_code == 401


@pytest.mark.asyncio
async def test_symbol_catalogue_is_public(client):
    # The signed-out landing page depends on this; it must not 401.
    for path in ("/api/market/symbols", "/api/market/symbols/EURUSD"):
        assert (await client.get(path)).status_code == 200


@pytest.mark.asyncio
async def test_symbols_are_listed(client, auth):
    symbols = (await client.get("/api/market/symbols", headers=auth)).json()
    assert len(symbols) > 0
    s = symbols[0]
    assert {"symbol", "digits", "pip_size", "min_volume", "max_volume"} <= set(s)


@pytest.mark.asyncio
async def test_unknown_symbol_is_404(client, auth):
    assert (await client.get("/api/market/symbols/NOPEUSD", headers=auth)).status_code == 404


@pytest.mark.asyncio
async def test_quote_without_a_feed_is_503_not_500(client, auth):
    """No market data provider is configured in tests, so this must be a clean 503."""
    resp = await client.get("/api/market/quotes/EURUSD", headers=auth)
    assert resp.status_code in {200, 503}
    if resp.status_code == 503:
        assert "detail" in resp.json()


@pytest.mark.asyncio
async def test_dashboard_summary(client, auth):
    body = (await client.get("/api/dashboard/summary", headers=auth)).json()
    assert body["open_positions"] == 0
    assert float(body["equity"]) == 0.0
    assert body["total_trades"] == 0
    assert body["total_bots"] == 0


@pytest.mark.asyncio
async def test_dashboard_equity_curve(client, auth):
    body = (await client.get("/api/dashboard/equity", headers=auth)).json()
    assert isinstance(body, list)
