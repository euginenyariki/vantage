"""ReconciliationWorker — compares broker state to our database.

Runs on startup, on reconnect, and every N seconds. Findings are FLAGGED, not
auto-corrected, unless a predefined corrective rule applies. That distinction
matters: silently "fixing" a mismatch in live trading can turn a small
discrepancy into a large one.

Discrepancy types:
  POSITION_MISSING_LOCALLY  — broker has it, we do not
  POSITION_MISSING_BROKER  — we think it is open, broker does not
  ORDER_STATUS_MISMATCH    — same order, different status
  BALANCE_MISMATCH         — broker balance differs from ours
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_engine.brokers.base import BrokerError, BrokerInterface
from trading_engine.core.types import DiscrepancyType, PositionStatus, utcnow
from trading_engine.execution.events import EngineEvent, EventBus
from trading_engine.persistence.models import (
    BrokerConnection,
    ReconciliationRun,
)
from trading_engine.persistence.repository import TradingRepository

logger = logging.getLogger("vantage.reconciliation")

#: Predefined, opt-in corrective rules. Nothing outside this mapping is
#: ever done automatically.
CORRECTIVE_RULES: dict[str, str] = {
    DiscrepancyType.POSITION_MISSING_LOCALLY.value: "CREATE_LOCAL_RECORD",
    DiscrepancyType.POSITION_MISSING_BROKER.value: "MARK_LOCAL_CLOSED",
    DiscrepancyType.ORDER_STATUS_MISMATCH.value: "UPDATE_LOCAL_STATUS",
    DiscrepancyType.BALANCE_MISMATCH.value: "NONE",
}

SEVERITY = {
    DiscrepancyType.POSITION_MISSING_LOCALLY: "HIGH",
    DiscrepancyType.POSITION_MISSING_BROKER: "CRITICAL",
    DiscrepancyType.ORDER_STATUS_MISMATCH: "MEDIUM",
    DiscrepancyType.BALANCE_MISMATCH: "LOW",
}


@dataclass
class Discrepancy:
    kind: DiscrepancyType
    entity: str
    entity_id: str
    local_value: str | None
    broker_value: str | None
    corrective_rule: str
    severity: str
    auto_action_taken: str = "NONE"
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "entity": self.entity,
            "entity_id": self.entity_id,
            "local_value": self.local_value,
            "broker_value": self.broker_value,
            "corrective_rule": self.corrective_rule,
            "severity": self.severity,
            "auto_action_taken": self.auto_action_taken,
            "detail": self.detail,
        }


@dataclass
class ReconciliationReport:
    trigger: str
    checked_positions: int
    checked_orders: int
    discrepancies: list[Discrepancy]
    status: str
    created_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "checked_positions": self.checked_positions,
            "checked_orders": self.checked_orders,
            "discrepancy_count": len(self.discrepancies),
            "discrepancies": [d.as_dict() for d in self.discrepancies],
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


class ReconciliationWorker:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker,
        broker: BrokerInterface,
        user_id: str,
        event_bus: EventBus | None = None,
        balance_tolerance: float = 0.01,
        apply_corrections: bool = True,
    ) -> None:
        self._sm = sessionmaker
        self.broker = broker
        self.user_id = user_id
        self.events = event_bus
        self.balance_tolerance = balance_tolerance
        self.apply_corrections = apply_corrections
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self, interval_seconds: float) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(interval_seconds), name="reconciliation")

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _loop(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once(trigger="scheduled")
            except Exception as exc:
                logger.exception("Reconciliation cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------ #
    async def run_once(self, trigger: str = "manual") -> ReconciliationReport:
        found: list[Discrepancy] = []

        if self.broker.connection_state.value != "CONNECTED":
            report = ReconciliationReport(
                trigger=trigger, checked_positions=0, checked_orders=0, discrepancies=[],
                status="SKIPPED_BROKER_DISCONNECTED",
            )
            await self._persist(report)
            return report

        try:
            broker_positions = await self.broker.get_positions()
            broker_orders = await self.broker.get_orders()
            account = await self.broker.get_account()
        except BrokerError as exc:
            logger.warning("Reconciliation could not read broker: %s", exc)
            report = ReconciliationReport(
                trigger=trigger, checked_positions=0, checked_orders=0, discrepancies=[],
                status=f"ERROR: {exc}",
            )
            await self._persist(report)
            return report

        async with self._sm() as session:
            repo = TradingRepository(session)
            local_positions = await repo.open_positions(self.user_id)
            local_orders = await repo.orders_for_user(self.user_id, limit=300)
            local_open_orders = [o for o in local_orders if o.status.is_open]

            # ---- positions ------------------------------------------------ #
            local_by_key = {
                (p.symbol, p.side.value, Decimal(str(p.volume))): p for p in local_positions
            }
            broker_by_key = {
                (p.symbol, p.side.value, Decimal(str(round(p.volume, 4)))): p
                for p in broker_positions
            }

            for key, bp in broker_by_key.items():
                if key not in local_by_key:
                    found.append(
                        Discrepancy(
                            kind=DiscrepancyType.POSITION_MISSING_LOCALLY,
                            entity="position", entity_id=str(bp.id),
                            local_value=None,
                            broker_value=f"{bp.side.value} {bp.volume} @ {bp.open_price}",
                            corrective_rule=CORRECTIVE_RULES[
                                DiscrepancyType.POSITION_MISSING_LOCALLY.value
                            ],
                            severity=SEVERITY[DiscrepancyType.POSITION_MISSING_LOCALLY],
                        )
                    )

            for key, lp in local_by_key.items():
                if key not in broker_by_key:
                    found.append(
                        Discrepancy(
                            kind=DiscrepancyType.POSITION_MISSING_BROKER,
                            entity="position", entity_id=str(lp.id),
                            local_value=f"{lp.side.value} {lp.volume} @ {lp.open_price}",
                            broker_value=None,
                            corrective_rule=CORRECTIVE_RULES[
                                DiscrepancyType.POSITION_MISSING_BROKER.value
                            ],
                            severity=SEVERITY[DiscrepancyType.POSITION_MISSING_BROKER],
                        )
                    )
                    if self.apply_corrections:
                        lp.status = PositionStatus.CLOSED
                        lp.closed_at = utcnow()
                        lp.close_reason = "RECONCILIATION_CLOSED_AT_BROKER"

            # ---- orders --------------------------------------------------- #
            broker_order_map = {o.broker_order_id: o for o in broker_orders if o.broker_order_id}
            for lo in local_open_orders:
                if not lo.broker_order_id:
                    continue
                bo = broker_order_map.get(lo.broker_order_id)
                if bo is None:
                    found.append(
                        Discrepancy(
                            kind=DiscrepancyType.ORDER_STATUS_MISMATCH,
                            entity="order", entity_id=str(lo.id),
                            local_value=lo.status.value, broker_value=None,
                            corrective_rule=CORRECTIVE_RULES[
                                DiscrepancyType.ORDER_STATUS_MISMATCH.value
                            ],
                            severity=SEVERITY[DiscrepancyType.ORDER_STATUS_MISMATCH],
                        )
                    )
                    continue
                if bo.status != lo.status:
                    found.append(
                        Discrepancy(
                            kind=DiscrepancyType.ORDER_STATUS_MISMATCH,
                            entity="order", entity_id=str(lo.id),
                            local_value=lo.status.value, broker_value=bo.status.value,
                            corrective_rule=CORRECTIVE_RULES[
                                DiscrepancyType.ORDER_STATUS_MISMATCH.value
                            ],
                            severity=SEVERITY[DiscrepancyType.ORDER_STATUS_MISMATCH],
                            auto_action_taken="UPDATE_LOCAL_STATUS" if self.apply_corrections else "NONE",
                        )
                    )
                    if self.apply_corrections:
                        lo.status = bo.status
                        if bo.filled_price is not None:
                            lo.filled_price = bo.filled_price

            # ---- balance ---------------------------------------------------- #
            # Compare the broker's reported balance with the last balance we
            # stored for this user. Without a stored value there is nothing to
            # compare against, so skip rather than report a false mismatch.
            latest_local_balance: float | None = None
            conn = await session.scalar(
                select(BrokerConnection)
                .where(BrokerConnection.user_id == self.user_id)
                .order_by(BrokerConnection.last_synced_at.desc().nullslast())
                .limit(1)
            )
            if conn is not None and conn.balance is not None:
                latest_local_balance = float(conn.balance)

            if latest_local_balance is not None:
                diff = abs(latest_local_balance - account.balance)
                if diff > self.balance_tolerance:
                    found.append(
                        Discrepancy(
                            kind=DiscrepancyType.BALANCE_MISMATCH,
                            entity="account", entity_id=str(self.user_id),
                            local_value=f"{latest_local_balance:.2f}",
                            broker_value=f"{account.balance:.2f}",
                            corrective_rule=CORRECTIVE_RULES[
                                DiscrepancyType.BALANCE_MISMATCH.value
                            ],
                            severity=SEVERITY[DiscrepancyType.BALANCE_MISMATCH],
                            detail={"difference": round(diff, 4)},
                        )
                    )

            report = ReconciliationReport(
                trigger=trigger,
                checked_positions=len(local_positions),
                checked_orders=len(local_open_orders),
                discrepancies=found,
                status="OK" if not found else "DISCREPANCIES_FOUND",
            )
            await session.commit()

        await self._persist(report)

        if self.events and found:
            await self.events.publish(
                EngineEvent(
                    type="reconciliation",
                    payload=report.as_dict(),
                )
            )
        if found:
            logger.warning(
                "Reconciliation found %d discrepancy(ies): %s",
                len(found), [d.kind.value for d in found],
            )
        return report

    async def _persist(self, report: ReconciliationReport) -> None:
        try:
            async with self._sm() as session:
                session.add(
                    ReconciliationRun(
                        user_id=self.user_id, trigger=report.trigger,
                        checked_positions=report.checked_positions,
                        checked_orders=report.checked_orders,
                        discrepancies=[d.as_dict() for d in report.discrepancies],
                        status=report.status,
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.error("Could not persist reconciliation run: %s", exc)
