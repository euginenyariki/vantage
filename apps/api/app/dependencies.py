"""FastAPI dependencies: current user, sessions, Redis, and rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.core.redis_bus import get_redis
from app.core.security import decode_token
from trading_engine.persistence.models import User

# --- in-memory rate limiting (per process; fine for a single API instance) --- #
_buckets: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(
    scope: str, limit: int, window_seconds: int = 60
) -> Callable[[Request], None]:
    def _dependency(request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        key = f"{scope}:{client}"
        now = time.monotonic()
        bucket = _buckets[key]
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down.",
            )
        bucket.append(now)

    return _dependency


def get_app_settings() -> Settings:
    return get_settings()


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> User:
    """Resolve the authenticated user from the bearer token.

    Raises 401 on a missing, malformed, expired, or revoked session, and 403
    when the account is inactive.
    """
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = header.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token, expected="access", settings=settings)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if (user.role or "USER") not in {"ADMIN", "SUPER_ADMIN"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required"
        )
    return user


def get_redis_client():
    return get_redis()
