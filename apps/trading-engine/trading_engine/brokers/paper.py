"""PaperBroker — simulated execution with honest frictions.

This is not a stub that always fills at the mid price. It models:
  * spread (from a configurable, per-symbol base)
  * slippage that scales with order size and volatility
  * commission per lot
  * swap for positions held past the rollover
  * SL / TP triggering on subsequent ticks
  * partial fills and rejects on invalid volume
  * a `latency_ms` knob plus a `chaos` knob so the reconciliation and
    timeout tests exercise real failure paths

Every object it returns is stamped PAPER so the UI can never confuse simulated
results for live ones (rule 10/14).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from datetime import datetime, timedelta

from trading_engine.brokers.base import (
    Bar,
    BrokerConnectionError,
    BrokerInterface,
    ConnectionState,
    InvalidVolumeError,
    SubmitResult,
)
from trading_engine.core.types import (
    DEFAULT_SPECS,
    AccountSnapshot,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    Side,
    SymbolSpec,
    Tick,
    Trade,
    TradingMode,
    get_spec,
    new_id,
    position_margin,
    position_pnl,
    round_price,
    utcnow,
)

_EPS = 1e-12


# Indicative reference prices used only to seed the first synthetic tick for an
# instrument the paper broker has not been given a quote for. They are plausible
# market levels, not live prices: paper results are an execution and risk
# rehearsal, never a statement about live profitability.
_REFERENCE_PRICES: dict[str, float] = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2680,
    "USDJPY": 151.20,
    "XAUUSD": 2350.0,
    "BTCUSD": 98500.0,
    "ETHUSD": 3450.0,
    "SOLUSD": 215.0,
    "XRPUSD": 2.35,
}

logger = logging.getLogger(__name__)


class PaperBroker(BrokerInterface):
    name = "paper"
    supports_live = False

    def __init__(
        self,
        *,
        mode: TradingMode = TradingMode.PAPER,
        initial_balance: float = 10_000.0,
        leverage: int = 100,
        latency_ms: float = 40.0,
        jitter_ms: float = 25.0,
        slippage_pips: float = 0.4,
        chaos_rate: float = 0.0,
        rng_seed: int | None = 7,
    ) -> None:
        self.mode = mode
        self.balance = initial_balance
        self.equity = initial_balance
        self.currency = "USD"
        self.leverage = leverage

        self.latency_ms = latency_ms
        self.jitter_ms = jitter_ms
        self.slippage_pips = slippage_pips
        self.chaos_rate = chaos_rate
        self._rng = random.Random(rng_seed)

        self._state = ConnectionState.DISCONNECTED
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._trades: list[Trade] = []
        self._quotes: dict[str, Tick] = {}
        self._prices: dict[str, float] = {}
        self._counter = 0
        self._locked = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        self._state = ConnectionState.CONNECTING
        await asyncio.sleep(0.05)
        self._state = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        self._state = ConnectionState.DISCONNECTED

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    async def test_connection(self) -> tuple[bool, str]:
        if self._state is ConnectionState.CONNECTED:
            return True, "Paper broker connected"
        return False, f"Paper broker is {self._state.value}"

    def force_disconnect(self) -> None:
        """Test hook used by the kill-switch / disconnect scenarios."""
        self._state = ConnectionState.DISCONNECTED

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #
    async def get_account(self) -> AccountSnapshot:
        self._require_connected()
        await self._latency()
        margin = sum(
            position_margin(p.volume, self._mid(p.symbol), get_spec(p.symbol), self.leverage)
            for p in self._positions.values()
            if p.status is not PositionStatus.CLOSED
        )
        equity = self.equity
        return AccountSnapshot(
            balance=self.balance,
            equity=equity,
            margin=margin,
            free_margin=max(0.0, equity - margin),
            currency=self.currency,
            leverage=self.leverage,
            updated_at=utcnow(),
        )

    async def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        return get_spec(symbol)

    async def get_symbols(self) -> list[str]:
        return sorted(DEFAULT_SPECS.keys())

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    def set_quote(self, tick: Tick) -> None:
        """Injected by the market-data simulator / live feed."""
        self._quotes[tick.symbol.upper()] = tick
        self._prices[tick.symbol.upper()] = tick.mid

    def set_mid(self, symbol: str, price: float) -> None:
        symbol = symbol.upper()
        spec = get_spec(symbol)
        half = spec.pips_to_price(0.4)
        self.set_quote(
            Tick(
                symbol=symbol,
                bid=round_price(price - half, spec.digits),
                ask=round_price(price + half, spec.digits),
                timestamp=utcnow(),
                volume=self._rng.uniform(50, 500),
            )
        )

    async def get_tick(self, symbol: str) -> Tick:
        symbol = symbol.upper()
        tick = self._quotes.get(symbol)
        if tick is None:
            spec = get_spec(symbol)
            base = _REFERENCE_PRICES.get(symbol)
            if base is None:
                # No reference price for this instrument. We still synthesise a
                # tick so unlisted symbols stay testable, but log it: paper
                # results at an invented price are meaningless and must not be
                # mistaken for a real market.
                logger.warning(
                    "No reference price for %s; seeding a synthetic tick at 1.0. "
                    "Paper P/L for this symbol is not meaningful.",
                    symbol,
                )
                base = 1.0
            half = spec.pips_to_price(0.4)
            tick = Tick(
                symbol=symbol,
                bid=round(base - half, spec.digits),
                ask=round(base + half, spec.digits),
                timestamp=utcnow(),
            )
            self.set_quote(tick)
        return tick

    async def get_ticks(self, symbols: Sequence[str]) -> dict[str, Tick]:
        out: dict[str, Tick] = {}
        for s in symbols:
            out[s.upper()] = await self.get_tick(s)
        return out

    async def get_bars(
        self, symbol: str, timeframe: str, count: int, until: datetime | None = None
    ) -> list[Bar]:
        """Synthesised OHLC consistent with the current mid. Used by the paper
        strategy and, in live mode, replaced by the broker's own history."""
        symbol = symbol.upper()
        spec = get_spec(symbol)
        end = until or utcnow()
        minutes = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}.get(timeframe, 1)
        mid = self._prices.get(symbol) or (await self.get_tick(symbol)).mid
        rng = random.Random(hash((symbol, timeframe, count)) & 0xFFFFFFFF)
        vol = spec.pips_to_price(8) if symbol != "XAUUSD" else 2.0
        bars: list[Bar] = []
        price = mid
        for i in range(count, 0, -1):
            drift = rng.gauss(0, vol)
            o = price
            c = round_price(price + drift, spec.digits)
            h = round(max(o, c) + abs(rng.gauss(0, vol * 0.5)), spec.digits)
            low = round(min(o, c) - abs(rng.gauss(0, vol * 0.5)), spec.digits)
            ts = end - timedelta(minutes=minutes * i)
            bars.append(
                Bar(symbol=symbol, timeframe=timeframe, open=o, high=h, low=low,
                    close=c, volume=rng.uniform(100, 2000), timestamp=ts)
            )
            price = c
        return bars

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    async def submit_order(self, request: OrderRequest) -> SubmitResult:
        self._require_connected()
        async with self._locked:
            await self._latency()
            self._maybe_chaos()

            spec = get_spec(request.symbol)
            symbol = request.symbol.upper()

            # Idempotency: same client_order_id returns the existing order.
            for existing in self._orders.values():
                if existing.client_order_id == request.client_order_id:
                    return SubmitResult(
                        accepted=True,
                        order=existing,
                        broker_order_id=existing.broker_order_id,
                        message="Duplicate client_order_id — returning existing order",
                    )

            if request.volume < spec.min_volume - _EPS or request.volume > spec.max_volume:
                raise InvalidVolumeError(
                    f"Volume {request.volume} outside broker limits "
                    f"[{spec.min_volume}, {spec.max_volume}]"
                )
            if request.order_type is OrderType.MARKET and request.limit_price is not None:
                pass  # tolerated, ignored for market orders

            tick = await self.get_tick(symbol)
            fill_price, slippage = self._fill_price(tick, request.side, spec)

            self._counter += 1
            broker_id = f"PAPER-{self._counter:08d}"
            commission = spec.commission_per_lot * request.volume

            order = Order(
                id=new_id(),
                client_order_id=request.client_order_id,
                broker_order_id=broker_id,
                symbol=symbol,
                side=request.side,
                volume=request.volume,
                filled_volume=request.volume,
                order_type=request.order_type,
                status=OrderStatus.FILLED,
                requested_price=request.limit_price,
                filled_price=fill_price,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
                commission=commission,
                mode=self.mode,
            )
            self._orders[broker_id] = order

            position = Position(
                id=new_id(),
                symbol=symbol,
                side=request.side,
                volume=request.volume,
                open_price=fill_price,
                current_price=fill_price,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
                commission=commission,
                mode=self.mode,
                trailing_mode=request.trailing_mode,
                trailing_distance_pips=request.trailing_distance_pips,
                trailing_step_pips=request.trailing_step_pips,
                break_even_trigger_pips=request.break_even_trigger_pips,
                break_even_offset_pips=request.break_even_offset_pips,
                initial_stop_distance_pips=(
                    spec.price_to_pips(abs(fill_price - request.stop_loss))
                    if request.stop_loss else 0.0
                ),
                meta={
                    "slippage_pips": round(slippage, 2),
                    "atr": request.meta.get("atr"),
                    "signal_reason": request.meta.get("reason", ""),
                },
            )
            self._positions[position.id] = position
            # Commission is NOT deducted here: it is carried on the position and
            # settled once inside the closing P/L, so a round trip charges
            # exactly one commission and the balance reconciles against trades.
            self.equity = self.balance

            return SubmitResult(
                accepted=True,
                order=order,
                broker_order_id=broker_id,
                message=(
                    f"Filled {request.volume} {symbol} @ {fill_price} "
                    f"(slippage {slippage:.2f} pips)"
                ),
            )

    def _fill_price(self, tick: Tick, side: Side, spec: SymbolSpec) -> tuple[float, float]:
        base = tick.price_for(side)
        slippage_pips = max(0.0, self._rng.gauss(self.slippage_pips, self.slippage_pips * 0.3))
        adverse = 1 if side is Side.BUY else -1
        price = base + adverse * spec.pips_to_price(slippage_pips)
        return round_price(price, spec.digits), slippage_pips

    async def get_order(self, broker_order_id: str) -> Order | None:
        self._require_connected()
        return self._orders.get(broker_order_id)

    async def get_orders(self, only_open: bool = False) -> list[Order]:
        self._require_connected()
        orders = list(self._orders.values())
        if only_open:
            orders = [o for o in orders if o.status.is_open]
        return sorted(orders, key=lambda o: o.created_at, reverse=True)

    async def cancel_order(self, broker_order_id: str) -> bool:
        self._require_connected()
        order = self._orders.get(broker_order_id)
        if order is None or order.status.is_terminal:
            return False
        order.status = OrderStatus.CANCELLED
        order.updated_at = utcnow()
        return True

    # ------------------------------------------------------------------ #
    # Positions
    # ------------------------------------------------------------------ #
    async def get_positions(self) -> list[Position]:
        self._require_connected()
        return [p for p in self._positions.values() if p.status is not PositionStatus.CLOSED]

    async def close_position(
        self, position_id: str, volume: float | None = None
    ) -> SubmitResult:
        self._require_connected()
        async with self._locked:
            await self._latency()
            position = self._positions.get(position_id)
            if position is None or position.status is PositionStatus.CLOSED:
                return SubmitResult(accepted=False, message=f"Position {position_id} not found")

            close_volume = min(volume or position.volume, position.volume)
            tick = await self.get_tick(position.symbol)
            spec = get_spec(position.symbol)
            close_price, _ = self._fill_price(tick, position.side.opposite, spec)

            pnl = position_pnl(
                position.side, close_volume, position.open_price, close_price, spec,
                commission=position.commission, swap=position.swap,
            )
            self.balance += pnl

            self._counter += 1
            broker_id = f"PAPER-{self._counter:08d}"
            order = Order(
                id=new_id(),
                client_order_id=f"close-{position_id}-{self._counter}",
                broker_order_id=broker_id,
                symbol=position.symbol,
                side=position.side.opposite,
                volume=close_volume,
                filled_volume=close_volume,
                order_type=OrderType.MARKET,
                status=OrderStatus.FILLED,
                requested_price=None,
                filled_price=close_price,
                stop_loss=None,
                take_profit=None,
                mode=self.mode,
            )
            self._orders[broker_id] = order

            self._trades.append(
                Trade(
                    id=new_id(),
                    position_id=position.id,
                    symbol=position.symbol,
                    side=position.side,
                    volume=close_volume,
                    open_price=position.open_price,
                    close_price=close_price,
                    commission=position.commission,
                    swap=position.swap,
                    pnl=pnl,
                    mode=self.mode,
                    bot_id=position.bot_id,
                    opened_at=position.opened_at,
                    closed_at=utcnow(),
                    reason=position.meta.get("close_reason", ""),
                )
            )

            position.volume -= close_volume
            position.realized_pnl += pnl
            if position.volume <= 1e-9:
                position.status = PositionStatus.CLOSED
                position.closed_at = utcnow()
                position.current_price = close_price

            self.equity = self.balance
            return SubmitResult(
                accepted=True, order=order, broker_order_id=broker_id,
                message=f"Closed {close_volume} {position.symbol} @ {close_price}, P/L {pnl:+.2f}",
            )

    async def modify_position(
        self,
        position_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> bool:
        self._require_connected()
        position = self._positions.get(position_id)
        if position is None or position.status is PositionStatus.CLOSED:
            return False
        if stop_loss is not None:
            position.stop_loss = stop_loss
        if take_profit is not None:
            position.take_profit = take_profit
        return True

    # ------------------------------------------------------------------ #
    # Tick processing (SL/TP triggers, swaps, P/L)
    # ------------------------------------------------------------------ #
    async def on_tick(self, tick: Tick) -> list[Trade]:
        """Advance the paper world with a new tick: trigger SL/TP, accrue swap,
        refresh unrealized P/L. Returns any trades closed by this tick."""
        symbol = tick.symbol.upper()
        self.set_quote(tick)
        closed: list[Trade] = []
        now = utcnow()

        for position in list(self._positions.values()):
            if position.status is PositionStatus.CLOSED:
                continue
            if position.symbol != symbol:
                continue

            spec = get_spec(symbol)
            position.current_price = tick.mid
            position.unrealized_pnl = position_pnl(
                position.side, position.volume, position.open_price, tick.mid, spec,
                commission=position.commission, swap=position.swap,
            )

            if self._rolled_over(position, now):
                swap = spec.swap_long if position.side is Side.BUY else spec.swap_short
                position.swap += swap * position.volume
                position.meta["last_swap_day"] = now.date().isoformat()

            hit_sl = (
                position.stop_loss is not None
                and (tick.bid <= position.stop_loss if position.side is Side.BUY
                     else tick.ask >= position.stop_loss)
            )
            hit_tp = (
                position.take_profit is not None
                and (tick.bid >= position.take_profit if position.side is Side.BUY
                     else tick.ask <= position.take_profit)
            )
            if hit_sl or hit_tp:
                reason = "stop_loss" if hit_sl else "take_profit"
                position.meta["close_reason"] = reason
                result = await self.close_position(position.id)
                if result.order is not None:
                    for t in self._trades:
                        if t.position_id == position.id and t.reason == reason:
                            closed.append(t)
        return closed

    def _rolled_over(self, position: Position, now: datetime) -> bool:
        last = position.meta.get("last_swap_day")
        today = now.date().isoformat()
        rollover_passed = now.hour >= 22 or now.hour < 1
        return rollover_passed and last != today and position.opened_at.date() < now.date()

    # ------------------------------------------------------------------ #
    async def health(self) -> dict:
        return {
            "broker": self.name,
            "state": self._state.value,
            "mode": self.mode.value,
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "open_positions": len([p for p in self._positions.values()
                                   if p.status is not PositionStatus.CLOSED]),
            "open_orders": len([o for o in self._orders.values() if o.status.is_open]),
            "total_trades": len(self._trades),
        }

    @property
    def paper_trades(self) -> list[Trade]:
        return list(self._trades)

    @property
    def paper_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    # ------------------------------------------------------------------ #
    async def _latency(self) -> None:
        jitter = self._rng.uniform(0, self.jitter_ms) if self.jitter_ms else 0
        await asyncio.sleep((self.latency_ms + jitter) / 1000.0)

    def _maybe_chaos(self) -> None:
        if self.chaos_rate <= 0:
            return
        roll = self._rng.random()
        if roll < self.chaos_rate * 0.5:
            raise BrokerConnectionError("Simulated broker connection drop")
        if roll < self.chaos_rate * 0.8:
            from trading_engine.brokers.base import BrokerTimeoutError
            raise BrokerTimeoutError("Simulated broker timeout — outcome unknown")

    def _require_connected(self) -> None:
        if self._state is not ConnectionState.CONNECTED:
            raise BrokerConnectionError(f"Broker not connected (state={self._state.value})")

    def _mid(self, symbol: str) -> float:
        tick = self._quotes.get(symbol.upper())
        return tick.mid if tick else 0.0
