"""Open and historical positions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.redis_bus import publish_bot_command
from app.dependencies import get_current_user
from app.schemas import PositionModify, PositionOut
from trading_engine.core.types import PositionStatus
from trading_engine.persistence.models import Position

router = APIRouter(prefix="/positions", tags=["positions"])


async def _get_position(session: AsyncSession, user_id: str, position_id: str) -> Position:
    pos = await session.scalar(
        select(Position).where(Position.id == position_id, Position.user_id == user_id)
    )
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found")
    return pos


@router.get("", response_model=list[PositionOut])
async def list_positions(
    open_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[Position]:
    stmt = select(Position).where(Position.user_id == user.id)
    if open_only:
        stmt = stmt.where(Position.status == PositionStatus.OPEN)
    stmt = stmt.order_by(Position.opened_at.desc()).limit(limit).offset(offset)
    return list(await session.scalars(stmt))


@router.get("/{position_id}", response_model=PositionOut)
async def get_position(
    position_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Position:
    return await _get_position(session, user.id, position_id)


@router.patch("/{position_id}/modify", response_model=dict)
async def modify_position(
    position_id: str,
    payload: PositionModify,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Request a stop-loss / take-profit change.

    The broker, not this API, is the source of truth for protective levels: the
    change is forwarded to the engine and the local row is updated from the
    resulting broker event.
    """
    pos = await _get_position(session, user.id, position_id)
    if pos.status != PositionStatus.OPEN:
        raise HTTPException(status_code=409, detail="Only an open position can be modified")

    if payload.stop_loss is None and payload.take_profit is None:
        raise HTTPException(status_code=422, detail="Provide stop_loss and/or take_profit")

    changes: dict[str, float] = {}
    if payload.stop_loss is not None:
        changes["stop_loss"] = payload.stop_loss
    if payload.take_profit is not None:
        changes["take_profit"] = payload.take_profit

    await publish_bot_command(
        "modify_position", bot_id=pos.bot_id, user_id=user.id,
        position_id=pos.id, **changes,
    )
    return {
        "ok": True,
        "message": "Modification requested; awaiting broker confirmation",
        "position_id": pos.id,
    }


@router.post("/{position_id}/close", response_model=dict)
async def close_position(
    position_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    pos = await _get_position(session, user.id, position_id)
    if pos.status != PositionStatus.OPEN:
        raise HTTPException(status_code=409, detail="Position is already closed")
    await publish_bot_command(
        "close_position", bot_id=pos.bot_id, user_id=user.id, position_id=pos.id
    )
    return {
        "ok": True,
        "message": "Close requested; awaiting broker confirmation",
        "position_id": pos.id,
    }


@router.get("/summary/open-count")
async def open_count(
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    count = await session.scalar(
        select(func.count(Position.id)).where(
            Position.user_id == user.id, Position.status == PositionStatus.OPEN
        )
    )
    return {"open": int(count or 0)}
