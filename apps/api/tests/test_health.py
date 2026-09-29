"""Health and readiness probes."""

import pytest


@pytest.mark.asyncio
async def test_health_reports_dependencies(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    # The test database and Redis are both up, so it must be fully healthy.
    assert body["database"] is True
    assert body["redis"] is True


@pytest.mark.asyncio
async def test_ready_returns_200_when_healthy(client):
    resp = await client.get("/api/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


@pytest.mark.asyncio
async def test_health_reports_disabled_live_trading(client):
    """The test env must never advertise live trading as on."""
    body = (await client.get("/api/health")).json()
    assert body["live_trading_enabled"] is False
    assert body["trading_mode"] == "PAPER"


class TestCorsOriginParsing:
    """Regression: cors_origins is a list field, so pydantic-settings
    JSON-decodes it from the environment. The natural single-URL form in a
    .env file made the API raise SettingsError and refuse to boot."""

    def test_bare_single_url(self):
        from app.core.config import Settings

        assert Settings(cors_origins="http://localhost:3000").cors_origin_list == [
            "http://localhost:3000"
        ]

    def test_comma_separated(self):
        from app.core.config import Settings

        raw = "http://localhost:3000, https://app.example.com"
        assert Settings(cors_origins=raw).cors_origin_list == [
            "http://localhost:3000",
            "https://app.example.com",
        ]

    def test_json_array(self):
        from app.core.config import Settings

        raw = '["https://a.example.com", "https://b.example.com"]'
        assert Settings(cors_origins=raw).cors_origin_list == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_empty_means_no_origins(self):
        from app.core.config import Settings

        assert Settings(cors_origins="").cors_origin_list == []

    def test_malformed_json_is_reported_clearly(self):
        from pydantic import ValidationError

        from app.core.config import Settings

        with pytest.raises((ValidationError, ValueError)):
            Settings(cors_origins='["http://unterminated')
