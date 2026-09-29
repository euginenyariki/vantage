"""Bot lifecycle and the live-trading safety gates."""

import pytest



async def _make_bot(client, auth, **overrides):
    body = {"name": "Test Bot", "symbol": "EURUSD", "mode": "PAPER"}
    body.update(overrides)
    resp = await client.post("/api/bots", json=body, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_list_requires_auth(client):
    assert (await client.get("/api/bots")).status_code == 401


@pytest.mark.asyncio
async def test_create_and_list(client, auth):
    bot = await _make_bot(client, auth)
    assert bot["mode"] == "PAPER"
    assert bot["live_granted"] is False
    assert bot["state"] == "STOPPED"

    listed = (await client.get("/api/bots", headers=auth)).json()
    assert [b["id"] for b in listed] == [bot["id"]]


@pytest.mark.asyncio
async def test_live_mode_cannot_be_created_at_all(client, auth):
    """Creating a bot directly in LIVE mode is refused, not silently demoted."""
    resp = await client.post("/api/bots", json={
        "name": "Sneaky", "symbol": "EURUSD", "mode": "LIVE",
    }, headers=auth)
    assert resp.status_code == 403
    assert "grant" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_to_live_is_blocked_before_a_grant(client, auth):
    bot = await _make_bot(client, auth)
    resp = await client.patch(f"/api/bots/{bot['id']}", json={"mode": "LIVE"}, headers=auth)
    assert resp.status_code == 403
    assert "grant" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_live_allowed_after_grant(client, auth):
    bot = await _make_bot(client, auth)
    granted = (await client.post(f"/api/bots/{bot['id']}/grant-live", headers=auth)).json()
    assert granted["live_granted"] is True

    resp = await client.patch(f"/api/bots/{bot['id']}", json={"mode": "LIVE"}, headers=auth)
    assert resp.status_code == 200
    assert resp.json()["mode"] == "LIVE"


@pytest.mark.asyncio
async def test_revoke_live_demotes_the_bot(client, auth):
    bot = await _make_bot(client, auth)
    await client.post(f"/api/bots/{bot['id']}/grant-live", headers=auth)
    await client.patch(f"/api/bots/{bot['id']}", json={"mode": "LIVE"}, headers=auth)

    after = (await client.post(f"/api/bots/{bot['id']}/revoke-live", headers=auth)).json()
    assert after["live_granted"] is False
    assert after["mode"] == "PAPER"


@pytest.mark.asyncio
async def test_start_is_blocked_while_server_live_is_off(client, auth):
    """The global switch outranks the per-bot grant."""
    bot = await _make_bot(client, auth)
    await client.post(f"/api/bots/{bot['id']}/grant-live", headers=auth)
    await client.patch(f"/api/bots/{bot['id']}", json={"mode": "LIVE"}, headers=auth)

    resp = await client.post(f"/api/bots/{bot['id']}/start", headers=auth)
    assert resp.status_code == 403
    assert "server-wide" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_live_start_requires_a_verified_connection(client, auth):
    """Even with the global switch on, an unverified connection must block."""
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.live_trading_enabled
    object.__setattr__(settings, "live_trading_enabled", True)
    try:
        conn = (await client.post("/api/brokers", json={
            "name": "Unverified", "login": "12345", "password": "pw",
            "server": "srv", "is_demo": False,
        }, headers=auth)).json()
        assert conn["connection_state"] != "CONNECTED"

        bot = await _make_bot(client, auth, broker_connection_id=conn["id"])
        await client.post(f"/api/bots/{bot['id']}/grant-live", headers=auth)
        await client.patch(f"/api/bots/{bot['id']}", json={"mode": "LIVE"}, headers=auth)

        resp = await client.post(f"/api/bots/{bot['id']}/start", headers=auth)
        assert resp.status_code == 403
        detail = resp.json()["detail"].lower()
        assert "verified" in detail or "not verified" in detail
    finally:
        object.__setattr__(settings, "live_trading_enabled", original)


@pytest.mark.asyncio
async def test_live_start_rejects_a_demo_connection(client, auth):
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.live_trading_enabled
    object.__setattr__(settings, "live_trading_enabled", True)
    try:
        conn = (await client.post("/api/brokers", json={
            "name": "Demo", "login": "12345", "password": "pw",
            "server": "srv", "is_demo": True,
        }, headers=auth)).json()

        bot = await _make_bot(client, auth, broker_connection_id=conn["id"])
        await client.post(f"/api/bots/{bot['id']}/grant-live", headers=auth)
        await client.patch(f"/api/bots/{bot['id']}", json={"mode": "LIVE"}, headers=auth)

        resp = await client.post(f"/api/bots/{bot['id']}/start", headers=auth)
        assert resp.status_code == 403
        assert "demo" in resp.json()["detail"].lower()
    finally:
        object.__setattr__(settings, "live_trading_enabled", original)


@pytest.mark.asyncio
async def test_kill_switch_sets_emergency_stop(client, auth):
    bot = await _make_bot(client, auth)
    resp = await client.post(f"/api/bots/{bot['id']}/kill-switch", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["state"] == "EMERGENCY_STOP"


@pytest.mark.asyncio
async def test_delete_bot(client, auth):
    bot = await _make_bot(client, auth)
    assert (await client.delete(f"/api/bots/{bot['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/bots/{bot['id']}", headers=auth)).status_code == 404


@pytest.mark.asyncio
async def test_cannot_see_or_modify_another_users_bot(client, auth, other_auth):
    bot = await _make_bot(client, auth)

    assert (await client.get(f"/api/bots/{bot['id']}", headers=other_auth)).status_code == 404
    assert (await client.patch(f"/api/bots/{bot['id']}", json={"name": "Hacked"},
                               headers=other_auth)).status_code == 404
    assert (await client.delete(f"/api/bots/{bot['id']}", headers=other_auth)).status_code == 404
    assert (await client.post(f"/api/bots/{bot['id']}/start", headers=other_auth)).status_code == 404


@pytest.mark.asyncio
async def test_invalid_broker_connection_uuid_is_422(client, auth):
    resp = await client.post("/api/bots", json={
        "name": "Bad", "symbol": "EURUSD", "broker_connection_id": "not-a-uuid",
    }, headers=auth)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_broker_connection_id_means_unset(client, auth):
    bot = await _make_bot(client, auth, broker_connection_id="")
    assert bot["broker_connection_id"] is None
