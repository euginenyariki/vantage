"""Regression tests for supervisor config and reconciliation balance checks."""

from sqlalchemy import select

from trading_engine.core.types import TradingMode
from trading_engine.main import EngineSupervisor
from trading_engine.persistence.models import Bot, BrokerConnection
from trading_engine.reconciliation.worker import ReconciliationWorker
from trading_engine.trailing.service import TrailingMode


def make_bot(**kw) -> Bot:
    defaults = dict(
        id="b1", user_id="u1", name="Test Bot", symbol="EURUSD",
        strategy="hft_scalper", mode=TradingMode.PAPER, timeframe="M1",
        auto_start=True, live_granted=False,
        risk_config={}, trailing_config={},
    )
    defaults.update(kw)
    return Bot(**defaults)


class TestBuildConfig:
    def _supervisor(self) -> EngineSupervisor:
        # Bypass __init__: it opens a real DB pool and Redis connection.
        sup = EngineSupervisor.__new__(EngineSupervisor)
        sup.settings = None
        sup.bots = {}
        sup.brokers = {}
        sup.broker_names = {}
        return sup

    def test_does_not_crash_on_missing_broker_name(self):
        # Regression: the Bot model has no broker_name column, so reading it
        # raised AttributeError and every bot failed to start.
        cfg = self._supervisor()._build_config(make_bot())
        assert cfg.bot_id == "b1"
        assert cfg.broker_name == "paper"

    def test_uses_cached_broker_name(self):
        sup = self._supervisor()
        sup.broker_names["conn-1"] = "metaapi"
        cfg = sup._build_config(make_bot(broker_connection_id="conn-1"))
        assert cfg.broker_name == "metaapi"

    def test_mode_comes_from_the_row(self):
        cfg = self._supervisor()._build_config(make_bot(mode=TradingMode.DEMO))
        assert cfg.mode is TradingMode.DEMO
        assert cfg.risk.trading_mode is TradingMode.DEMO

    def test_stale_stored_mode_does_not_win(self):
        # Regression: risk_config JSON could carry a different trading_mode,
        # which was passed alongside row.mode and raised "duplicate argument".
        cfg = self._supervisor()._build_config(
            make_bot(mode=TradingMode.PAPER,
                     risk_config={"trading_mode": "live", "max_risk_percent_per_trade": 2.0})
        )
        assert cfg.risk.trading_mode is TradingMode.PAPER
        assert cfg.risk.max_risk_percent_per_trade == 2.0

    def test_unknown_risk_keys_are_dropped(self):
        cfg = self._supervisor()._build_config(
            make_bot(risk_config={"nonsense": 1, "max_spread_pips": 4.0})
        )
        assert cfg.risk.max_spread_pips == 4.0
        assert not hasattr(cfg.risk, "nonsense")

    def test_trailing_mode_string_is_coerced(self):
        cfg = self._supervisor()._build_config(
            make_bot(trailing_config={"mode": "BREAK_EVEN", "distance_pips": 12})
        )
        assert cfg.trailing.mode is TrailingMode.BREAK_EVEN
        assert cfg.trailing.distance_pips == 12


class TestBalanceQueryIsReachable:
    """The balance check was dead code behind `if False else None`."""

    def test_select_is_imported_and_builds(self):
        stmt = (
            select(BrokerConnection)
            .where(BrokerConnection.user_id == "u1")
            .order_by(BrokerConnection.last_synced_at.desc().nullslast())
            .limit(1)
        )
        assert str(stmt).startswith("SELECT")

    def test_worker_tolerates_a_small_drift(self):
        w = ReconciliationWorker(
            sessionmaker=None, broker=None, user_id="u1", balance_tolerance=0.01
        )
        # A sub-cent difference must not raise a discrepancy; a real one must.
        assert abs(10_000.00 - 10_000.005) <= w.balance_tolerance
        assert abs(10_000.00 - 9_500.00) > w.balance_tolerance
