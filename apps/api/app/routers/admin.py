"""Platform administration.

Every route requires an ADMIN or SUPER_ADMIN user via ``require_admin``, and
each one returns aggregate or operational data only. Password hashes, broker
credentials and decrypted secrets are never exposed here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from trading_engine.core.types import BotState, PositionStatus
from trading_engine.persistence.models import (
    AuditLog,
    Bot,
    BrokerConnection,
    Position,
    Trade,
    User,
)

from app.core.database import get_session
from app.dependencies import rate_limit, require_admin
from app.schemas import BaseModel

router = APIRouter(prefix="/admin", tags=["admin"])

_READ = rate_limit("admin-read", limit=120, window_seconds=60)
_WRITE = rate_limit("admin-write", limit=30, window_seconds=60)


class PlatformStats(BaseModel):
    users: int = 0
    active_users: int = 0
    admins: int = 0
    bots: int = 0
    running_bots: int = 0
    broker_connections: int = 0
    connected_brokers: int = 0
    open_positions: int = 0
    trades: int = 0


class AdminUserOut(BaseModel):
    id: str
    email: str
    name: str
    country: str
    role: str
    is_active: bool
    is_email_verified: bool
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    bots: int = 0
    broker_connections: int = 0


class AdminUserList(BaseModel):
    total: int
    items: list[AdminUserOut]


class AdminBrokerOut(BaseModel):
    id: str
    user_id: str
    user_email: str
    name: str
    broker: str
    is_demo: bool
    connection_state: str
    balance: float | None = None
    equity: float | None = None
    last_synced_at: datetime | None = None
    last_error: str | None = None


class AdminBotOut(BaseModel):
    id: str
    user_id: str
    user_email: str
    name: str
    symbol: str
    strategy: str
    timeframe: str
    mode: str
    state: str
    live_granted: bool = False
    created_at: datetime | None = None


@router.get("/stats", response_model=PlatformStats)
async def stats(
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    _rl: None = Depends(_READ),
) -> PlatformStats:
    """Platform-wide counts. No user data beyond aggregates."""
    async def count(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return int(await session.scalar(stmt) or 0)

    admin_roles = ["ADMIN", "SUPER_ADMIN"]
    return PlatformStats(
        users=await count(User),
        active_users=await count(User, User.is_active.is_(True)),
        admins=int(
            await session.scalar(
                select(func.count()).select_from(User).where(User.role.in_(admin_roles))
            )
            or 0
        ),
        bots=await count(Bot),
        running_bots=await count(Bot, Bot.state == BotState.RUNNING),
        broker_connections=await count(BrokerConnection),
        connected_brokers=await count(BrokerConnection, BrokerConnection.connection_state == "CONNECTED"),
        open_positions=await count(Position, Position.status == PositionStatus.OPEN),
        trades=await count(Trade),
    )


@router.get("/users", response_model=AdminUserList)
async def list_users(
    q: str | None = Query(None, max_length=200, description="Filter by email or name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_READ),
) -> AdminUserList:
    """User directory with per-user resource counts."""
    stmt = select(User)
    if q:
        needle = f"%{q}%"
        stmt = stmt.where(User.email.ilike(needle) | User.name.ilike(needle))
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        await session.execute(
            stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    if rows:
        ids = [u.id for u in rows]
        bot_counts = dict(
            (
                await session.execute(
                    select(Bot.user_id, func.count()).where(Bot.user_id.in_(ids))
                    .group_by(Bot.user_id)
                )
            ).all()
        )
        conn_counts = dict(
            (
                await session.execute(
                    select(BrokerConnection.user_id, func.count())
                    .where(BrokerConnection.user_id.in_(ids))
                    .group_by(BrokerConnection.user_id)
                )
            ).all()
        )
    else:
        bot_counts, conn_counts = {}, {}

    return AdminUserList(
        total=total,
        items=[
            AdminUserOut(
                id=u.id,
                email=u.email,
                name=u.name,
                country=u.country,
                role=getattr(u.role, "value", str(u.role)),
                is_active=u.is_active,
                is_email_verified=u.is_email_verified,
                last_login_at=u.last_login_at,
                created_at=u.created_at,
                bots=int(bot_counts.get(u.id, 0)),
                broker_connections=int(conn_counts.get(u.id, 0)),
            )
            for u in rows
        ],
    )


@router.post("/users/{user_id}/active", response_model=AdminUserOut)
async def set_user_active(
    user_id: str,
    active: bool = Query(..., description="true to re-enable, false to disable"),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_WRITE),
) -> AdminUserOut:
    """Enable or disable an account.

    An admin cannot disable their own account, which is the usual way an admin
    panel locks everyone out of a deployment.
    """
    if user_id == admin.id and not active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot disable your own account",
        )
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.is_active = active
    if not active:
        # Disabling must also kill live sessions, otherwise the account keeps
        # trading until its access token expires.
        from trading_engine.persistence.models import UserSession

        await session.execute(
            UserSession.__table__.update()
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
    await session.commit()
    await session.refresh(user)

    session.add(
        AuditLog(
            user_id=admin.id,
            actor_email=admin.email,
            action="admin.user.active_changed",
            entity_type="user",
            entity_id=user_id,
            details={"active": active},
        )
    )
    await session.commit()

    return AdminUserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        country=user.country,
        role=getattr(user.role, "value", str(user.role)),
        is_active=user.is_active,
        is_email_verified=user.is_email_verified,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.get("/brokers", response_model=list[AdminBrokerOut])
async def list_brokers(
    limit: int = Query(50, ge=1, le=200),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_READ),
) -> list[AdminBrokerOut]:
    """Broker connections across the platform.

    Login and password are never selected here: the row is read through an
    explicit column list so encrypted credential material cannot leak into an
    admin response by accident.
    """
    rows = (
        await session.execute(
            select(
                BrokerConnection.id,
                BrokerConnection.user_id,
                BrokerConnection.name,
                BrokerConnection.broker,
                BrokerConnection.is_demo,
                BrokerConnection.connection_state,
                BrokerConnection.balance,
                BrokerConnection.equity,
                BrokerConnection.last_synced_at,
                BrokerConnection.last_error,
                User.email,
            )
            .join(User, User.id == BrokerConnection.user_id)
            .order_by(BrokerConnection.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        AdminBrokerOut(
            id=r.id,
            user_id=r.user_id,
            user_email=r.email,
            name=r.name,
            broker=r.broker,
            is_demo=r.is_demo,
            connection_state=r.connection_state,
            balance=float(r.balance) if r.balance is not None else None,
            equity=float(r.equity) if r.equity is not None else None,
            last_synced_at=r.last_synced_at,
            last_error=r.last_error,
        )
        for r in rows
    ]


@router.get("/bots", response_model=list[AdminBotOut])
async def list_bots(
    limit: int = Query(50, ge=1, le=200),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(_READ),
) -> list[AdminBotOut]:
    """Bots across the platform, including which ones hold a live grant."""
    rows = (
        await session.execute(
            select(
                Bot.id, Bot.user_id, Bot.name, Bot.symbol, Bot.strategy,
                Bot.timeframe, Bot.mode, Bot.state, Bot.live_granted, Bot.created_at,
                User.email,
            )
            .join(User, User.id == Bot.user_id)
            .order_by(Bot.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        AdminBotOut(
            id=r.id,
            user_id=r.user_id,
            user_email=r.email,
            name=r.name,
            symbol=r.symbol,
            strategy=r.strategy,
            timeframe=r.timeframe,
            mode=r.mode,
            state=getattr(r.state, "value", str(r.state)),
            live_granted=bool(r.live_granted),
            created_at=r.created_at,
        )
        for r in rows
    ]
