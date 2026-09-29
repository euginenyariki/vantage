"""Broker connection CRUD, credential encryption, and access control."""

import pytest
from sqlalchemy import select

from app.core.crypto import decrypt_secret, encrypt_secret
from trading_engine.persistence.models import BrokerConnection

SECRET = "SuperSecret123"


async def _create(client, auth, **overrides):
    body = {
        "name": "MT5 Demo", "broker": "metaapi", "is_demo": True,
        "login": "5123456", "password": SECRET, "server": "MetaQuotes-Demo",
        "currency": "USD",
    }
    body.update(overrides)
    resp = await client.post("/api/brokers", json=body, headers=auth)
    return resp


@pytest.mark.asyncio
async def test_crypto_round_trip():
    blob = encrypt_secret("hunter2")
    assert blob.startswith("v1:")
    assert "hunter2" not in blob
    assert decrypt_secret(blob) == "hunter2"


@pytest.mark.asyncio
async def test_crypto_rejects_a_tampered_ciphertext():
    blob = encrypt_secret("hunter2")
    tampered = blob[:-4] + "AAAA"
    assert decrypt_secret(tampered) == ""


@pytest.mark.asyncio
async def test_crypto_passes_through_empty_values():
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


@pytest.mark.asyncio
async def test_create_stores_credentials_encrypted(client, auth):
    from app.core.database import get_sessionmaker

    conn_id = (await _create(client, auth)).json()["id"]

    sm = get_sessionmaker()
    async with sm() as session:
        row = await session.scalar(
            select(BrokerConnection).where(BrokerConnection.id == conn_id))
        assert row is not None
        # The raw columns must be ciphertext, never the values the user typed.
        assert SECRET not in row.password_encrypted
        assert "5123456" not in row.login_encrypted
        assert "MetaQuotes" not in row.server_encrypted
        # ...but they must still be recoverable by the server.
        assert decrypt_secret(row.password_encrypted) == SECRET
        assert decrypt_secret(row.login_encrypted) == "5123456"
        assert decrypt_secret(row.server_encrypted) == "MetaQuotes-Demo"


@pytest.mark.asyncio
async def test_response_masks_the_login(client, auth):
    body = (await _create(client, auth)).json()
    assert body["masked_login"] == "****456"
    assert "password" not in body
    assert "login_encrypted" not in body
    assert "server_encrypted" not in body


@pytest.mark.asyncio
async def test_no_endpoint_ever_returns_a_secret(client, auth):
    conn = (await _create(client, auth)).json()
    for path in ("/api/brokers", f"/api/brokers/{conn['id']}"):
        body = (await client.get(path, headers=auth)).text
        assert SECRET not in body
        assert "5123456" not in body
        assert "MetaQuotes-Demo" not in body


@pytest.mark.asyncio
async def test_duplicate_name_is_409(client, auth):
    await _create(client, auth)
    assert (await _create(client, auth)).status_code == 409


@pytest.mark.asyncio
async def test_test_endpoint_reports_unconfigured_broker(client, auth):
    conn = (await _create(client, auth)).json()
    resp = await client.post(f"/api/brokers/{conn['id']}/test", headers=auth)
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_failed_test_marks_the_connection_as_error(client, auth):
    conn = (await _create(client, auth)).json()
    await client.post(f"/api/brokers/{conn['id']}/test", headers=auth)

    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        row = await session.scalar(
            select(BrokerConnection).where(BrokerConnection.id == conn["id"]))
        assert row.connection_state == "ERROR"
        assert row.last_error


@pytest.mark.asyncio
async def test_delete_is_blocked_while_a_bot_uses_it(client, auth):
    conn = (await _create(client, auth)).json()
    bot = (await client.post("/api/bots", json={
        "name": "Attached", "symbol": "EURUSD", "mode": "PAPER",
        "broker_connection_id": conn["id"],
    }, headers=auth)).json()

    assert (await client.delete(f"/api/brokers/{conn['id']}", headers=auth)).status_code == 409

    await client.delete(f"/api/bots/{bot['id']}", headers=auth)
    assert (await client.delete(f"/api/brokers/{conn['id']}", headers=auth)).status_code == 204


@pytest.mark.asyncio
async def test_list_and_get_require_auth(client):
    assert (await client.get("/api/brokers")).status_code == 401
    assert (await client.post("/api/brokers", json={})).status_code == 401


@pytest.mark.asyncio
async def test_cannot_see_another_users_connection(client, auth, other_auth):
    conn = (await _create(client, auth)).json()
    assert (await client.get(f"/api/brokers/{conn['id']}", headers=other_auth)).status_code == 404
    assert (await client.post(f"/api/brokers/{conn['id']}/test",
                              headers=other_auth)).status_code == 404
    assert (await client.delete(f"/api/brokers/{conn['id']}",
                                headers=other_auth)).status_code == 404


@pytest.mark.asyncio
async def test_list_only_returns_own_connections(client, auth, other_auth):
    await _create(client, auth, name="Mine")
    await _create(client, other_auth, name="Theirs")

    mine = (await client.get("/api/brokers", headers=auth)).json()
    assert [c["name"] for c in mine] == ["Mine"]


@pytest.mark.asyncio
async def test_short_password_is_rejected(client, auth):
    assert (await _create(client, auth, password="")).status_code == 422
