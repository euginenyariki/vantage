"""Tests for the admin router and the analytics endpoints.

The admin surface is the part of the API where a mistake leaks other people's
data or locks everyone out, so the rules are pinned here rather than left to
manual testing:

* non-admins cannot reach any admin route
* a regular user cannot escalate by hitting an admin route
* an admin cannot disable their own account
* disabling a user revokes their live sessions
* admin listings never contain broker credentials

Analytics is checked for the behaviour that matters to a trader: an empty
account must report honest zeros and ``None`` (not a flattering 100% win rate),
and one user's trades must never appear in another's report.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from decimal import Decimal

import pytest
from sqlalchemy import select
from trading_engine.persistence.models import BrokerConnection, Role, Trade, User

pytestmark = pytest.mark.asyncio


async def _promote(email: str, role: Role = Role.ADMIN) -> None:
    """Grant a role directly in the database, then force a fresh token.

    There is deliberately no API for self-promotion, so this bypasses the app
    the same way an operator promoting someone in the database would.
    """
    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None
        user.role = role
        await session.commit()


async def _admin_auth(client) -> dict:
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": f"admin-{uuid.uuid4().hex[:8]}@example.com",
            "name": "Admin",
            "password": "Str0ngPass!2026",
            "country": "KE",
            "accepted_terms": True,
        },
    )
    assert reg.status_code == 201
    body = reg.json()
    await _promote(body["user"]["email"], Role.ADMIN)
    # The access token embeds the role at issue time, so re-login to pick it up.
    login = await client.post(
        "/api/auth/login",
        json={"email": body["user"]["email"], "password": "Str0ngPass!2026"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}


async def _add_trade(user_id: str, symbol: str, pnl: str, closed: bool = True) -> None:
    from datetime import datetime

    from trading_engine.core.types import Side

    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        session.add(
            Trade(
                user_id=user_id,
                symbol=symbol,
                side=Side.BUY,
                volume=Decimal("0.10"),
                open_price=Decimal("1.10000"),
                close_price=Decimal("1.10100"),
                commission=Decimal("0"),
                swap=Decimal("0"),
                pnl=Decimal(pnl),
                opened_at=datetime.now(UTC),
                closed_at=datetime.now(UTC) if closed else None,
            )
        )
        await session.commit()


class TestAdminAuthorization:
    async def test_regular_user_is_rejected(self, client, auth):
        for method, path in [
            ("get", "/api/admin/stats"),
            ("get", "/api/admin/users"),
            ("get", "/api/admin/brokers"),
            ("get", "/api/admin/bots"),
        ]:
            resp = await getattr(client, method)(path, headers=auth)
            assert resp.status_code == 403, f"{path} -> {resp.status_code}"
            assert "Administrator" in resp.json()["detail"]

    async def test_anonymous_is_rejected(self, client):
        assert (await client.get("/api/admin/stats")).status_code == 401

    async def test_admin_can_read_stats(self, client):
        admin = await _admin_auth(client)
        resp = await client.get("/api/admin/stats", headers=admin)
        assert resp.status_code == 200
        body = resp.json()
        assert body["admins"] >= 1
        assert body["users"] >= 1

    async def test_user_cannot_self_promote_through_the_api(self, client, auth, user):
        # There must be no endpoint that edits your own role.
        for payload in ({"role": "ADMIN"}, {"role": "SUPER_ADMIN"}):
            resp = await client.patch("/api/auth/me", headers=auth, json=payload)
            assert resp.status_code in (404, 405, 422)
        me = await client.get("/api/auth/me", headers=auth)
        assert me.json()["role"] == "USER"

    async def test_admin_cannot_disable_own_account(self, client):
        admin = await _admin_auth(client)
        me = await client.get("/api/auth/me", headers=admin)
        own_id = me.json()["id"]
        resp = await client.post(
            f"/api/admin/users/{own_id}/active", params={"active": "false"}, headers=admin
        )
        assert resp.status_code == 400
        assert "your own account" in resp.json()["detail"]


class TestAdminUserManagement:
    async def test_user_directory_lists_users(self, client, admin_auth):
        resp = await client.get("/api/admin/users", headers=admin_auth)
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    async def test_search_filters_by_email(self, client, admin_auth, user):
        resp = await client.get(
            "/api/admin/users", params={"q": user["user"]["email"]}, headers=admin_auth
        )
        assert resp.status_code == 200
        emails = [row["email"] for row in resp.json()["items"]]
        assert user["user"]["email"] in emails

    async def test_disabling_a_user_revokes_their_sessions(self, client, admin_auth, user):
        target_id = user["user"]["id"]
        resp = await client.post(
            f"/api/admin/users/{target_id}/active",
            params={"active": "false"},
            headers=admin_auth,
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

        # The disabled account's existing token must stop working immediately,
        # otherwise a disabled trader keeps trading until the token expires.
        headers = {"Authorization": f"Bearer {user['tokens']['access_token']}"}
        after = await client.get("/api/auth/me", headers=headers)
        assert after.status_code == 403

    async def test_re_enabling_restores_access(self, client, admin_auth, user):
        target_id = user["user"]["id"]
        await client.post(
            f"/api/admin/users/{target_id}/active",
            params={"active": "false"}, headers=admin_auth,
        )
        resp = await client.post(
            f"/api/admin/users/{target_id}/active",
            params={"active": "true"}, headers=admin_auth,
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

        # Security model: disabling blocks the account immediately (checked on
        # every request) and revokes refresh sessions so no new access token can
        # be minted. An access token that was already issued stays valid until
        # it expires, which is standard JWT behaviour and is bounded by the
        # short access-token lifetime. What must not be possible is renewing a
        # revoked session, so the assertion is that logging in fresh works.
        fresh = await client.post(
            "/api/auth/login",
            json={"email": user["user"]["email"], "password": "Str0ngPass!2026"},
        )
        assert fresh.status_code == 200
        me = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {fresh.json()['tokens']['access_token']}"},
        )
        assert me.status_code == 200

    async def test_disabling_is_audited(self, client, admin_auth, user):
        from trading_engine.persistence.models import AuditLog

        from app.core.database import get_sessionmaker

        await client.post(
            f"/api/admin/users/{user['user']['id']}/active",
            params={"active": "false"}, headers=admin_auth,
        )
        sm = get_sessionmaker()
        async with sm() as session:
            row = await session.scalar(
                select(AuditLog).where(AuditLog.action == "admin.user.active_changed")
            )
        assert row is not None
        assert row.entity_type == "user"
        assert row.details == {"active": False}

    async def test_unknown_user_is_404(self, client, admin_auth):
        resp = await client.post(
            f"/api/admin/users/{uuid.uuid4()}/active",
            params={"active": "false"}, headers=admin_auth,
        )
        assert resp.status_code == 404


class TestAdminCredentialSafety:
    async def test_broker_listing_never_exposes_credentials(self, client, admin_auth, user):
        from app.core.crypto import encrypt_secret
        from app.core.database import get_sessionmaker

        sm = get_sessionmaker()
        async with sm() as session:
            session.add(
                BrokerConnection(
                    user_id=user["user"]["id"],
                    name="Live MT5",
                    broker="metaapi",
                    is_demo=True,
                    login_encrypted=encrypt_secret("12345678"),
                    password_encrypted=encrypt_secret("super-secret-password"),
                    server_encrypted=encrypt_secret("DemoBroker-Server"),
                )
            )
            await session.commit()

        resp = await client.get("/api/admin/brokers", headers=admin_auth)
        assert resp.status_code == 200
        body = resp.text
        for secret in ("super-secret-password", "DemoBroker-Server", "login_encrypted",
                       "password_encrypted", "server_encrypted"):
            assert secret not in body, f"admin broker listing leaked {secret}"
        assert resp.json()[0]["broker"] == "metaapi"


class TestAnalytics:
    async def test_report_for_a_new_account_is_honest(self, client, auth):
        resp = await client.get("/api/analytics/report", headers=auth)
        assert resp.status_code == 200
        metrics = resp.json()["metrics"]
        assert metrics["total_trades"] == 0
        assert metrics["win_rate"] == 0.0
        # With no trades there is no profit factor to report. Returning 0 or 1
        # here would read as "no losses", which is a different and wrong claim.
        assert metrics["profit_factor"] is None
        assert metrics["payoff_ratio"] is None
        assert metrics["sharpe_pnl_ratio"] is None
        assert resp.json()["by_symbol"] == []
        assert resp.json()["by_strategy"] == []

    async def test_metrics_reflect_real_trades(self, client, auth, user):
        await _add_trade(user["user"]["id"], "EURUSD", "25.00")
        await _add_trade(user["user"]["id"], "EURUSD", "-10.00")
        await _add_trade(user["user"]["id"], "GBPUSD", "5.00")

        resp = await client.get("/api/analytics/report", headers=auth)
        metrics = resp.json()["metrics"]
        assert metrics["total_trades"] == 3
        assert metrics["winning_trades"] == 2
        assert metrics["losing_trades"] == 1
        assert metrics["net_pnl"] == 20.0
        assert metrics["gross_profit"] == 30.0
        assert metrics["gross_loss"] == 10.0
        assert metrics["profit_factor"] == 3.0
        assert metrics["win_rate"] == pytest.approx(66.67, abs=0.01)
        # Net PnL 20 over 3 trades.
        assert metrics["expectancy"] == pytest.approx(6.6667, abs=0.001)
        # 15.0 average win against a 10.0 average loss.
        assert metrics["payoff_ratio"] == 1.5

    async def test_breakdown_is_grouped_correctly(self, client, auth, user):
        await _add_trade(user["user"]["id"], "EURUSD", "50.00")
        await _add_trade(user["user"]["id"], "GBPUSD", "-20.00")

        body = (await client.get("/api/analytics/report", headers=auth)).json()
        symbols = {row["symbol"]: row for row in body["by_symbol"]}
        assert symbols["EURUSD"]["net_pnl"] == 50.0
        assert symbols["EURUSD"]["trades"] == 1
        assert symbols["EURUSD"]["win_rate"] == 100.0
        assert symbols["GBPUSD"]["net_pnl"] == -20.0
        assert symbols["GBPUSD"]["win_rate"] == 0.0
        # Best symbol first.
        assert body["by_symbol"][0]["symbol"] == "EURUSD"

    async def test_one_user_never_sees_another_users_trades(
        self, client, auth, other_auth, user
    ):
        await _add_trade(user["user"]["id"], "EURUSD", "999.00")
        other = (await client.get("/api/analytics/report", headers=other_auth)).json()
        assert other["metrics"]["total_trades"] == 0
        assert other["metrics"]["net_pnl"] == 0.0
        assert other["by_symbol"] == []

    async def test_equity_series_accumulates(self, client, auth, user):
        await _add_trade(user["user"]["id"], "EURUSD", "30.00")
        await _add_trade(user["user"]["id"], "EURUSD", "-5.00")
        equity = (await client.get("/api/analytics/report", headers=auth)).json()["equity"]
        assert equity
        assert equity[-1]["cumulative_pnl"] == 25.0

    async def test_period_is_validated(self, client, auth):
        assert (await client.get("/api/analytics/performance?days=0", headers=auth)).status_code == 422
        assert (await client.get("/api/analytics/performance?days=9999", headers=auth)).status_code == 422
        assert (await client.get("/api/analytics/performance?days=90", headers=auth)).status_code == 200

    async def test_analytics_requires_authentication(self, client):
        assert (await client.get("/api/analytics/report")).status_code == 401


class TestPublicSymbolCatalogue:
    """The instrument catalogue is public reference data.

    The marketing page renders it while signed out; making it private broke the
    landing page (a silent 401 that the client swallowed into an empty list).
    Quotes stay authenticated, since prices are the sensitive part.
    """

    async def test_symbol_catalogue_is_readable_anonymously(self, client):
        resp = await client.get("/api/market/symbols")
        assert resp.status_code == 200
        symbols = resp.json()
        assert len(symbols) >= 8
        by_symbol = {s["symbol"]: s for s in symbols}
        assert {"EURUSD", "XAUUSD", "BTCUSD", "ETHUSD"} <= set(by_symbol)
        assert by_symbol["BTCUSD"]["asset_class"] == "crypto"
        assert by_symbol["EURUSD"]["asset_class"] == "forex"

    async def test_single_symbol_is_readable_anonymously(self, client):
        resp = await client.get("/api/market/symbols/BTCUSD")
        assert resp.status_code == 200
        assert resp.json()["symbol"] == "BTCUSD"

    async def test_unknown_symbol_is_404_for_anonymous(self, client):
        assert (await client.get("/api/market/symbols/NOPE")).status_code == 404

    async def test_quotes_remain_authenticated(self, client):
        assert (await client.get("/api/market/quotes")).status_code == 401
