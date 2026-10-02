"""Trading engine supervisor.

This process owns every running bot. It is deliberately independent of the API
and of the browser: closing the dashboard does not stop trading, and restarting
the API does not interrupt a bot (Phase 6 requirement).

On startup it:
  1. Validates TRADING_MODE (refuses to boot on an invalid value)
  2. Restores bots from the database, reconciling their state with reality
  3. Starts a ReconciliationWorker per broker connection
  4. Auto-starts bots flagged auto_start, unless the kill switch is engaged
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import UTC, datetime

from sqlalchemy import select

from trading_engine.bot.runtime import BotConfig, TradingBot
from trading_engine.brokers.base import BrokerInterface
from trading_engine.brokers.metaapi import MetaApiAdapter
from trading_engine.brokers.paper import PaperBroker
from trading_engine.core.settings import get_settings
from trading_engine.core.types import BotState, TradingMode, TrailingMode
from trading_engine.execution.engine import ExecutionEngine
from trading_engine.execution.events import CHANNEL_BOT, EngineEvent, EventBus
from trading_engine.market_data.feed import build_feed
from trading_engine.persistence.models import Bot, BrokerConnection
from trading_engine.persistence.repository import init_engine
from trading_engine.reconciliation.worker import ReconciliationWorker
from trading_engine.risk.engine import RiskConfig
from trading_engine.risk.sizing import PositionSizer
from trading_engine.trailing.service import TrailingConfig

logger = logging.getLogger("vantage.engine")


class EngineSupervisor:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.sessionmaker = init_engine(self.settings.database_url)
        import redis.asyncio as aioredis
        self.redis = aioredis.from_url(self.settings.redis_url, decode_responses=True)
        self.events = EventBus(self.redis)
        self.bots: dict[str, TradingBot] = {}
        self.brokers: dict[str, BrokerInterface] = {}
        self.broker_names: dict[str, str] = {}
        self.reconcilers: list[ReconciliationWorker] = []
        self._stop = asyncio.Event()
        self._kill_switch = False

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        mode = self.settings.mode
        logger.info("=" * 68)
        logger.info("Vantage trading engine starting — TRADING_MODE=%s", mode.value)
        logger.info(
            "Live execution: %s",
            "ENABLED" if self.settings.live_allowed else "DISABLED (rule 15)",
        )
        logger.info("=" * 68)

        if mode is TradingMode.LIVE and not self.settings.live_trading_enabled:
            logger.critical(
                "TRADING_MODE=LIVE but LIVE_TRADING_ENABLED=false — "
                "the engine will run, but NO live orders will be sent."
            )

        await self._load_bots()
        await self._start_reconcilers()
        asyncio.create_task(self._command_listener(), name="engine-commands")

    async def _load_bots(self) -> None:
        async with self.sessionmaker() as session:
            rows = list(await session.scalars(select(Bot)))

        for row in rows:
            try:
                await self._start_bot(row)
            except Exception as exc:
                logger.exception("Could not start bot %s (%s)", row.name, row.id)
                await self.events.publish(
                    EngineEvent.bot_state(
                        bot_id=row.id, state=BotState.ERROR.value, reason=str(exc)
                    )
                )

    async def _start_bot(self, row: Bot) -> None:
        if row.id in self.bots and self.bots[row.id].state is BotState.RUNNING:
            return

        # Resolve the broker first: connecting it populates the broker-name
        # cache that _build_config needs.
        broker = await self._broker_for(row)
        config = self._build_config(row)

        feed = build_feed(self.settings, broker)
        execution = ExecutionEngine(
            broker=broker,
            risk_engine=__import__(
                "trading_engine.risk.engine", fromlist=["RiskEngine"]
            ).RiskEngine(config.risk),
            sizer=PositionSizer(),
            event_bus=self.events,
            mode=config.mode,
            live_allowed=self.settings.live_allowed and row.live_granted,
        )

        bot = TradingBot(
            config, broker=broker, feed=feed, execution=execution, event_bus=self.events,
        )

        async def _persist(bot_id: str, state: str, reason: str) -> None:
            try:
                async with self.sessionmaker() as s:
                    db_bot = await s.get(Bot, bot_id)
                    if db_bot is not None:
                        db_bot.state = state
                        db_bot.last_heartbeat_at = datetime.now(UTC)
                        await s.commit()
            except Exception as exc:
                logger.error("Could not persist bot state: %s", exc)

        bot.persist = _persist

        # Register before starting so the supervisor always has a handle on the
        # bot and can stop it, even while it is still coming up.
        self.bots[row.id] = bot

        if row.auto_start and not self._kill_switch:
            try:
                await bot.start()
                logger.info("Bot '%s' (%s) started in %s", row.name, row.symbol, row.mode.value)
            except Exception as exc:
                logger.error("Bot '%s' auto-start failed: %s", row.name, exc)
        else:
            if self._kill_switch and row.auto_start:
                logger.warning(
                    "Bot '%s' not auto-started: supervisor kill switch is engaged", row.name
                )
            self.bots[row.id] = bot

    def _build_config(self, row: Bot) -> BotConfig:
        risk_data = row.risk_config or {}
        trailing_data = row.trailing_config or {}
        risk_kwargs = {k: v for k, v in risk_data.items() if k in RiskConfig.__dataclass_fields__}
        # The bot row is the source of truth for the mode; a stale copy in the
        # stored JSON must not win, or risk checks would run against the wrong
        # trading mode.
        risk_kwargs.pop("trading_mode", None)
        risk_kwargs["trading_mode"] = row.mode
        trailing_kwargs = {
            k: v for k, v in trailing_data.items() if k in TrailingConfig.__dataclass_fields__
        }
        if "mode" in trailing_kwargs:
            trailing_kwargs["mode"] = TrailingMode(trailing_kwargs["mode"])
        if "allowed_symbols" in risk_kwargs:
            risk_kwargs["allowed_symbols"] = tuple(risk_kwargs["allowed_symbols"])
        if "allowed_days" in risk_kwargs:
            risk_kwargs["allowed_days"] = tuple(risk_kwargs["allowed_days"])
        if "trading_window" in risk_kwargs and risk_kwargs["trading_window"] is not None:
            risk_kwargs["trading_window"] = tuple(risk_kwargs["trading_window"])

        return BotConfig(
            bot_id=row.id, user_id=row.user_id, name=row.name, symbol=row.symbol,
            strategy_name=row.strategy, mode=row.mode,
            broker_name=self.broker_names.get(row.broker_connection_id or "", "paper"),
            timeframe=row.timeframe, trailing=TrailingConfig(**trailing_kwargs),
            risk=RiskConfig(**risk_kwargs),
            live_granted=row.live_granted, auto_start=row.auto_start,
        )

    async def _broker_for(self, row: Bot) -> BrokerInterface:
        key = f"{row.user_id}:{row.broker_connection_id or 'paper'}"
        if key in self.brokers:
            broker = self.brokers[key]
            if broker.connection_state.value == "CONNECTED":
                return broker

        if row.broker_connection_id and self.settings.metaapi_enabled and self.settings.metaapi_token:
            async with self.sessionmaker() as session:
                conn = await session.get(BrokerConnection, row.broker_connection_id)
            if conn is not None:
                from trading_engine.core.crypto import decrypt
                self.broker_names[row.broker_connection_id] = conn.broker
                try:
                    broker = MetaApiAdapter(
                        token=self.settings.metaapi_token,
                        region=self.settings.metaapi_region,
                        account_login=int(decrypt(conn.login_encrypted)),
                        account_password=decrypt(conn.password_encrypted),
                        account_server=decrypt(conn.server_encrypted),
                        live_allowed=self.settings.live_allowed and row.live_granted,
                        mode=row.mode,
                    )
                    await broker.connect()
                    self.brokers[key] = broker
                    logger.info("Connected MetaApi broker for bot %s", row.name)
                    return broker
                except Exception as exc:
                    logger.error("MetaApi connect failed for %s: %s", row.name, exc)

        broker = PaperBroker(mode=row.mode)
        await broker.connect()
        self.brokers[key] = broker
        return broker

    async def _start_reconcilers(self) -> None:
        interval = self.settings.reconcile_interval_seconds
        for key, broker in self.brokers.items():
            user_id = key.split(":")[0]
            worker = ReconciliationWorker(
                sessionmaker=self.sessionmaker, broker=broker, user_id=user_id,
                event_bus=self.events,
            )
            await worker.run_once(trigger="startup")
            worker.start(interval)
            self.reconcilers.append(worker)
        if self.reconcilers:
            logger.info("Reconciliation active every %ds for %d connection(s)",
                        interval, len(self.reconcilers))

    # ------------------------------------------------------------------ #
    # Command channel (API -> engine)
    # ------------------------------------------------------------------ #
    async def _command_listener(self) -> None:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(CHANNEL_BOT)
        logger.info("Listening for bot commands")
        try:
            async for message in pubsub.listen():
                if self._stop.is_set():
                    break
                if message.get("type") != "message":
                    continue
                import json
                try:
                    cmd = json.loads(message["data"])
                except json.JSONDecodeError:
                    continue
                asyncio.create_task(self._handle_command(cmd))
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.unsubscribe(CHANNEL_BOT)
            await pubsub.aclose()

    async def _handle_command(self, cmd: dict) -> None:
        action = cmd.get("action")
        bot_id = cmd.get("bot_id")
        user_id = cmd.get("user_id")
        bot = self.bots.get(bot_id) if bot_id else None
        try:
            if action == "kill_switch":
                self._kill_switch = bool(cmd.get("engaged", True))
                for b in self.bots.values():
                    b.execution.set_kill_switch(self._kill_switch)
                if not self._kill_switch:
                    for b in self.bots.values():
                        b.execution.clear_emergency_stop()
                await self.events.publish(
                    EngineEvent.emergency(
                        "KILL_SWITCH",
                        "engaged" if self._kill_switch else "released",
                        user_id=user_id,
                    )
                )
                return

            if action == "emergency_stop":
                reason = cmd.get("reason", "manual")
                for b in self.bots.values():
                    await b.emergency_stop(reason)
                await self.events.publish(
                    EngineEvent.emergency("EMERGENCY_STOP", reason, user_id=user_id)
                )
                return

            if action == "stop_new_trades":
                for b in self.bots.values():
                    if b.state is BotState.RUNNING:
                        await b.pause()
                await self.events.publish(
                    EngineEvent.emergency("STOP_NEW_TRADES", cmd.get("reason", ""), user_id=user_id)
                )
                return

            if action == "close_all_positions":
                closed = 0
                for broker in self.brokers.values():
                    try:
                        for pos in await broker.get_positions():
                            await broker.close_position(pos.id)
                            closed += 1
                    except Exception as exc:
                        logger.error("close_all failed: %s", exc)
                await self.events.publish(
                    EngineEvent.emergency(
                        "CLOSE_ALL_POSITIONS",
                        cmd.get("reason", "manual"),
                        user_id=user_id,
                        positions_closed=closed,
                    )
                )
                return

            if bot is None:
                logger.warning("Command %s referenced unknown bot %s", action, bot_id)
                return

            if action == "start":
                await bot.start()
            elif action == "pause":
                await bot.pause()
            elif action == "stop":
                await bot.stop()
            elif action == "restart":
                await bot.stop()
                await bot.start()
            elif action == "reload":
                async with self.sessionmaker() as session:
                    row = await session.get(Bot, bot_id)
                    if row is not None:
                        await self._start_bot(row)
            elif action == "close_position":
                await bot.execution.close_position(cmd.get("position_id", ""))
            elif action == "modify_position":
                await bot.broker.modify_position(
                    cmd.get("position_id", ""),
                    stop_loss=cmd.get("stop_loss"),
                    take_profit=cmd.get("take_profit"),
                )
            else:
                logger.warning("Unknown bot command: %s", action)
        except Exception as exc:
            logger.exception("Command %s failed: %s", action, exc)
            if bot is not None:
                await self.events.publish(
                    EngineEvent.bot_state(bot_id=bot_id, state=BotState.ERROR.value, reason=str(exc))
                )

    # ------------------------------------------------------------------ #
    async def shutdown(self) -> None:
        self._stop.set()
        logger.info("Shutting down trading engine…")
        for worker in self.reconcilers:
            await worker.stop()
        for bot in self.bots.values():
            try:
                await bot.stop()
            except Exception:
                pass
        for broker in self.brokers.values():
            try:
                await broker.disconnect()
            except Exception:
                pass
        await self.redis.aclose()


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )
    supervisor = EngineSupervisor()
    await supervisor.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    await supervisor.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
