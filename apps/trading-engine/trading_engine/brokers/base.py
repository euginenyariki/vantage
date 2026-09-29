"""BrokerInterface — the only contract the engine trades through.

Rule 4: nothing above this line knows a broker exists. Rule 6: a submit call
returning 200 does NOT mean filled — every implementation must expose enough to
reconcile against, and `get_order` is the way to find out what really happened.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from trading_engine.core.types import (
    AccountSnapshot,
    Order,
    OrderRequest,
    Position,
    SymbolSpec,
    Tick,
)


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


class BrokerError(Exception):
    """Base. `retryable` drives the execution engine's reconciliation path."""

    retryable = False


class BrokerAuthError(BrokerError):
    retryable = False


class BrokerConnectionError(BrokerError):
    retryable = True


class BrokerTimeoutError(BrokerError):
    """Ambiguous outcome: the order may or may not have reached the broker.
    NEVER blindly retry — query broker state first (rule 6)."""

    retryable = False


class BrokerRejectedError(BrokerError):
    retryable = False


class SymbolNotFoundError(BrokerError):
    retryable = False


class InvalidVolumeError(BrokerError):
    retryable = False


@dataclass
class SubmitResult:
    accepted: bool
    order: Order | None = None
    broker_order_id: str | None = None
    message: str = ""
    ambiguous: bool = False   # True when we cannot yet tell if it landed


@dataclass
class Bar:
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: datetime

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


class BrokerInterface(abc.ABC):
    """Abstract broker adapter."""

    name: str = "abstract"
    supports_live: bool = False

    # ---- lifecycle ----------------------------------------------------- #
    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abc.abstractmethod
    def connection_state(self) -> ConnectionState: ...

    @abc.abstractmethod
    async def test_connection(self) -> tuple[bool, str]: ...

    # ---- account ------------------------------------------------------- #
    @abc.abstractmethod
    async def get_account(self) -> AccountSnapshot: ...

    @abc.abstractmethod
    async def get_symbol_spec(self, symbol: str) -> SymbolSpec: ...

    @abc.abstractmethod
    async def get_symbols(self) -> list[str]: ...

    # ---- market data --------------------------------------------------- #
    @abc.abstractmethod
    async def get_tick(self, symbol: str) -> Tick: ...

    @abc.abstractmethod
    async def get_ticks(self, symbols: Sequence[str]) -> dict[str, Tick]: ...

    @abc.abstractmethod
    async def get_bars(
        self, symbol: str, timeframe: str, count: int, until: datetime | None = None
    ) -> list[Bar]: ...

    # ---- trading ------------------------------------------------------- #
    @abc.abstractmethod
    async def submit_order(self, request: OrderRequest) -> SubmitResult: ...

    @abc.abstractmethod
    async def get_order(self, broker_order_id: str) -> Order | None: ...

    @abc.abstractmethod
    async def get_orders(self, only_open: bool = False) -> list[Order]: ...

    @abc.abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool: ...

    @abc.abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    async def close_position(
        self, position_id: str, volume: float | None = None
    ) -> SubmitResult: ...

    @abc.abstractmethod
    async def modify_position(
        self,
        position_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> bool: ...

    @abc.abstractmethod
    async def health(self) -> dict: ...
