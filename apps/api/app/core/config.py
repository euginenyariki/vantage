"""API configuration.

Reads from the environment (and an optional ``.env``) with safe defaults so the
API can boot locally without any setup. Secrets have no usable default in
production: :func:`assert_production_ready` refuses to start if they are still
the development placeholders.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Development-only placeholders. Startup fails in production if these survive.
DEV_JWT_SECRET = "dev-insecure-jwt-secret-change-me"
DEV_REFRESH_SECRET = "dev-insecure-refresh-secret-change-me"
DEV_CREDENTIAL_KEY = "dev-insecure-credential-key-change-me"


def _repo_env_file() -> str:
    """Locate the repository-root ``.env``.

    A bare ``env_file=".env"`` is resolved against the *current working directory*,
    so the same code silently read different configuration depending on where it
    was launched from: the repo root saw the real file, while a process started in
    ``apps/api`` or ``apps/trading-engine`` saw no file at all and fell back to
    the Docker-service defaults. That failure is quiet and confusing, because
    startup succeeds and then nothing connects.

    Walking up from this file locates the one ``.env`` that belongs to the checkout,
    regardless of the caller's working directory. A packaged deployment has no such
    file and relies purely on real environment variables, which is unaffected.
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

    # --- core ------------------------------------------------------------- #
    environment: str = "development"
    debug: bool = True
    api_title: str = "Vantage API"
    api_version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8000

    # --- datastores -------------------------------------------------------- #
    database_url: str = "postgresql+asyncpg://vantage:vantage@localhost:5432/vantage"
    redis_url: str = "redis://localhost:6379/0"

    # --- auth -------------------------------------------------------------- #
    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 30
    refresh_pepper: str = DEV_REFRESH_SECRET
    cookie_name: str = "vantage_refresh"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    cookie_domain: str | None = None

    # --- CORS -------------------------------------------------------------- #
    # NoDecode hands the raw environment string to the validator below instead
    # of having pydantic-settings JSON-parse it first.
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # --- broker credentials ------------------------------------------------ #
    # Encrypts broker logins/passwords at rest. Never rotate without a re-encryption plan.
    credential_master_key: str = "dev-insecure-credential-key-change-me"
    # Accepts "1" or "v1"; .env files commonly use the prefixed form.
    credential_key_version: str = "1"
    metaapi_token: str = ""
    # The adapter derives the client, provisioning and market-data hosts itself.
    metaapi_region: str = "london"

    @property
    def credential_version_number(self) -> int:
        """Numeric form of the key version, for the encryption prefix."""
        return int(str(self.credential_key_version).strip().lstrip("vV") or 1)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, v: object) -> object:
        """Accept both a JSON array and a bare comma-separated list.

        Left to itself pydantic-settings JSON-decodes complex fields, so the
        natural ``CORS_ORIGINS=http://localhost:3000`` in a .env file raises a
        SettingsError and the API refuses to boot. Operators write both forms
        and neither is wrong, so accept either rather than failing at startup.
        """
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return []
            if text.startswith("["):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return v

    # --- trading ----------------------------------------------------------- #
    trading_mode: str = "PAPER"
    live_trading_enabled: bool = False
    market_data_stale_seconds: float = 15.0

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def assert_production_ready(settings: Settings | None = None) -> None:
    """Refuse to run production with development secrets or cookie flags off."""
    s = settings or get_settings()
    problems: list[str] = []
    if s.jwt_secret == DEV_JWT_SECRET:
        problems.append("JWT_SECRET is still the development default")
    if s.refresh_pepper == DEV_REFRESH_SECRET:
        problems.append("REFRESH_PEPPER is still the development default")
    if s.credential_master_key == DEV_CREDENTIAL_KEY:
        problems.append("CREDENTIAL_MASTER_KEY is still the development default")
    if len(s.jwt_secret) < 32:
        problems.append("JWT_SECRET must be at least 32 characters")
    if not s.cookie_secure:
        problems.append("COOKIE_SECURE must be true in production")
    if problems:
        raise RuntimeError(
            "Refusing to start in production with an insecure configuration:\n  - "
            + "\n  - ".join(problems)
        )
