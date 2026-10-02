"""Engine configuration. Every setting is validated at import; the engine
refuses to boot on an invalid or missing TRADING_MODE (rule 10)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading_engine.core.types import TradingMode

VALID_MODES = {m.value for m in TradingMode}


def _repo_env_file() -> str:
    """Locate the repository-root ``.env``, independent of the working directory.

    The engine's fallback DSNs point at the Docker Compose service names
    (``postgres`` and ``redis``). If this process is launched from
    ``apps/trading-engine`` rather than the repo root, a bare ``env_file=".env"``
    resolves to nothing, so those fallbacks are used and every queue and job lookup
    targets a host that does not exist here. Startup still succeeds, which is what
    makes this expensive to diagnose: the worker simply never picks up any work.

    Walking up from this file finds the ``.env`` belonging to the checkout. In a
    container there is no such file and configuration comes entirely from the
    environment, so behaviour there is unchanged.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".env"
        if candidate.is_file():
            return str(candidate)
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_repo_env_file(), env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    trading_mode: str = Field(default="PAPER")
    live_trading_enabled: bool = False

    database_url: str = "postgresql+asyncpg://vantage:vantage@postgres:5432/vantage"
    redis_url: str = "redis://redis:6379/0"

    engine_tick_seconds: float = 1.0
    reconcile_interval_seconds: float = 30.0
    market_data_stale_seconds: float = 15.0

    metaapi_enabled: bool = False
    metaapi_token: str = ""
    # MetaApi only publishes public access for these two regions; the adapter
    # derives all three of its API hosts from this.
    metaapi_region: str = "london"

    @field_validator("trading_mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        normalized = (v or "").strip().upper()
        if normalized not in VALID_MODES:
            raise ValueError(
                f"TRADING_MODE must be one of {sorted(VALID_MODES)}, got {v!r}"
            )
        return normalized

    @field_validator("live_trading_enabled")
    @classmethod
    def _guard_live(cls, v: bool) -> bool:
        # Rule 15: live execution stays off unless explicitly enabled. This does
        # not *grant* live trading — ExecutionEngine also re-checks it.
        return bool(v)

    @property
    def mode(self) -> TradingMode:
        return TradingMode(self.trading_mode)

    @property
    def live_allowed(self) -> bool:
        return self.live_trading_enabled and self.mode.is_real


@lru_cache
def get_settings() -> Settings:
    return Settings()


def refresh_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def assert_live_permitted(settings: Settings | None = None) -> None:
    """Fail closed. Called immediately before any real-money code path."""
    s = settings or get_settings()
    if s.mode is not TradingMode.LIVE:
        raise RuntimeError(
            f"Live execution refused: TRADING_MODE is {s.mode.value}, not LIVE"
        )
    if not s.live_trading_enabled:
        raise RuntimeError(
            "Live execution refused: LIVE_TRADING_ENABLED is false (rule 15)"
        )


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
