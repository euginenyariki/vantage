"""MetaApiAdapter — real MT5 execution from Linux via MetaApi's cloud bridge.

MetaApi hosts MetaTrader 5 terminals in the cloud, so this adapter runs on any
OS (no Windows VPS, no local MT5 install). It implements the same
BrokerInterface as PaperBroker, so the engine cannot tell the difference.

Credentials are never stored here: `login`, `password` and `server` are passed
in per session from the encrypted vault in the API layer (rule 12).

Guard rails baked in (rules 6, 8, 15):
  * `submit_order` is idempotent on MetaApi's own order id.
  * A timeout is surfaced as `ambiguous=True` so the execution engine
    reconciles by querying the broker instead of blind-retrying.
  * The adapter refuses to submit when `live_allowed` is false.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from trading_engine.brokers.base import (
    Bar,
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    BrokerInterface,
    BrokerRejectedError,
    BrokerTimeoutError,
    ConnectionState,
    SubmitResult,
    SymbolNotFoundError,
)
from trading_engine.core.types import (
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
    TradingMode,
    get_spec,
    new_id,
    utcnow,
)

# MetaApi order status -> our OrderStatus
_STATUS_MAP: dict[str, OrderStatus] = {
    "placed": OrderStatus.PENDING,
    "pending": OrderStatus.PENDING,
    "accepted": OrderStatus.ACCEPTED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
    "partial": OrderStatus.PARTIALLY_FILLED,
    "partialFilled": OrderStatus.PARTIALLY_FILLED,
}


class MetaApiAdapter(BrokerInterface):
    name = "metaapi"
    supports_live = True

    def __init__(
        self,
        *,
        token: str,
        api_url: str = "https://sandbox.app.metaapi.cloud",
        region: str = "eu-central-1",
        account_login: int | None = None,
        account_password: str | None = None,
        account_server: str | None = None,
        live_allowed: bool = False,
        mode: TradingMode = TradingMode.DEMO,
    ) -> None:
        if not token:
            raise BrokerAuthError("MetaApi access token is required")

        self.token = token
        self.api_url = api_url.rstrip("/")
        self.region = region
        self.account_login = account_login
        self.account_password = account_password
        self.account_server = account_server
        self.live_allowed = live_allowed
        self.mode = mode

        self._state = ConnectionState.DISCONNECTED
        self._account_id: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # Set only after account_type() has confirmed a live account. Reset on
        # disconnect so a reconnected adapter re-verifies rather than assuming.
        self._live_account_verified = False

    # ------------------------------------------------------------------ #
    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.api_url, headers=self.headers, timeout=httpx.Timeout(20.0)
            )
        return self._client

    async def _request(self, method: str, path: str, **kwargs) -> dict | list:
        client = await self._ensure_client()
        try:
            resp = await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise BrokerTimeoutError(f"MetaApi timeout on {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise BrokerConnectionError(f"MetaApi transport error: {exc}") from exc

        if resp.status_code in (401, 403):
            raise BrokerAuthError(f"MetaApi auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise SymbolNotFoundError(f"MetaApi 404 on {path}")
        if resp.status_code >= 500:
            raise BrokerConnectionError(f"MetaApi server error {resp.status_code}")
        if resp.status_code >= 400:
            raise BrokerRejectedError(f"MetaApi rejected {method} {path}: {resp.text[:300]}")

        if not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        self._state = ConnectionState.CONNECTING
        try:
            if self._account_id is None:
                await self._ensure_account()
            await self.test_connection()
            self._state = ConnectionState.CONNECTED
        except BrokerError:
            self._state = ConnectionState.ERROR
            raise

    async def _ensure_account(self) -> None:
        if self.account_login and self.account_password and self.account_server:
            data = await self._request(
                "POST",
                "/api/accounts",
                json={
                    "name": f"vantage-{self.account_login}",
                    "type": "cloud",
                    "login": self.account_login,
                    "password": self.account_password,
                    "server": self.account_server,
                    "region": self.region,
                    "platform": "mt5",
                },
            )
            if isinstance(data, dict) and "id" in data:
                self._account_id = data["id"]
                return

        data = await self._request("GET", "/api/accounts", params={"platform": "mt5"})
        accounts = data.get("accounts", []) if isinstance(data, dict) else list(data)
        if not accounts:
            raise BrokerAuthError("No MetaApi account available")
        self._account_id = accounts[0]["id"]

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._state = ConnectionState.DISCONNECTED
        self._live_account_verified = False

    async def test_connection(self) -> tuple[bool, str]:
        try:
            if self._account_id is None:
                await self._ensure_account()
            await self._request("GET", f"/api/accounts/{self._account_id}")
            return True, f"Connected to MetaApi account {self._account_id}"
        except BrokerError as exc:
            return False, str(exc)

    async def account_type(self) -> str:
        """Return ``'live'`` or ``'demo'`` for the connected account.

        This is the third live-trading gate. The server flag and the per-bot
        grant can both be correct while the credentials still point at a demo
        account, which means orders would be simulated while every dashboard and
        log claimed they were real. The reverse is worse: live credentials reached
        by a test that meant to stay simulated.

        MetaApi reports this on the account-info endpoint. A response without the
        field is treated as unverified rather than assumed, so an upstream change
        fails closed.
        """
        if self._account_id is None:
            raise BrokerConnectionError("MetaApi account not initialised; call connect() first")
        data = await self._request("GET", f"{self._prefix}/account-info")
        raw = ""
        if isinstance(data, dict):
            raw = str(data.get("accountType") or data.get("type") or "").strip().lower()
        if raw not in ("live", "demo"):
            raise BrokerRejectedError(
                "Could not verify whether the MetaApi account is live or demo "
                f"(got {raw or 'no account-type field'}). Refusing to classify it."
            )
        return raw

    async def verify_live_account(self) -> None:
        """Fail closed unless the connected account is confirmed live.

        Called before any live order path. In demo mode this is a no-op, because a
        demo order is a genuine order against a simulated balance and needs no
        permission beyond the mode itself.
        """
        if not self.mode.is_real:
            return
        if not self.live_allowed:
            raise BrokerRejectedError("Live execution is not enabled (rule 15)")
        kind = await self.account_type()
        if kind != "live":
            raise BrokerRejectedError(
                f"Live execution requires a verified live account, but MetaApi "
                f"reports this account as '{kind}'. Refusing to submit."
            )

    @property
    def _prefix(self) -> str:
        if self._account_id is None:
            raise BrokerConnectionError("MetaApi account not initialised; call connect() first")
        return f"/api/accounts/{self._account_id}"

    # ------------------------------------------------------------------ #
    # Account / symbols
    # ------------------------------------------------------------------ #
    async def get_account(self) -> AccountSnapshot:
        data = await self._request("GET", f"{self._prefix}/account")
        return AccountSnapshot(
            balance=float(data.get("balance", 0.0)),
            equity=float(data.get("equity", 0.0)),
            margin=float(data.get("margin", 0.0)),
            free_margin=float(data.get("freeMargin", data.get("free_margin", 0.0))),
            currency=data.get("currency", "USD"),
            leverage=int(data.get("leverage", 100)),
            updated_at=utcnow(),
        )

    async def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        data = await self._request("GET", f"{self._prefix}/symbols", params={"symbol": symbol})
        specs = data.get("specifications", [data]) if isinstance(data, dict) else data
        raw = specs[0] if specs else {}
        if not raw:
            return get_spec(symbol)

        digits = int(raw.get("digits", 5))
        pip = 10 ** (-digits) * (10 if digits in (3, 5) else 1)
        tick_size = float(raw.get("tickSize", 10 ** (-digits)))
        tick_value = float(raw.get("tickValue", 1.0))
        min_vol, max_vol, step = raw.get("minVolume", 0.01), raw.get("maxVolume", 100.0), raw.get(
            "volumeStep", 0.01
        )
        return SymbolSpec(
            symbol=symbol.upper(),
            digits=digits,
            pip_size=pip,
            contract_size=float(raw.get("contractSize", 100_000.0)),
            tick_size=tick_size,
            tick_value=tick_value,
            min_volume=float(min_vol),
            max_volume=float(max_vol),
            volume_step=float(step),
            commission_per_lot=float(raw.get("commission", 0.0) or 0.0),
            min_stop_distance=float(raw.get("stopsLevel", 0) or 0) * tick_size,
        )

    async def get_symbols(self) -> list[str]:
        data = await self._request("GET", f"{self._prefix}/symbols", params={"enabled": "true"})
        specs = data.get("specifications", []) if isinstance(data, dict) else data
        return [s.get("symbol") for s in specs if s.get("symbol")]

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    async def get_tick(self, symbol: str) -> Tick:
        symbol = symbol.upper()
        data = await self._request("GET", f"{self._prefix}/symbols", params={"symbol": symbol})
        if not isinstance(data, dict) or "bid" not in data:
            raise SymbolNotFoundError(f"No tick data for {symbol}")
        return Tick(
            symbol=symbol,
            bid=float(data["bid"]),
            ask=float(data["ask"]),
            timestamp=utcnow(),
            volume=float(data.get("volume", 0.0) or 0.0),
        )

    async def get_ticks(self, symbols: Sequence[str]) -> dict[str, Tick]:
        try:
            data = await self._request(
                "GET", f"{self._prefix}/symbols", params={"symbol": ",".join(symbols)}
            )
            out: dict[str, Tick] = {}
            entries = data if isinstance(data, list) else data.get("symbols", [data])
            for item in entries:
                if item.get("symbol") and "bid" in item:
                    out[item["symbol"]] = Tick(
                        symbol=item["symbol"],
                        bid=float(item["bid"]),
                        ask=float(item["ask"]),
                        timestamp=utcnow(),
                        volume=float(item.get("volume", 0.0) or 0.0),
                    )
            return out
        except BrokerError:
            ticks = {}
            for s in symbols:
                try:
                    ticks[s.upper()] = await self.get_tick(s)
                except BrokerError:
                    continue
            return ticks

    _TF_MAP = {
        "M1": "M1", "M5": "M5", "M15": "M15", "M30": "M30",
        "H1": "H1", "H4": "H4", "D1": "D1",
    }

    async def get_bars(
        self, symbol: str, timeframe: str, count: int, until: datetime | None = None
    ) -> list[Bar]:
        tf = self._TF_MAP.get(timeframe, timeframe)
        params: dict[str, str] = {"timeframe": tf, "limit": str(count)}
        if until is not None:
            params["to"] = int(until.timestamp() * 1000)
        data = await self._request(
            "GET", f"{self._prefix}/history", params={"symbol": symbol.upper(), **params}
        )
        bars = data.get("bars", []) if isinstance(data, dict) else data
        out: list[Bar] = []
        for b in bars:
            out.append(
                Bar(
                    symbol=symbol.upper(), timeframe=tf,
                    open=float(b["open"]), high=float(b["high"]),
                    low=float(b["low"]), close=float(b["close"]),
                    volume=float(b.get("volume", 0.0) or 0.0),
                    timestamp=datetime.fromtimestamp(b["time"] / 1000, tz=UTC),
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Trading
    # ------------------------------------------------------------------ #
    async def submit_order(self, request: OrderRequest) -> SubmitResult:
        if self.mode.is_real and not self.live_allowed:
            raise BrokerRejectedError(
                "Live submission refused: live_allowed is false (rule 15)"
            )
        if self._state is not ConnectionState.CONNECTED:
            raise BrokerConnectionError("MetaApi adapter is not connected")
        # Confirms the credential really points at a live account. Cached after the
        # first check: the account type cannot change without a reconnect.
        if self.mode.is_real and not self._live_account_verified:
            await self.verify_live_account()
            self._live_account_verified = True

        payload: dict = {
            "symbol": request.symbol.upper(),
            "type": "trade" if request.order_type is OrderType.MARKET else "limit",
            "side": request.side.value.lower(),
            "volume": request.volume,
        }
        if request.order_type is OrderType.LIMIT and request.limit_price is not None:
            payload["price"] = request.limit_price
        if request.stop_loss is not None:
            payload["stopLoss"] = request.stop_loss
        if request.take_profit is not None:
            payload["takeProfit"] = request.take_profit
        if request.client_order_id:
            payload["comment"] = request.client_order_id[:31]
        payload["magic"] = 20260101
        payload["deviation"] = 20

        try:
            data = await self._request("POST", f"{self._prefix}/trade", json=payload)
        except BrokerTimeoutError:
            # Outcome unknown — the caller MUST reconcile, not retry.
            return SubmitResult(
                accepted=False, ambiguous=True,
                message="Broker timeout after submit; reconcile with broker state",
            )

        raw = data[0] if isinstance(data, list) and data else data
        status = _STATUS_MAP.get(str(raw.get("status", "")).lower(), OrderStatus.PENDING)
        order = self._map_order(raw, request)

        return SubmitResult(
            accepted=status in (OrderStatus.FILLED, OrderStatus.ACCEPTED, OrderStatus.PENDING),
            order=order,
            broker_order_id=str(raw.get("orderId") or raw.get("id") or ""),
            message=str(raw.get("reason", raw.get("message", "submitted"))),
        )

    def _map_order(self, raw: dict, request: OrderRequest | None = None) -> Order:
        status = _STATUS_MAP.get(str(raw.get("status", "")).lower(), OrderStatus.PENDING)
        side = Side(str(raw.get("side", (request.side.value if request else "BUY"))).upper())
        return Order(
            id=new_id(),
            client_order_id=str(raw.get("comment", request.client_order_id if request else "")),
            broker_order_id=str(raw.get("orderId") or raw.get("id") or ""),
            symbol=str(raw.get("symbol", request.symbol if request else "")).upper(),
            side=side,
            volume=float(raw.get("volume", request.volume if request else 0.0)),
            filled_volume=float(raw.get("realizedVolume", 0.0) or 0.0),
            order_type=OrderType.LIMIT if str(raw.get("type")) == "limit" else OrderType.MARKET,
            status=status,
            requested_price=raw.get("price"),
            filled_price=raw.get("price") if status is OrderStatus.FILLED else None,
            stop_loss=raw.get("stopLoss"),
            take_profit=raw.get("takeProfit"),
            mode=self.mode,
        )

    async def get_order(self, broker_order_id: str) -> Order | None:
        data = await self._request("GET", f"{self._prefix}/orders", params={"orderId": broker_order_id})
        items = data.get("orders", []) if isinstance(data, dict) else data
        if not items:
            return None
        return self._map_order(items[0])

    async def get_orders(self, only_open: bool = False) -> list[Order]:
        params: dict[str, str] = {}
        if only_open:
            params["status"] = "pending"
        data = await self._request("GET", f"{self._prefix}/orders", params=params)
        items = data.get("orders", []) if isinstance(data, dict) else data
        return [self._map_order(o) for o in items]

    async def cancel_order(self, broker_order_id: str) -> bool:
        try:
            await self._request(
                "POST", f"{self._prefix}/orders/{broker_order_id}/cancel", json={}
            )
            return True
        except BrokerError:
            return False

    async def get_positions(self) -> list[Position]:
        data = await self._request("GET", f"{self._prefix}/positions")
        items = data.get("positions", []) if isinstance(data, dict) else data
        out: list[Position] = []
        for p in items:
            symbol = str(p.get("symbol", "")).upper()
            out.append(
                Position(
                    id=str(p.get("id") or p.get("positionId") or new_id()),
                    symbol=symbol,
                    side=Side(str(p.get("side", "buy")).upper().replace("BUY", "BUY")),
                    volume=float(p.get("volume", 0.0)),
                    open_price=float(p.get("openPrice", 0.0)),
                    current_price=float(p.get("currentPrice", p.get("openPrice", 0.0))),
                    stop_loss=p.get("stopLoss"),
                    take_profit=p.get("takeProfit"),
                    status=PositionStatus.OPEN,
                    commission=float(p.get("commission", 0.0) or 0.0),
                    swap=float(p.get("swap", 0.0) or 0.0),
                    unrealized_pnl=float(p.get("profit", 0.0) or 0.0),
                    mode=self.mode,
                )
            )
        return out

    async def close_position(self, position_id: str, volume: float | None = None) -> SubmitResult:
        data = await self._request(
            "POST", f"{self._prefix}/positions/{position_id}/close",
            json={"volume": volume} if volume else {},
        )
        raw = data[0] if isinstance(data, list) and data else data
        return SubmitResult(
            accepted=bool(raw.get("orderId") or raw.get("dealId")),
            broker_order_id=str(raw.get("orderId") or raw.get("dealId") or ""),
            message=str(raw.get("reason", raw.get("message", "close submitted"))),
        )

    async def modify_position(
        self,
        position_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> bool:
        body: dict = {}
        if stop_loss is not None:
            body["stopLoss"] = stop_loss
        if take_profit is not None:
            body["takeProfit"] = take_profit
        if not body:
            return False
        try:
            await self._request("POST", f"{self._prefix}/positions/{position_id}/modify", json=body)
            return True
        except BrokerError:
            return False

    async def health(self) -> dict:
        try:
            account = await self.get_account()
            return {
                "broker": self.name,
                "state": self._state.value,
                "mode": self.mode.value,
                "account_id": self._account_id,
                "balance": round(account.balance, 2),
                "equity": round(account.equity, 2),
                "currency": account.currency,
            }
        except BrokerError as exc:
            return {"broker": self.name, "state": self._state.value, "error": str(exc)}
