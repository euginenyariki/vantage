"""End-to-end paper cycle: signal -> risk -> size -> order -> fill -> trailing -> close.

Run:  .venv/bin/python scripts/paper_cycle.py
No database, no network. Proves the core pipeline works and prints the
trailing/break-even audit trail.
"""

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_engine.bot.runtime import BotConfig, TradingBot
from trading_engine.brokers.paper import PaperBroker
from trading_engine.core.types import (
    BotState,
    Side,
    SignalAction,
    StrategySignal,
    Tick,
    TradingMode,
    get_spec,
    round_price,
    utcnow,
)
from trading_engine.execution.engine import ExecutionEngine
from trading_engine.execution.events import NullEventBus
from trading_engine.market_data.feed import ReplayMarketData
from trading_engine.risk.engine import RiskConfig, RiskContext, RiskEngine
from trading_engine.risk.sizing import PositionSizer
from trading_engine.strategies.synthetic import make_bars
from trading_engine.trailing.service import TrailingConfig

OK, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    print(f"  [{OK if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))


class CountingBus(NullEventBus):
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


async def main() -> int:
    bus = CountingBus()
    broker = PaperBroker(mode=TradingMode.PAPER, initial_balance=10_000, rng_seed=42)
    await broker.connect()

    risk_cfg = RiskConfig(
        trading_mode=TradingMode.PAPER,
        max_risk_percent_per_trade=1.0,
        max_spread_pips=5.0,
        allowed_symbols=("EURUSD",),
    )
    engine = ExecutionEngine(
        broker=broker, risk_engine=RiskEngine(risk_cfg), sizer=PositionSizer(),
        event_bus=bus, mode=TradingMode.PAPER,
    )
    feed = ReplayMarketData([])

    cfg = BotConfig(
        bot_id="smoke", user_id="smoke-user", name="Smoke", symbol="EURUSD",
        strategy_name="hft_scalper", mode=TradingMode.PAPER,
        trailing=TrailingConfig(distance_pips=12, step_pips=3,
                                break_even_trigger_pips=8, break_even_offset_pips=1),
        risk=risk_cfg,
    )
    bot = TradingBot(cfg, broker=broker, feed=feed, execution=engine, event_bus=bus)
    await bot.start()
    spec = get_spec("EURUSD")

    # ---- 1. ENTRY ------------------------------------------------------- #
    print("\n\033[1m1. Entry — strategy signal must survive risk + sizing\033[0m")
    # Build a tick series from real OHLC bars: several ticks per M1 bucket, so
    # the aggregator forms genuine candles instead of one bar per tick.
    # A deliberate uptrend, passed explicitly: this harness needs price to
    # travel far enough for the scalper to take an entry and for the trailing
    # leg to have room to run. It exercises plumbing, not strategy edge — the
    # resulting P&L says nothing about live profitability.
    bars = make_bars("EURUSD", 200, seed=9, trend_pips_per_bar=1.4, noise_pips=8)
    bot._bars_cache = list(bars[:80])
    ticks: list[Tick] = []
    for i in range(80, 200):
        bar = bars[i]
        span = bar.high - bar.low
        for sub in range(4):
            px = bar.low + span * (sub + 1) / 5
            ticks.append(Tick(
                "EURUSD",
                round_price(px, 5), round_price(px + 0.00012, 5),
                bar.timestamp + timedelta(seconds=sub * 15),
            ))

    # Pin the bot's clock to the replay timeline so the stale-data guard judges
    # each tick against the data's own time, not wall time.
    cursor = {"t": bars[80].timestamp}
    bot.clock = lambda: cursor["t"]

    # Drive the bot through its real streaming loop, not a private method.
    bot._task.cancel()
    try:
        await bot._task
    except (asyncio.CancelledError, Exception):
        pass
    bot._task = None
    replay = ReplayMarketData(ticks)
    bot.feed = replay

    entry = None
    async for tick in replay.stream("EURUSD"):
        cursor["t"] = tick.timestamp
        broker.set_quote(tick)
        await bot._on_tick(tick)
        pos = await broker.get_positions()
        if pos:
            entry = pos[0]
            break
    check("a position was opened", entry is not None,
          f"{entry.side.value} {entry.volume} @ {entry.open_price}" if entry else "no entry")
    if not entry:
        print("      rejection reasons:")
        seen: dict[str, int] = {}
        for e in bus.events:
            if e.type == "order_state" and e.payload.get("state") in {
                "RISK_REJECTED", "REJECTED"
            }:
                why = e.payload.get("risk_reason") or e.payload.get("message") or "?"
                seen[f"{e.payload['state']}: {why}"] = seen.get(
                    f"{e.payload['state']}: {why}", 0) + 1
        for reason, n in list(seen.items())[:8]:
            print(f"        {n:>3}x {reason}")
        print(f"      signals={bot.stats.signals} submitted={bot.stats.orders_submitted} "
              f"risk_rejections={bot.stats.risk_rejections}")
        print(f"      last_error={bot.stats.last_error}")
    if entry:
        check("position is tagged PAPER", entry.mode is TradingMode.PAPER, entry.mode.value)
        check("stop loss set", entry.stop_loss is not None, f"SL={entry.stop_loss}")
        # Sizing targets the budget at the quoted price; the simulated fill
        # drifts by slippage, so allow a small band above the exact budget.
        risk_amt = entry.volume * spec.value_per_price_unit(1.0) * abs(
            entry.open_price - entry.stop_loss)
        check("risk within budget + slippage tolerance", risk_amt <= 105,
              f"risk ${risk_amt:.2f} vs $100.00 budget")
        check("SL on the protective side",
              (entry.stop_loss < entry.open_price) if entry.side.value == "BUY"
              else (entry.stop_loss > entry.open_price))

    # ---- 2. TRAILING + BREAK-EVEN --------------------------------------- #
    print("\n\033[1m2. Trailing — stop must only ever move protectively\033[0m")
    if entry:
        pos = (await broker.get_positions())[0]
        start_sl = pos.stop_loss
        moves, be_seen = [], False
        price = entry.open_price
        for i in range(1, 260):
            price += 0.00008
            tick = Tick("EURUSD", round_price(price - 0.00006, 5),
                        round_price(price + 0.00006, 5), utcnow())
            broker.set_quote(tick)
            await broker.on_tick(tick)
            live = await broker.get_positions()
            if not live:
                break
            pos = live[0]
            res = bot.trailing.update(pos, tick, spec)
            if res.changed:
                ok = (res.new_stop > res.previous_stop) if pos.side.value == "BUY" \
                     else (res.new_stop < res.previous_stop)
                if not ok:
                    check("trailing never loosens the stop", False,
                          f"{res.previous_stop} -> {res.new_stop}")
                await broker.modify_position(pos.id, stop_loss=res.new_stop)
                pos.stop_loss = res.new_stop
                if res.break_even_done:
                    be_seen = True
                moves.append(res)
                if len(moves) <= 3 or res.action.value == "BREAK_EVEN_APPLIED":
                    print(f"      {res.action.value:24s} {res.previous_stop} -> {res.new_stop}")
        check("stop only moves protectively", all(
            (m.new_stop > m.previous_stop) if entry.side.value == "BUY"
            else (m.new_stop < m.previous_stop) for m in moves), f"{len(moves)} moves")
        check("break-even was applied", be_seen)
        check("stop ratcheted above entry", pos.stop_loss > start_sl,
              f"{start_sl} -> {pos.stop_loss}")

    # ---- 3. CLOSE + P/L -------------------------------------------------- #
    print("\n\033[1m3. Exit — position closes and a trade is recorded\033[0m")
    acct = await broker.get_account()
    trades = broker.paper_trades
    check("a trade was recorded", len(trades) >= 1,
          f"{len(trades)} trade(s), balance ${acct.balance:,.2f}")
    if trades:
        t = trades[-1]
        print(f"      {t.side.value} {t.volume} {t.open_price} -> {t.close_price} "
              f"P/L ${t.pnl:+.2f} ({t.reason})")
        check("trade P/L matches the balance change",
              abs((acct.balance - 10_000) - sum(x.pnl for x in trades)) < 0.01,
              f"balance Δ ${acct.balance - 10_000:+.2f}")

    # ---- 4. RISK GATES ---------------------------------------------------- #
    print("\n\033[1m4. Risk gates — each must reject with a reason\033[0m")
    r = RiskEngine(RiskConfig(trading_mode=TradingMode.PAPER, allowed_symbols=("EURUSD",),
                              max_spread_pips=2.0))
    good = Tick("EURUSD", 1.0850, 1.08505, utcnow())
    ctx = RiskContext(equity=10_000, balance=10_000, daily_pnl=0, open_positions=[],
                      now=utcnow())
    cases = [
        ("wide spread rejected",
         r.evaluate(symbol="EURUSD", side=SignalAction.BUY and Side.BUY, volume=0.1,
             tick=Tick("EURUSD", 1.0850, 1.0900, utcnow()), spec=spec, ctx=ctx)),
        ("stale tick rejected",
         r.evaluate(symbol="EURUSD", side=Side.BUY, volume=0.1,
             tick=Tick("EURUSD", 1.0850, 1.08505, utcnow().replace(year=2020)), spec=spec, ctx=ctx)),
        ("oversized volume rejected",
         r.evaluate(symbol="EURUSD", side=Side.BUY, volume=99.0,
             tick=good, spec=spec, ctx=ctx)),
        ("disconnected broker rejected",
         r.evaluate(symbol="EURUSD", side=Side.BUY, volume=0.1,
             tick=good, spec=spec, ctx=RiskContext(equity=10_000, balance=10_000, daily_pnl=0,
                                                  open_positions=[], now=utcnow(),
                                                  broker_connected=False))),
        ("SL on wrong side rejected",
         r.evaluate_stop_loss(symbol="EURUSD", side=Side.BUY, stop_loss=1.0900,
             entry_price=1.0850, tick=good, spec=spec)),
    ]
    for name, decision in cases:
        check(name, decision.blocked, decision.reason[:70])
    check("a valid trade is approved",
          r.evaluate(symbol="EURUSD", side=Side.BUY, volume=0.1,
              tick=good, spec=spec, ctx=ctx).approved)

    # ---- 5. EMERGENCY STOP ----------------------------------------------- #
    print("\n\033[1m5. Emergency stop — must block all new orders\033[0m")
    await bot.emergency_stop("smoke test")
    res = await engine.execute_signal(
        signal=StrategySignal(action=SignalAction.BUY, symbol="EURUSD", price=1.085,
                              stop_loss=1.0800, atr=0.0008),
        tick=good, ctx=ctx, bot_id="smoke",
    )
    check("new order blocked under emergency stop",
          res.state.value == "REJECTED", res.message[:60])
    check("bot state is EMERGENCY_STOP", bot.state is BotState.EMERGENCY_STOP)

    # ---- 6. IDEMPOTENCY ---------------------------------------------------- #
    print("\n\033[1m6. Idempotency — a repeated signal must not double-fill\033[0m")
    engine2 = ExecutionEngine(broker=broker, risk_engine=RiskEngine(risk_cfg),
                              sizer=PositionSizer(), event_bus=None, mode=TradingMode.PAPER)
    sig = StrategySignal(action=SignalAction.BUY, symbol="EURUSD", price=1.085,
                         stop_loss=1.0800, atr=0.0008, created_at=utcnow())
    r1 = await engine2.execute_signal(signal=sig, tick=good, ctx=ctx, bot_id="idem")
    r2 = await engine2.execute_signal(signal=sig, tick=good, ctx=ctx, bot_id="idem")
    check("first submit fills", r1.filled, r1.message[:60])
    check("replay returns the same order",
          r2.filled and r1.order and r2.order and r1.order.id == r2.order.id,
          "no duplicate order created")

    # ---- 7. LIVE MODE REFUSAL ---------------------------------------------- #
    print("\n\033[1m7. Live-mode refusal (rule 15)\033[0m")
    live_engine = ExecutionEngine(
        broker=broker, risk_engine=RiskEngine(
            RiskConfig(trading_mode=TradingMode.LIVE, live_trading_enabled=True)),
        sizer=PositionSizer(), event_bus=None,
        mode=TradingMode.LIVE, live_allowed=False,
    )
    res = await live_engine.execute_signal(signal=sig, tick=good, ctx=ctx, bot_id="live")
    check("live order refused while disabled", res.state.value == "REJECTED", res.message[:70])

    # ---- SUMMARY ------------------------------------------------------------ #
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n\033[1m{passed}/{len(results)} checks passed\033[0m")
    if passed != len(results):
        for name, ok, detail in results:
            if not ok:
                print(f"  FAILED: {name} — {detail}")
    kinds: dict[str, int] = {}
    for e in bus.events:
        kinds[e.type] = kinds.get(e.type, 0) + 1
    print(f"events published: {len(bus.events)} {kinds}")

    await bot.stop()
    await broker.disconnect()
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
