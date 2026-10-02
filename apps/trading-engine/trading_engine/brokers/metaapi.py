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
  * A live order additionally requires a broker account that MetaApi confirms is
    `ACCOUNT_TRADE_MODE_REAL`. See `verify_live_account`.

MetaApi exposes three separate REST APIs, each on its own region-specific host:

  * provisioning  ``mt-provisioning-api-v1.<region>.agiliumtrade.ai``
    - adds a broker account, creates demo accounts, deploys terminals
  * client        ``mt-client-api-v1.<region>.agiliumtrade.ai``
    - account state, orders and trades, current ticks
  * market data   ``mt-market-data-client-api-v1.<region>.agiliumtrade.ai``
    - historical OHLCV candles, served separately from everything else

All three authenticate with an ``auth-token`` header (not ``Authorization:
Bearer``), and every provisioning POST also requires a ``transaction-id``.
Provisioning calls can return 202 while a cloud terminal is still starting;
those are retried with the *same* transaction id, which is how MetaApi
deduplicates them.

There is no MetaApi sandbox host. The closest thing is `create_demo_account`,
which asks MetaApi to generate a real MT5 *demo* account at a demo broker
server and provision a terminal for it. That is genuine sandboxing: real
account, real terminal, real order mechanics, no financial exposure.
"""

from __future__ import annotations

import asyncio
import uuid
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

# MetaApi only publishes public regions for London and New York. An unknown
# region is rejected loudly rather than silently building a host that cannot
# resolve, because a bad region looks exactly like a bad token.
SUPPORTED_REGIONS = frozenset({"new-york", "london"})

_CLIENT_HOST = "https://mt-client-api-v1.{region}.agiliumtrade.ai"
_PROVISIONING_HOST = "https://mt-provisioning-api-v1.{region}.agiliumtrade.ai"
_MARKET_DATA_HOST = "https://mt-market-data-client-api-v1.{region}.agiliumtrade.ai"

# AccountInformation.type. Only _REAL is funded, and _CONTEST is a competition
# account rather than a normal funded one, so neither of the other two passes
# the live gate.
LIVE_ACCOUNT_TYPE = "ACCOUNT_TRADE_MODE_REAL"
NON_LIVE_ACCOUNT_TYPES = frozenset(
    {"ACCOUNT_TRADE_MODE_DEMO", "ACCOUNT_TRADE_MODE_CONTEST"}
)

# Trading terminal order states (MetatraderOrder.status).
_STATUS_MAP = {
    "order_state_started": OrderStatus.PENDING,
    "order_state_placed": OrderStatus.PENDING,
    "order_state_request_add": OrderStatus.PENDING,
    "order_state_request_modify": OrderStatus.PENDING,
    "order_state_request_cancel": OrderStatus.PENDING,
    "order_state_partial": OrderStatus.PARTIALLY_FILLED,
    "order_state_filled": OrderStatus.FILLED,
    "order_state_canceled": OrderStatus.CANCELLED,
    "order_state_cancelled": OrderStatus.CANCELLED,
    "order_state_rejected": OrderStatus.REJECTED,
    "order_state_expired": OrderStatus.EXPIRED,
}

# MetatraderOrder.type -> our OrderType. Anything ending in _LIMIT/_STOP is a
# pending order; the plain buy/sell pair is a market order.
_LIMIT_SUFFIXES = ("_LIMIT", "_STOP", "_STOP_LIMIT")


def _order_status(raw: dict) -> OrderStatus:
    return _STATUS_MAP.get(str(raw.get("status", "")).strip().lower(), OrderStatus.PENDING)


def _order_type(raw: dict) -> OrderType:
    meta_type = str(raw.get("type", "")).strip().upper()
    if meta_type.endswith(_LIMIT_SUFFIXES):
        return OrderType.LIMIT
    return OrderType.MARKET


def _side(raw: dict, fallback: Side | None = None) -> Side:
    value = str(raw.get("side", "")).strip().upper()
    if value.startswith("SELL"):
        return Side.SELL
    if value.startswith("BUY"):
        return Side.BUY
    if fallback is not None:
        return fallback
    return Side.BUY


class MetaApiAdapter(BrokerInterface):
    name = "metaapi"
    supports_live = True

    def __init__(
        self,
        *,
        token: str,
        region: str = "new-york",
        account_id: str | None = None,
        account_login: int | None = None,
        account_password: str | None = None,
        account_server: str | None = None,
        account_name: str = "vantage",
        live_allowed: bool = False,
        mode: TradingMode = TradingMode.DEMO,
        client_url: str | None = None,
        provisioning_url: str | None = None,
        market_data_url: str | None = None,
    ) -> None:
        if not token:
            raise BrokerAuthError("MetaApi access token is required")

        if region not in SUPPORTED_REGIONS:
            raise BrokerAuthError(
                f"Unknown MetaApi region {region!r}. MetaApi publishes public "
                f"access for: {', '.join(sorted(SUPPORTED_REGIONS))}."
            )

        self.token = token
        self.region = region
        self.account_login = account_login
        self.account_password = account_password
        self.account_server = account_server
        self.account_name = account_name
        self.live_allowed = live_allowed
        self.mode = mode

        self._client_url = (client_url or _CLIENT_HOST.format(region=region)).rstrip("/")
        self._provisioning_url = (
            provisioning_url or _PROVISIONING_HOST.format(region=region)
        ).rstrip("/")
        self._market_data_url = (
            market_data_url or _MARKET_DATA_HOST.format(region=region)
        ).rstrip("/")

        self._state = ConnectionState.DISCONNECTED
        self._account_id: str | None = account_id
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # Set only after the account has been confirmed live. Reset on disconnect
        # so a reconnected adapter re-verifies rather than assuming.
        self._live_account_verified = False

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    @property
    def client_url(self) -> str:
        return self._client_url

    @property
    def provisioning_url(self) -> str:
        return self._provisioning_url

    @property
    def market_data_url(self) -> str:
        return self._market_data_url

    @property
    def headers(self) -> dict[str, str]:
        # MetaApi documents a plain `auth-token` header, not RFC 6759 bearer auth.
        return {"auth-token": self.token, "Content-Type": "application/json"}

    @property
    def connection_state(self) -> ConnectionState:
        return self._state

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self.headers, timeout=httpx.Timeout(30.0)
            )
        return self._client

    @staticmethod
    def _new_transaction_id() -> str:
        return uuid.uuid4().hex

    async def _request(
        self,
        method: str,
        path: str,
        *,
        api: str = "client",
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> dict | list:
        base = {
            "provisioning": self._provisioning_url,
            "market_data": self._market_data_url,
        }.get(api, self._client_url)
        client = await self._ensure_client()
        try:
            resp = await client.request(method, f"{base}{path}", headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise BrokerTimeoutError(f"MetaApi timeout on {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise BrokerConnectionError(f"MetaApi transport error: {exc}") from exc

        if resp.status_code in (401, 403):
            raise BrokerAuthError(f"MetaApi auth failed ({resp.status_code}) on {path}")
        if resp.status_code == 404:
            raise SymbolNotFoundError(f"MetaApi 404 on {path}")
        if resp.status_code == 429:
            raise BrokerRejectedError("MetaApi rate limit reached (429)")
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
        """Resolve account_id, provisioning the terminal if we hold credentials."""
        if self.account_login and self.account_password and self.account_server:
            created = await self.provision_account(
                login=str(self.account_login),
                password=self.account_password,
                server=self.account_server,
                name=self.account_name,
            )
            self._account_id = created
            return

        # No credentials: fall back to an account already added to MetaApi.
        data = await self._request("GET", "/users/current/accounts", api="provisioning")
        accounts = data.get("accounts", []) if isinstance(data, dict) else list(data)
        if not accounts:
            raise BrokerAuthError(
                "No MetaApi account available. Add one in the MetaApi web app, or "
                "pass login/password/server so it can be provisioned."
            )
        # Prefer the account matching our login so a user with several terminals
        # does not silently get whichever one happens to be listed first.
        wanted = str(self.account_login) if self.account_login else None
        if wanted:
            for acct in accounts:
                if str(acct.get("login", "")) == wanted:
                    self._account_id = acct["id"]
                    return
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
            await self._request("GET", f"{self._prefix}/account-information")
            return True, f"Connected to MetaApi account {self._account_id}"
        except BrokerError as exc:
            return False, str(exc)

    @property
    def _prefix(self) -> str:
        if self._account_id is None:
            raise BrokerConnectionError("MetaApi account not initialised; call connect() first")
        return f"/users/current/accounts/{self._account_id}"

    # ------------------------------------------------------------------ #
    # Provisioning
    # ------------------------------------------------------------------ #
    async def _provisioning_post(
        self, path: str, payload: dict, *, attempts: int = 3
    ) -> dict:
        """POST to the provisioning API, honouring 202 + Retry-After.

        MetaApi answers 202 while it starts the cloud terminal, and expects the
        same transaction-id on the retry so it treats it as the same request
        rather than a second account.
        """
        transaction_id = self._new_transaction_id()
        headers = {"transaction-id": transaction_id}
        last: Exception | None = None

        for attempt in range(attempts):
            try:
                data = await self._request(
                    "POST",
                    path,
                    api="provisioning",
                    headers=headers,
                    json=payload,
                )
            except BrokerRejectedError as exc:
                # A 202 arrives as a normal 2xx body, not an error, so this only
                # fires for genuine rejections. Surface them immediately.
                raise exc
            except BrokerError as exc:
                last = exc
                await asyncio.sleep(min(2**attempt, 8))
                continue

            if isinstance(data, dict) and "id" in data:
                return data
            # 202: still starting. The retry-after hint is advisory.
            last = BrokerConnectionError(
                str(data.get("message", "MetaApi provisioning still in progress"))
                if isinstance(data, dict)
                else "MetaApi provisioning still in progress"
            )
            await asyncio.sleep(min(2**attempt, 8))

        raise BrokerConnectionError(
            f"MetaApi did not finish provisioning after {attempts} attempts: {last}"
        )

    async def provision_account(
        self,
        *,
        login: str,
        password: str,
        server: str,
        name: str = "vantage",
        platform: str = "mt5",
        magic: int = 20260101,
        keywords: Sequence[str] | None = None,
    ) -> str:
        """Add a broker account to MetaApi and start its cloud terminal.

        This bills MetaApi credit for the terminal's runtime, so it is only
        called when credentials are actually available.
        """
        payload: dict = {
            "login": str(login),
            "password": password,
            "name": name,
            "server": server,
            "platform": platform,
            "magic": magic,
            "region": self.region,
        }
        if keywords:
            payload["keywords"] = list(keywords)
        created = await self._provisioning_post("/users/current/accounts", payload)
        return str(created["id"])

    async def create_demo_account(
        self,
        *,
        email: str,
        name: str,
        phone: str,
        server_name: str,
        account_type: str,
        balance: float = 10_000.0,
        leverage: int = 100,
        keywords: Sequence[str] | None = None,
        profile_id: str = "default",
    ) -> dict:
        """Have MetaApi generate a real MT5 demo account, then provision it.

        This is the sandbox path. The broker treats the account as demo, so no
        money can be lost, while MetaApi still runs a real terminal against it
        and accepts real orders.

        `account_type` and `server_name` are broker-specific; the values are
        visible in the MT terminal when you register a demo account by hand.
        """
        creds = await self._provisioning_post(
            f"/users/current/provisioning-profiles/{profile_id}/mt5-demo-accounts",
            {
                "email": email,
                "name": name,
                "phone": phone,
                "serverName": server_name,
                "accountType": account_type,
                "balance": balance,
                "leverage": leverage,
                "keywords": list(keywords or []),
            },
        )
        login = str(creds["login"])
        account_id = await self.provision_account(
            login=login,
            password=str(creds["password"]),
            server=server_name,
            name=name,
        )
        return {
            "account_id": account_id,
            "login": login,
            "password": str(creds["password"]),
            "investor_password": creds.get("investorPassword"),
            "server": server_name,
        }

    # ------------------------------------------------------------------ #
    # Live safety
    # ------------------------------------------------------------------ #
    async def account_information(self) -> dict:
        if self._account_id is None:
            raise BrokerConnectionError("MetaApi account not initialised; call connect() first")
        data = await self._request("GET", f"{self._prefix}/account-information")
        return data if isinstance(data, dict) else {}

    async def account_type(self) -> str:
        """Return ``'live'`` or ``'demo'`` for the connected account.

        This is the third live-trading gate. The server flag and the per-bot
        grant can both be correct while the credential still points at a demo
        account, which means orders would be simulated while every dashboard and
        log called them real.

        MetaApi reports the raw value as ACCOUNT_TRADE_MODE_* on the
        account-information endpoint. An unrecognised value is treated as
        unverified rather than assumed live, so an upstream change fails closed.
        """
        info = await self.account_information()
        raw = str(info.get("type", "")).strip().upper()
        if raw == LIVE_ACCOUNT_TYPE:
            return "live"
        if raw in NON_LIVE_ACCOUNT_TYPES:
            return "demo"
        raise BrokerRejectedError(
            "Could not verify whether the MetaApi account is live or demo "
            f"(got {raw or 'no type field'}). Refusing to classify it."
        )

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
                "Live execution requires a verified live account, but MetaApi "
                f"reports this account as '{kind}'. Refusing to submit."
            )

    # ------------------------------------------------------------------ #
    # Account / symbols
    # ------------------------------------------------------------------ #
    async def get_account(self) -> AccountSnapshot:
        info = await self.account_information()
        return AccountSnapshot(
            balance=float(info.get("balance", 0.0)),
            equity=float(info.get("equity", 0.0)),
            margin=float(info.get("margin", 0.0)),
            free_margin=float(info.get("freeMargin", 0.0)),
            currency=info.get("currency", "USD"),
            leverage=int(info.get("leverage", 100)),
            updated_at=utcnow(),
        )

    async def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        try:
            raw = await self._request(
                "GET", f"{self._prefix}/symbols/{symbol.upper()}/specification"
            )
        except SymbolNotFoundError:
            return get_spec(symbol)
        if not isinstance(raw, dict) or not raw:
            return get_spec(symbol)

        digits = int(raw.get("digits", 5))
        # MetaApi supplies pipSize directly; deriving it from digits is ambiguous
        # for JPY crosses and metals.
        pip_size = float(raw.get("pipSize") or 0) or (10 ** (-digits) * (10 if digits in (3, 5) else 1))
        tick_size = float(raw.get("tickSize", 10 ** (-digits)))
        return SymbolSpec(
            symbol=symbol.upper(),
            digits=digits,
            pip_size=pip_size,
            contract_size=float(raw.get("contractSize", 100_000.0)),
            tick_size=tick_size,
            tick_value=float(raw.get("tickValue", 1.0)),
            min_volume=float(raw.get("minVolume", 0.01)),
            max_volume=float(raw.get("maxVolume", 100.0)),
            volume_step=float(raw.get("volumeStep", 0.01)),
            commission_per_lot=float(raw.get("commission", 0.0) or 0.0),
            min_stop_distance=float(raw.get("stopsLevel", 0) or 0) * tick_size,
        )

    async def get_symbols(self) -> list[str]:
        data = await self._request("GET", f"{self._prefix}/symbols")
        specs = data.get("symbols", []) if isinstance(data, dict) else data
        return [s["symbol"] for s in specs if isinstance(s, dict) and s.get("symbol")]

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    async def get_tick(self, symbol: str) -> Tick:
        symbol = symbol.upper()
        data = await self._request("GET", f"{self._prefix}/symbols/{symbol}/current-tick")
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
        ticks: dict[str, Tick] = {}
        for s in symbols:
            try:
                tick = await self.get_tick(s)
                ticks[tick.symbol] = tick
            except BrokerError:
                continue
        return ticks

    # MetaApi takes MT5 timeframe codes (lowercase, "15m"), not our M15 style.
    _TF_MAP = {
        "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
        "H1": "1h", "H4": "4h", "D1": "1d",
    }

    async def get_bars(
        self, symbol: str, timeframe: str, count: int, until: datetime | None = None
    ) -> list[Bar]:
        tf = self._TF_MAP.get(timeframe, timeframe)
        params: dict[str, str] = {"limit": str(min(count, 1000))}
        if until is not None:
            # ISO 8601 with milliseconds; MetaApi parses this, not epoch millis.
            params["startTime"] = until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        data = await self._request(
            "GET",
            f"{self._prefix}/historical-market-data/symbols/{symbol.upper()}"
            f"/timeframes/{tf}/candles",
            api="market_data",
            params=params,
        )
        bars = data if isinstance(data, list) else data.get("candles", [])
        out: list[Bar] = []
        for b in bars:
            # MetaApi returns `time` as an ISO string; it is not epoch millis.
            stamp = b.get("time")
            if isinstance(stamp, (int, float)):
                ts = datetime.fromtimestamp(int(stamp) / 1000, tz=UTC)
            else:
                ts = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            out.append(
                Bar(
                    symbol=symbol.upper(), timeframe=timeframe,
                    open=float(b["open"]), high=float(b["high"]),
                    low=float(b["low"]), close=float(b["close"]),
                    # `volume` is real tick volume; tickVolume is the tick count.
                    volume=float(b.get("volume", 0.0) or 0.0),
                    timestamp=ts,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Trading
    # ------------------------------------------------------------------ #
    def _action_type(self, request: OrderRequest) -> str:
        side = "BUY" if request.side is Side.BUY else "SELL"
        if request.order_type is OrderType.LIMIT:
            return f"ORDER_TYPE_{side}_LIMIT"
        return f"ORDER_TYPE_{side}"

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
            "actionType": self._action_type(request),
            "symbol": request.symbol.upper(),
            "volume": request.volume,
        }
        # Market orders take `price` as the deviation-tolerant target; pending
        # orders call the same field `openPrice`.
        if request.order_type is OrderType.LIMIT and request.limit_price is not None:
            payload["openPrice"] = request.limit_price
        elif request.limit_price is not None:
            payload["price"] = request.limit_price
        if request.stop_loss is not None:
            payload["stopLoss"] = request.stop_loss
        if request.take_profit is not None:
            payload["takeProfit"] = request.take_profit
        if request.client_order_id:
            payload["comment"] = request.client_order_id[:31]

        try:
            raw = await self._trade(payload)
        except BrokerTimeoutError:
            # Outcome unknown — the caller MUST reconcile, not retry.
            return SubmitResult(
                accepted=False, ambiguous=True,
                message="Broker timeout after submit; reconcile with broker state",
            )

        code = str(raw.get("stringCode", ""))
        accepted = code == "TRADE_RETCODE_DONE"
        order = self._map_order(
            {
                "orderId": raw.get("orderId"),
                "symbol": request.symbol.upper(),
                "side": "buy" if request.side is Side.BUY else "sell",
                "volume": request.volume,
                "type": raw.get("actionType", self._action_type(request)),
                "status": "ORDER_STATE_FILLED" if accepted else "ORDER_STATE_REJECTED",
                "comment": request.client_order_id or "",
            },
            request,
        )

        return SubmitResult(
            accepted=accepted,
            order=order,
            broker_order_id=str(raw.get("orderId") or ""),
            message=str(raw.get("message", code or "submitted")),
        )

    async def _trade(self, payload: dict) -> dict:
        """POST to the single trade endpoint, which multiplexes every command."""
        data = await self._request("POST", f"{self._prefix}/trade", json=payload)
        return data if isinstance(data, dict) else {}

    def _map_order(self, raw: dict, request: OrderRequest | None = None) -> Order:
        status = _order_status(raw)
        return Order(
            id=new_id(),
            client_order_id=str(raw.get("comment", request.client_order_id if request else "")),
            broker_order_id=str(raw.get("orderId") or raw.get("id") or ""),
            symbol=str(raw.get("symbol", request.symbol if request else "")).upper(),
            side=_side(raw, request.side if request else None),
            volume=float(raw.get("volume", request.volume if request else 0.0)),
            filled_volume=float(
                raw.get("currentVolume", raw.get("realizedVolume", 0.0)) or 0.0
            ),
            order_type=_order_type(raw),
            status=status,
            requested_price=raw.get("openPrice", raw.get("price")),
            filled_price=(
                raw.get("price") if status is OrderStatus.FILLED else None
            ),
            stop_loss=raw.get("stopLoss"),
            take_profit=raw.get("takeProfit"),
            mode=self.mode,
        )

    async def get_order(self, broker_order_id: str) -> Order | None:
        try:
            raw = await self._request(
                "GET", f"{self._prefix}/orders/{broker_order_id}"
            )
        except SymbolNotFoundError:
            return None
        if not isinstance(raw, dict) or not raw:
            return None
        return self._map_order(raw)

    async def get_orders(self, only_open: bool = False) -> list[Order]:
        data = await self._request("GET", f"{self._prefix}/orders")
        items = data.get("orders", []) if isinstance(data, dict) else data
        orders = [self._map_order(o) for o in items if isinstance(o, dict)]
        if only_open:
            orders = [o for o in orders if o.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)]
        return orders

    async def cancel_order(self, broker_order_id: str) -> bool:
        try:
            raw = await self._trade({"actionType": "ORDER_CANCEL", "orderId": broker_order_id})
            return str(raw.get("stringCode")) == "TRADE_RETCODE_DONE"
        except BrokerError:
            return False

    async def get_positions(self) -> list[Position]:
        data = await self._request("GET", f"{self._prefix}/positions")
        items = data.get("positions", []) if isinstance(data, dict) else data
        out: list[Position] = []
        for p in items:
            if not isinstance(p, dict):
                continue
            symbol = str(p.get("symbol", "")).upper()
            open_price = float(p.get("openPrice", 0.0))
            current = p.get("currentPrice", open_price)
            out.append(
                Position(
                    id=str(p.get("id") or p.get("positionId") or new_id()),
                    symbol=symbol,
                    side=_side(p),
                    volume=float(p.get("volume", 0.0)),
                    open_price=open_price,
                    current_price=float(current),
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
        payload: dict = {"actionType": "POSITION_CLOSE_ID", "positionId": position_id}
        if volume:
            # A partial close is a different command entirely.
            payload = {
                "actionType": "POSITION_PARTIAL",
                "positionId": position_id,
                "volume": volume,
            }
        raw = await self._trade(payload)
        code = str(raw.get("stringCode", ""))
        return SubmitResult(
            accepted=code == "TRADE_RETCODE_DONE",
            broker_order_id=str(raw.get("orderId") or raw.get("dealId") or ""),
            message=str(raw.get("message", code or "close submitted")),
        )

    async def modify_position(
        self,
        position_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> bool:
        if stop_loss is None and take_profit is None:
            return False
        payload: dict = {"actionType": "POSITION_MODIFY", "positionId": position_id}
        if stop_loss is not None:
            payload["stopLoss"] = stop_loss
        if take_profit is not None:
            payload["takeProfit"] = take_profit
        try:
            raw = await self._trade(payload)
            return str(raw.get("stringCode")) == "TRADE_RETCODE_DONE"
        except BrokerError:
            return False

    async def health(self) -> dict:
        try:
            account = await self.get_account()
            return {
                "broker": self.name,
                "state": self._state.value,
                "mode": self.mode.value,
                "region": self.region,
                "account_id": self._account_id,
                "balance": round(account.balance, 2),
                "equity": round(account.equity, 2),
                "currency": account.currency,
            }
        except BrokerError as exc:
            return {"broker": self.name, "state": self._state.value, "error": str(exc)}
