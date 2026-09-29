"""Registration, login, refresh and logout.

The refresh token is returned both in the body (for non-browser clients) and
set as an HttpOnly cookie. Browsers should rely on the cookie alone: it is the
only variant a cross-site script cannot read.
"""

from __future__ import annotations

import logging
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_session
from app.dependencies import get_app_settings, get_current_user, rate_limit
from app.schemas import (
    AuthResponse,
    ErrorResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    UserOut,
)
from app.services.auth_service import (
    AuthError,
    authenticate,
    create_session,
    register_user,
    revoke_all_sessions,
    revoke_session,
    rotate_session,
)
from trading_engine.persistence.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

_REGISTRATION_LIMIT = rate_limit("auth-register", limit=5, window_seconds=3600)
_LOGIN_LIMIT = rate_limit("auth-login", limit=10, window_seconds=60)
_REFRESH_LIMIT = rate_limit("auth-refresh", limit=60, window_seconds=60)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.refresh_token_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=cast("Literal[\"lax\", \"strict\", \"none\"]", settings.cookie_samesite),
        path="/api/auth",
        domain=settings.cookie_domain,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.cookie_name, path="/api/auth", domain=settings.cookie_domain
    )


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
    dependencies=[Depends(_REGISTRATION_LIMIT)],
)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> AuthResponse:
    try:
        user = await register_user(session, payload, settings)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    tokens, raw_refresh = await create_session(
        session, user, settings,
        ip_address=_client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    _set_refresh_cookie(response, raw_refresh, settings)
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


@router.post(
    "/login",
    response_model=AuthResponse,
    responses={401: {"model": ErrorResponse}, 423: {"model": ErrorResponse}},
    dependencies=[Depends(_LOGIN_LIMIT)],
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> AuthResponse:
    try:
        user = await authenticate(session, payload.email, payload.password, settings)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    tokens, raw_refresh = await create_session(
        session, user, settings,
        ip_address=_client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    _set_refresh_cookie(response, raw_refresh, settings)
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


@router.post(
    "/refresh",
    response_model=AuthResponse,
    responses={401: {"model": ErrorResponse}},
    dependencies=[Depends(_REFRESH_LIMIT)],
)
async def refresh(
    request: Request,
    response: Response,
    body: RefreshRequest | None = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> AuthResponse:
    raw = _extract_refresh(request, body, settings)
    if not raw:
        raise HTTPException(status_code=401, detail="No refresh token supplied")
    try:
        user, tokens, new_raw = await rotate_session(session, raw, settings)
    except AuthError as exc:
        _clear_refresh_cookie(response, settings)
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    _set_refresh_cookie(response, new_raw, settings)
    return AuthResponse(user=UserOut.model_validate(user), tokens=tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    raw = _extract_refresh(request, None, settings)
    if raw:
        await revoke_session(session, raw, settings)
    _clear_refresh_cookie(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> Response:
    count = await revoke_all_sessions(session, user.id)
    logger.info("Revoked %d sessions for user %s", count, user.id)
    _clear_refresh_cookie(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


def _extract_refresh(
    request: Request, body: RefreshRequest | None, settings: Settings
) -> str | None:
    """Prefer the HttpOnly cookie; fall back to the JSON body for API clients."""
    cookie = request.cookies.get(settings.cookie_name)
    if cookie:
        return cookie
    return body.refresh_token if body else None
