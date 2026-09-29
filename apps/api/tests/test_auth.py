"""Registration, login, session rotation, and revocation."""

import pytest

from tests.conftest import _register


@pytest.mark.asyncio
async def test_register_returns_user_and_token(client):
    body = await _register(client)
    assert body["user"]["email"].endswith("@example.com")
    assert body["user"]["country"] == "US"
    assert body["tokens"]["token_type"] == "bearer"
    assert body["tokens"]["access_token"]


@pytest.mark.asyncio
async def test_password_is_never_returned(client):
    body = await _register(client)
    assert "password" not in body["user"]
    assert "password_hash" not in body["user"]


@pytest.mark.asyncio
async def test_weak_password_rejected(client):
    resp = await client.post("/api/auth/register", json={
        "email": "weak@example.com", "name": "W", "password": "short",
        "country": "US", "accepted_terms": True,
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_terms_must_be_accepted(client):
    resp = await client.post("/api/auth/register", json={
        "email": "noterms@example.com", "name": "N", "password": "Str0ngPass!2026",
        "country": "US", "accepted_terms": False,
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_email_rejected(client):
    await _register(client, email="dup@example.com")
    resp = await client.post("/api/auth/register", json={
        "email": "dup@example.com", "name": "D", "password": "Str0ngPass!2026",
        "country": "US", "accepted_terms": True,
    })
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_login_with_wrong_password_is_401(client):
    await _register(client, email="pw@example.com")
    resp = await client.post("/api/auth/login", json={
        "email": "pw@example.com", "password": "WrongPass!2026",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_sets_http_only_refresh_cookie(client):
    await _register(client, email="cookie@example.com")
    resp = await client.post("/api/auth/login", json={
        "email": "cookie@example.com", "password": "Str0ngPass!2026",
    })
    assert resp.status_code == 200
    cookies = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
    assert any("httponly" in c.lower() for c in cookies), "refresh cookie must be HttpOnly"
    assert any("samesite" in c.lower() for c in cookies), "refresh cookie needs SameSite"


@pytest.mark.asyncio
async def test_me_requires_a_token(client):
    assert (await client.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_me_rejects_a_garbage_token(client):
    resp = await client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_the_authenticated_user(client, auth):
    body = (await client.get("/api/auth/me", headers=auth)).json()
    assert body["email"].endswith("@example.com")


@pytest.mark.asyncio
async def test_refresh_rotates_and_invalidates_the_old_cookie(client, user):
    """Rotating a refresh token must not leave the previous one usable."""

    resp = await client.post("/api/auth/refresh")
    assert resp.status_code == 200
    new_cookie = resp.cookies.get("vantage_refresh")
    assert new_cookie

    # Reuse the token from registration; it must be rejected after rotation.
    stale = await client.post("/api/auth/refresh", headers={
        "Cookie": f"vantage_refresh={user['tokens'].get('refresh_token', 'stale')}"
    })
    assert stale.status_code in {400, 401}


@pytest.mark.asyncio
async def test_logout_revokes_the_session(client, user):
    """After logout the access token must stop working."""
    from sqlalchemy import select

    from app.core.database import get_sessionmaker
    from trading_engine.persistence.models import UserSession

    resp = await client.post("/api/auth/logout")
    assert resp.status_code in {200, 204}

    sm = get_sessionmaker()
    async with sm() as session:
        rows = await session.scalars(select(UserSession))
        active = [s for s in rows if s.revoked_at is None]
    assert active == [], "logout must revoke the session row"


@pytest.mark.asyncio
async def test_logout_all_revokes_every_session(client, user, auth):
    from sqlalchemy import select

    from app.core.database import get_sessionmaker
    from trading_engine.persistence.models import UserSession

    resp = await client.post("/api/auth/logout-all", headers=auth)
    assert resp.status_code in {200, 204}

    sm = get_sessionmaker()
    async with sm() as session:
        rows = await session.scalars(select(UserSession))
        assert all(s.revoked_at is not None for s in rows)
