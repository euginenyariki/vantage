"""Authentication service: registration, login, refresh, and session revocation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.schemas import RegisterRequest, TokenPair
from trading_engine.persistence.models import AuditLog, User, UserSession

logger = logging.getLogger(__name__)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


class AuthError(Exception):
    """Authentication failed. Carries a user-safe message."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(
        select(User).where(func.lower(User.email) == email.lower())
    )


async def register_user(
    session: AsyncSession, payload: RegisterRequest, settings: Settings
) -> User:
    if await get_user_by_email(session, payload.email) is not None:
        raise AuthError("An account with that email already exists", status_code=409)

    user = User(
        email=payload.email.lower(),
        name=payload.name,
        password_hash=hash_password(payload.password),
        country=payload.country,
        terms_accepted_at=datetime.now(timezone.utc),
        is_active=True,
    )
    session.add(user)
    await session.flush()
    session.add(
        AuditLog(
            user_id=user.id,
            actor_email=user.email,
            action="user.registered",
            entity_type="user",
            entity_id=user.id,
        )
    )
    await session.commit()
    await session.refresh(user)
    return user


def _is_locked(user: User, now: datetime) -> bool:
    return user.locked_until is not None and user.locked_until > now


async def authenticate(
    session: AsyncSession, email: str, password: str, settings: Settings
) -> User:
    now = datetime.now(timezone.utc)
    user = await get_user_by_email(session, email)
    # Always run a verification so a missing account and a wrong password take
    # a comparable amount of time.
    stored = user.password_hash if user else (
        "$argon2id$v=19$m=65536,t=3,p=4$ZHV2$(not-a-real-hash)"
    )
    password_ok = verify_password(password, stored)

    if user is None or not password_ok:
        if user is not None:
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                logger.warning("Locked account %s after repeated failures", user.email)
            await session.commit()
        raise AuthError("Incorrect email or password", status_code=401)

    if _is_locked(user, now):
        raise AuthError(
            "Account is temporarily locked. Try again later.", status_code=423
        )
    if not user.is_active:
        raise AuthError("Account is disabled", status_code=403)

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    session.add(
        AuditLog(
            user_id=user.id, actor_email=user.email, action="user.login",
            entity_type="user", entity_id=user.id,
        )
    )
    await session.commit()
    await session.refresh(user)
    return user


async def create_session(
    session: AsyncSession, user: User, settings: Settings,
    *, ip_address: str | None = None, user_agent: str | None = None,
) -> tuple[TokenPair, str]:
    """Issue an access/refresh pair. Returns (tokens, plaintext refresh)."""
    raw_refresh, digest = create_refresh_token(settings)
    access = create_access_token(user.id, role=user.role or "USER", settings=settings)
    tokens = TokenPair(
        access_token=access,
        expires_in=settings.access_token_minutes * 60,
    )
    session.add(
        UserSession(
            user_id=user.id,
            refresh_token_hash=digest,
            csrf_token_hash="",
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_days),
            ip_address=(ip_address or "")[:64] or None,
            user_agent=(user_agent or "")[:512] or None,
        )
    )
    await session.commit()
    return tokens, raw_refresh


async def rotate_session(
    session: AsyncSession, raw_refresh: str, settings: Settings
) -> tuple[User, TokenPair, str]:
    """Exchange a refresh token for a new pair, revoking the old session."""
    from app.core.security import hash_refresh_token

    digest = hash_refresh_token(raw_refresh, settings)
    row = await session.scalar(
        select(UserSession)
        .where(UserSession.refresh_token_hash == digest)
        .with_for_update()
    )
    if row is None or row.revoked_at is not None:
        raise AuthError("Invalid refresh token")
    if row.expires_at < datetime.now(timezone.utc):
        raise AuthError("Refresh token expired")

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise AuthError("Account is unavailable", status_code=403)

    # Single-use: revoking first means a stolen token cannot outlive one refresh.
    row.revoked_at = datetime.now(timezone.utc)
    await session.flush()
    tokens, new_raw = await create_session(
        session, user, settings,
        ip_address=row.ip_address, user_agent=row.user_agent,
    )
    return user, tokens, new_raw


async def revoke_session(session: AsyncSession, raw_refresh: str, settings: Settings) -> None:
    from app.core.security import hash_refresh_token

    row = await session.scalar(
        select(UserSession).where(
            UserSession.refresh_token_hash == hash_refresh_token(raw_refresh, settings)
        )
    )
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await session.commit()


async def revoke_all_sessions(session: AsyncSession, user_id: str) -> int:
    rows = (
        await session.scalars(
            select(UserSession).where(
                UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.revoked_at = now
    await session.commit()
    return len(rows)
