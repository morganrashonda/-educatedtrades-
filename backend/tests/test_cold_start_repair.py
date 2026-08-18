"""Safety and identity tests for the pattern cold-start repair."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from main import (
    Orchestrator,
    OrchestratorMode,
    PipelineState,
    learned_pattern_allows_execution,
    regime_info_from_indicators,
)
from patterns import PatternEngine


def test_each_symbol_selects_its_own_regime_and_strategy():
    info = regime_info_from_indicators({
        "SPY": {"adx": 12.0},
        "XLE": {"adx": 34.0},
        "QQQ": {"adx": 22.0},
    })

    assert info["detail"]["SPY"]["strategy"] == "mean_reversion"
    assert info["detail"]["XLE"]["strategy"] == "trend_following"
    assert info["detail"]["QQQ"]["regime"] == "transitioning"


def test_normal_size_requires_twenty_resolved_pattern_outcomes():
    assert learned_pattern_allows_execution(0.8, 19, 0.1) is False
    assert learned_pattern_allows_execution(0.8, 20, 0.1) is True
    assert learned_pattern_allows_execution(0.05, 100, 0.1) is False


def test_previous_ema_values_make_a_real_cross_part_of_pattern_identity(tmp_path):
    engine = PatternEngine(Path(tmp_path) / "patterns.db")
    signal = engine.evaluate(
        symbol="QQQ", sentiment_score=0.0, conviction_score=0.4,
        rsi_value=55.0, ema_short=101.0, ema_long=100.0,
        prev_ema_short=99.0, prev_ema_long=100.0,
    )

    assert signal.pattern_signature.ema_cross == "bullish_cross"
    assert signal.pattern_stats.count == 0
    assert signal.action == "skip"


def _orchestrator_shell():
    orch = Orchestrator.__new__(Orchestrator)
    orch.state = PipelineState(mode=OrchestratorMode.AUTONOMOUS)
    orch.high_conviction = 0.30
    orch._market_clock = MagicMock()
    orch._market_clock.is_open.return_value = True
    orch._trading_engine = MagicMock()
    orch._shadow_forward_store = MagicMock()
    orch._shadow_forward_store.evidence.return_value = {
        "paper_exploration_eligible": True, "blockers": [],
    }
    return orch


def test_paper_exploration_can_never_run_against_live_broker():
    orch = _orchestrator_shell()
    orch._trading_engine.broker.is_simulating = False
    orch._trading_engine.broker.environment = "live"

    allowed, reason, _ = orch._paper_exploration_gate(
        "p", "buy", 0.5, True, "trend_following", "trending")

    assert allowed is False
    assert "paper broker required" in reason


def test_unpromoted_shadow_does_not_query_broker_or_clock():
    orch = _orchestrator_shell()
    orch._shadow_forward_store.evidence.return_value = {
        "paper_exploration_eligible": False,
        "blockers": ["2/100 completed shadows"],
    }

    allowed, reason, _ = orch._paper_exploration_gate(
        "p", "buy", 0.5, True, "trend_following", "trending")

    assert allowed is False
    assert reason == "shadow evidence gate not met"
    orch._trading_engine.get_broker_positions.assert_not_called()
    orch._market_clock.is_open.assert_not_called()


def test_shadow_database_failure_refuses_exploration_without_raising():
    orch = _orchestrator_shell()
    orch._shadow_forward_store.evidence.side_effect = OSError("disk unavailable")

    allowed, reason, evidence = orch._paper_exploration_gate(
        "p", "buy", 0.5, True, "trend_following", "trending")

    assert allowed is False
    assert "shadow evidence unavailable" in reason
    assert evidence["paper_exploration_eligible"] is False


def test_entry_authorization_blocks_incomplete_startup_recovery():
    orch = _orchestrator_shell()
    orch.state.startup_recovery_blocked = True

    allowed, reason = orch.authorize_entry("test")

    assert allowed is False
    assert reason == "startup recovery incomplete"


def test_filled_pattern_records_actual_rsi_and_previous_emas():
    orch = _orchestrator_shell()
    orch._pattern_engine = MagicMock()
    orch._pattern_engine.record_trade_pattern_and_track.return_value = (7, "hash")
    orch.state.live_indicators = {
        "QQQ": {
            "ema_short": 101.0, "ema_long": 100.0,
            "prev_ema_short": 99.0, "prev_ema_long": 100.0,
        }
    }
    result = SimpleNamespace(
        filled_price=500.0, filled_qty=1, quantity=1,
    )
    signal = {}

    orch._record_filled_pattern(
        "QQQ", "buy", 0.4, 63.5, result, signal,
        "trend_following", "trending", "signal")

    kwargs = orch._pattern_engine.record_trade_pattern_and_track.call_args.kwargs
    assert kwargs["rsi_value"] == 63.5
    assert kwargs["prev_ema_short"] == 99.0
    assert kwargs["prev_ema_long"] == 100.0
    assert signal == {"pattern_record_id": 7, "pattern_hash": "hash"}
