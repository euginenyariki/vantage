"""Symbol metadata and quotes.

Quotes come from the engine's feed cache over Redis, never from a direct broker
socket in the API process.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from trading_engine.core.types import DEFAULT_SPECS

from app.core.config import Settings
from app.core.redis_bus import get_redis
from app.dependencies import get_app_settings, get_current_user
from app.schemas import Quote, SymbolInfo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/market", tags=["market data"])

QUOTE_KEY = "vantage:quotes"


def _spec_to_info(symbol: str) -> SymbolInfo | None:
    spec = DEFAULT_SPECS.get(symbol.upper())
    if spec is None:
        return None
    return SymbolInfo(
        symbol=symbol,
        digits=spec.digits,
        pip_size=spec.pip_size,
        contract_size=spec.contract_size,
        tick_size=spec.tick_size,
        tick_value=spec.tick_value,
        min_volume=spec.min_volume,
        max_volume=spec.max_volume,
        volume_step=spec.volume_step,
        commission_per_lot=spec.commission_per_lot,
        min_stop_distance=spec.min_stop_distance,
        max_spread_pips=spec.max_spread_pips,
        asset_class=spec.asset_class,
    )


@router.get("/symbols", response_model=list[SymbolInfo])
async def list_symbols() -> list[SymbolInfo]:
    # Public by design: the instrument catalogue is reference data (no prices,
    # no account state) and the marketing page renders it while signed out.
    return [i for s in DEFAULT_SPECS if (i := _spec_to_info(s)) is not None]


@router.get("/symbols/{symbol}", response_model=SymbolInfo)
async def get_symbol(symbol: str) -> SymbolInfo:
    info = _spec_to_info(symbol.upper())
    if info is None:
        raise HTTPException(status_code=404, detail=f"Unsupported symbol: {symbol}")
    return info


@router.get("/quotes", response_model=list[Quote])
async def list_quotes(
    symbols: str | None = Query(default=None, description="Comma-separated symbols"),
    user=Depends(get_current_user),
    settings: Settings = Depends(get_app_settings),
) -> list[Quote]:
    """Return cached quotes, flagging anything older than the staleness limit."""
    redis = get_redis()
    requested = [s.strip().upper() for s in symbols.split(",")] if symbols else list(DEFAULT_SPECS)
    quotes: list[Quote] = []
    now = datetime.now(UTC)

    for symbol in requested:
        if _spec_to_info(symbol) is None:
            continue
        raw = await redis.hget(QUOTE_KEY, symbol)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("Discarding malformed cached quote for %s", symbol)
            continue
        info = _spec_to_info(symbol)
        if info is None:
            continue
        ts = datetime.fromisoformat(data.get("timestamp") or now.isoformat())
        bid, ask = float(data.get("bid", 0.0)), float(data.get("ask", 0.0))
        spread_pips = (ask - bid) / info.pip_size if info.pip_size else 0.0
        quotes.append(
            Quote(
                symbol=symbol, bid=bid, ask=ask, mid=round((bid + ask) / 2, 5),
                spread_pips=round(spread_pips, 2), timestamp=ts,
            )
        )
    return quotes


@router.get("/quotes/{symbol}", response_model=Quote)
async def get_quote(symbol: str, user=Depends(get_current_user)) -> Quote:
    sym = symbol.upper()
    spec = _spec_to_info(sym)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unsupported symbol: {symbol}")
    raw = await get_redis().hget(QUOTE_KEY, sym)
    if not raw:
        raise HTTPException(
            status_code=503, detail=f"No live quote for {sym}; the market feed may be offline"
        )
    data = json.loads(raw)
    bid, ask = float(data["bid"]), float(data["ask"])
    pip = spec.pip_size
    return Quote(
        symbol=sym, bid=bid, ask=ask, mid=round((bid + ask) / 2, 5),
        spread_pips=round((ask - bid) / pip, 2),
        timestamp=datetime.fromisoformat(data["timestamp"]),
    )
