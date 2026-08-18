"""Focused J2 broker-truth and recovery safety tests."""
import sys
import types
import pytest
from position_state import PositionStateManager
from trading import AlpacaBroker, BrokerPositionError, TradingEngine


def test_broker_position_query_failure_is_not_empty_list():
    broker = object.__new__(AlpacaBroker)
    broker._simulate = False
    broker._client = None
    broker._connected = False
    engine = object.__new__(TradingEngine)
    engine.broker = broker
    with pytest.raises(BrokerPositionError):
        engine.get_broker_positions()


def test_reconciliation_detects_side_mismatch(tmp_path):
    manager = PositionStateManager(str(tmp_path / "positions.json"))
    manager.save_positions([{"symbol": "SPY", "qty": 2, "side": "buy"}])
    result = manager.reconcile([{"symbol": "SPY", "qty": 2, "side": "sell"}])
    assert result["status"] == "inconsistent"
    assert result["inconsistencies"][0]["broker_side"] == "sell"


def test_live_initialization_failure_stays_non_simulating(monkeypatch):
    class FailingTradingClient:
        def __init__(self, **_kwargs):
            raise ConnectionError("Alpaca unavailable")

    alpaca = types.ModuleType("alpaca")
    trading_pkg = types.ModuleType("alpaca.trading")
    client_mod = types.ModuleType("alpaca.trading.client")
    client_mod.TradingClient = FailingTradingClient
    trading_pkg.client = client_mod
    alpaca.trading = trading_pkg
    monkeypatch.setitem(sys.modules, "alpaca", alpaca)
    monkeypatch.setitem(sys.modules, "alpaca.trading", trading_pkg)
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", client_mod)
    monkeypatch.setenv("APCA_API_KEY_ID", "paper-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "paper-secret")

    broker = AlpacaBroker()
    assert broker.is_simulating is False
    assert broker._connected is False
    assert isinstance(broker._initialization_error, ConnectionError)

    engine = object.__new__(TradingEngine)
    engine.broker = broker
    with pytest.raises(BrokerPositionError, match="initialization failed") as exc_info:
        engine.get_broker_positions()
    assert isinstance(exc_info.value.__cause__, ConnectionError)
    assert str(exc_info.value.__cause__) == "Alpaca unavailable"


def test_simulation_with_alpaca_credentials_fails_loudly(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "paper-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "paper-secret")
    with pytest.raises(ValueError, match="Simulation mode"):
        AlpacaBroker(simulate=True)


def test_startup_recovery_clears_block_after_retry(monkeypatch):
    class Manager:
        def load_positions(self):
            return []

        def reconcile(self, positions):
            assert positions == []
            return {"status": "ok", "adopted": [], "cleaned": [], "inconsistencies": []}

    class DB:
        def record_milestone(self, **_kwargs):
            return None

    class Patterns:
        db = DB()

    class Trading:
        def __init__(self):
            self.calls = 0
            self.position_truth = None
            self.broker = types.SimpleNamespace(is_simulating=False)

        def get_broker_positions(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary broker outage")
            return []

    monkeypatch.setitem(sys.modules, "position_state", types.SimpleNamespace(PositionStateManager=Manager))
    orch = object.__new__(__import__("main").Orchestrator)
    orch.state = types.SimpleNamespace(startup_recovery_blocked=False)
    orch._pattern_engine = Patterns()
    orch._trading_engine = Trading()

    orch._recover_positions_on_startup()
    assert orch.state.startup_recovery_blocked is True
    orch._recover_positions_on_startup()
    assert orch.state.startup_recovery_blocked is False
