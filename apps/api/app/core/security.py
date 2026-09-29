"""Password hashing and token handling.

Passwords use Argon2id. Access tokens are short-lived signed JWTs; refresh tokens
are opaque random strings stored only as a peppered SHA-256 digest, so a database
leak does not hand an attacker usable sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings, get_settings

_hasher = PasswordHasher()

TokenType = Literal["access", "refresh"]


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password, returning False for any malformed hash."""
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def create_access_token(
    subject: str,
    *,
    role: str = "USER",
    settings: Settings | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    s = settings or get_settings()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=s.access_token_minutes))
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "iss": "vantage",
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str, *, expected: TokenType = "access",
                 settings: Settings | None = None) -> dict[str, Any]:
    """Decode and validate a token. Raises jwt.PyJWTError on any problem."""
    s = settings or get_settings()
    payload = jwt.decode(
        token, s.jwt_secret, algorithms=[s.jwt_algorithm], issuer="vantage"
    )
    if payload.get("type") != expected:
        raise jwt.InvalidTokenError(f"expected a {expected} token")
    return payload


def create_refresh_token(settings: Settings | None = None) -> tuple[str, str]:
    """Return (plaintext, digest). Only the digest is ever stored."""
    s = settings or get_settings()
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw, s)


def hash_refresh_token(raw: str, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    return hashlib.sha256(f"{s.refresh_pepper}:{raw}".encode()).hexdigest()


def verify_refresh_token(raw: str, digest: str, settings: Settings | None = None) -> bool:
    """Constant-time comparison of a presented refresh token against its digest."""
    return hmac.compare_digest(hash_refresh_token(raw, settings), digest)


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_code(code: str) -> str:
    """Hash a short verification code (email/2FA) for storage."""
    return hashlib.sha256(code.encode()).hexdigest()
