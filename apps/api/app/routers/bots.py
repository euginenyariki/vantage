"""Bot CRUD and lifecycle control.

Start/stop calls are forwarded to the engine over Redis and are therefore
*requests*, not confirmations: the resulting state change arrives on the event
stream. The guards here refuse impossible or unsafe requests before they are
sent, so the engine never sees one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import get_session
from app.core.redis_bus import publish_bot_command
from app.dependencies import get_app_settings, get_current_user
from app.schemas import BotActionResponse, BotCreate, BotOut, BotUpdate
from trading_engine.core.types import BotState, TradingMode
from trading_engine.persistence.models import AuditLog, Bot, BrokerConnection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bots", tags=["bots"])


async def _get_bot(session: AsyncSession, user_id: str, bot_id: str) -> Bot:
    bot = await session.scalar(
        select(Bot).where(Bot.id == bot_id, Bot.user_id == user_id)
    )
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


async def _live_blocked(
    bot: Bot, settings: Settings, session: AsyncSession
) -> str | None:
    """Return a reason string when this bot must not trade live.

    Checks, in order: server-wide switch, per-bot grant, a broker connection
    that actually belongs to this user, and that connection being verified.
    A non-null id alone is not enough - it would let a bot trade live against
    credentials nobody has ever authenticated.
    """
    if not settings.live_trading_enabled:
        return "Live trading is disabled server-wide (LIVE_TRADING_ENABLED=false)"
    if not (bot.live_granted or False):
        return "This bot has not been granted live-trading permission"
    if not bot.broker_connection_id:
        return "Live trading requires a broker connection"

    conn = await session.scalar(
        select(BrokerConnection).where(
            BrokerConnection.id == bot.broker_connection_id,
            BrokerConnection.user_id == bot.user_id,
        )
    )
    if conn is None:
        return "The configured broker connection does not belong to this account"
    if bool(conn.is_demo):
        return "This bot is pointed at a demo account; live trading needs a live connection"
    if conn.connection_state != "CONNECTED":
        return (
            "The broker connection is not verified. Run the connection test "
            f"first (current state: {conn.connection_state})."
        )
    return None


@router.get("", response_model=list[BotOut])
async def list_bots(
    state: str | None = Query(default=None),
    mode: str | None = Query(default=None),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Bot]:
    stmt = select(Bot).where(Bot.user_id == user.id)
    if state:
        stmt = stmt.where(Bot.state == BotState(state.upper()))
    if mode:
        stmt = stmt.where(Bot.mode == TradingMode(mode.upper()))
    stmt = stmt.order_by(Bot.created_at.desc())
    return list(await session.scalars(stmt))


@router.post("", response_model=BotOut, status_code=status.HTTP_201_CREATED)
async def create_bot(
    payload: BotCreate,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Bot:
    if payload.mode == TradingMode.LIVE:
        raise HTTPException(
            status_code=403,
            detail="Create the bot in PAPER or DEMO mode; live mode must be granted explicitly",
        )

    count = await session.scalar(
        select(func.count(Bot.id)).where(Bot.user_id == user.id, Bot.state != BotState.STOPPED)
    )
    if (count or 0) >= 25:
        raise HTTPException(status_code=409, detail="Bot limit reached (25 active)")

    bot = Bot(
        user_id=user.id,
        name=payload.name,
        symbol=payload.symbol,
        strategy=payload.strategy,
        timeframe=payload.timeframe,
        mode=TradingMode(payload.mode),
        state=BotState.STOPPED,
        auto_start=payload.auto_start,
        broker_connection_id=payload.broker_connection_id,
        live_granted=False,
        risk_config=payload.risk.model_dump(),
        trailing_config=payload.trailing.model_dump(),
        config={"timeframe": payload.timeframe},
    )
    session.add(bot)
    await session.flush()
    session.add(
        AuditLog(
            user_id=user.id, actor_email=user.email, action="bot.created",
            entity_type="bot", entity_id=bot.id,
            details={"symbol": payload.symbol, "mode": payload.mode},
        )
    )
    await session.commit()
    await session.refresh(bot)
    return bot


@router.get("/{bot_id}", response_model=BotOut)
async def get_bot(
    bot_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Bot:
    return await _get_bot(session, user.id, bot_id)


@router.patch("/{bot_id}", response_model=BotOut)
async def update_bot(
    bot_id: str,
    payload: BotUpdate,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> Bot:
    bot = await _get_bot(session, user.id, bot_id)

    if bot.state == BotState.RUNNING:
        raise HTTPException(
            status_code=409, detail="Stop the bot before changing its configuration"
        )

    data = payload.model_dump(exclude_unset=True)
    if "mode" in data and data["mode"] and data["mode"] != bot.mode:
        if TradingMode(data["mode"]) == TradingMode.LIVE:
            if bot.mode != TradingMode.LIVE and not bot.live_granted:
                raise HTTPException(
                    status_code=403,
                    detail="Live mode requires an explicit grant; use POST /bots/{id}/grant-live",
                )
        bot.mode = TradingMode(data["mode"])

    if "live_granted" in data:
        # Granting live rights is a deliberate, separately-audited act and is
        # not something a user can toggle on their own account.
        raise HTTPException(
            status_code=403, detail="Use POST /bots/{id}/grant-live to change live permissions"
        )

    for key in ("name", "auto_start"):
        if key in data and data[key] is not None:
            setattr(bot, key, data[key])
    for key in ("risk_config", "trailing_config"):
        if key in data and data[key] is not None:
            setattr(bot, key, data[key])

    bot.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(bot)
    return bot


@router.delete("/{bot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bot(
    bot_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    bot = await _get_bot(session, user.id, bot_id)
    if bot.state == BotState.RUNNING:
        raise HTTPException(status_code=409, detail="Stop the bot before deleting it")
    await session.delete(bot)
    await session.commit()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
@router.post("/{bot_id}/start", response_model=BotActionResponse)
async def start_bot(
    bot_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
) -> BotActionResponse:
    bot = await _get_bot(session, user.id, bot_id)
    if bot.state == BotState.RUNNING:
        return BotActionResponse(
            ok=True, message="Bot is already running", bot_id=bot.id, state=bot.state
        )
    if bot.mode == TradingMode.LIVE:
        reason = await _live_blocked(bot, settings, session)
        if reason:
            raise HTTPException(status_code=403, detail=reason)

    await publish_bot_command("start", bot_id=bot.id, user_id=user.id)
    return BotActionResponse(
        ok=True, message="Start requested; the engine will confirm via the event stream",
        bot_id=bot.id, state=bot.state,
    )


@router.post("/{bot_id}/stop", response_model=BotActionResponse)
async def stop_bot(
    bot_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BotActionResponse:
    bot = await _get_bot(session, user.id, bot_id)
    if bot.state in {BotState.STOPPED, BotState.STOPPING, BotState.OFFLINE}:
        return BotActionResponse(
            ok=True, message="Bot is already stopped", bot_id=bot.id, state=bot.state
        )
    await publish_bot_command("stop", bot_id=bot.id, user_id=user.id)
    return BotActionResponse(
        ok=True, message="Stop requested", bot_id=bot.id, state=bot.state
    )


@router.post("/{bot_id}/pause", response_model=BotActionResponse)
async def pause_bot(
    bot_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BotActionResponse:
    bot = await _get_bot(session, user.id, bot_id)
    if bot.state != BotState.RUNNING:
        raise HTTPException(status_code=409, detail="Only a running bot can be paused")
    await publish_bot_command("pause", bot_id=bot.id, user_id=user.id)
    return BotActionResponse(ok=True, message="Pause requested", bot_id=bot.id, state=bot.state)


@router.post("/{bot_id}/resume", response_model=BotActionResponse)
async def resume_bot(
    bot_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BotActionResponse:
    bot = await _get_bot(session, user.id, bot_id)
    if bot.state != BotState.PAUSED:
        raise HTTPException(status_code=409, detail="Only a paused bot can be resumed")
    await publish_bot_command("resume", bot_id=bot.id, user_id=user.id)
    return BotActionResponse(ok=True, message="Resume requested", bot_id=bot.id, state=bot.state)


@router.post("/{bot_id}/kill-switch", response_model=BotActionResponse)
async def engage_kill_switch(
    bot_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BotActionResponse:
    """Halt trading immediately. The engine flattens positions and blocks new orders."""
    bot = await _get_bot(session, user.id, bot_id)
    await publish_bot_command("kill_switch", bot_id=bot.id, user_id=user.id)
    # Latch the state in the database as well. Without this the API would
    # return the bot's previous state and the UI would keep showing a running
    # bot whenever the engine is unreachable - the worst possible outcome for a
    # control whose whole job is to stop trading. The engine republishes its own
    # state when it processes the command above.
    bot.state = BotState.EMERGENCY_STOP
    session.add(
        AuditLog(
            user_id=user.id, actor_email=user.email, action="bot.kill_switch",
            entity_type="bot", entity_id=bot.id,
        )
    )
    await session.commit()
    logger.warning("Kill switch engaged for bot %s by %s", bot.id, user.email)
    return BotActionResponse(
        ok=True, message="Emergency stop engaged", bot_id=bot.id, state=bot.state
    )


@router.post("/{bot_id}/grant-live", response_model=BotOut)
async def grant_live(
    bot_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Bot:
    bot = await _get_bot(session, user.id, bot_id)
    bot.live_granted = True
    bot.live_granted_at = datetime.now(timezone.utc)
    session.add(
        AuditLog(
            user_id=user.id, actor_email=user.email, action="bot.live_granted",
            entity_type="bot", entity_id=bot.id,
        )
    )
    await session.commit()
    await session.refresh(bot)
    logger.warning("Live trading granted for bot %s by %s", bot.id, user.email)
    return bot


@router.post("/{bot_id}/revoke-live", response_model=BotOut)
async def revoke_live(
    bot_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Bot:
    bot = await _get_bot(session, user.id, bot_id)
    bot.live_granted = False
    bot.live_granted_at = None
    if bot.mode == TradingMode.LIVE:
        bot.mode = TradingMode.PAPER
    session.add(
        AuditLog(
            user_id=user.id, actor_email=user.email, action="bot.live_revoked",
            entity_type="bot", entity_id=bot.id,
        )
    )
    await session.commit()
    await session.refresh(bot)
    return bot
