from datetime import date, datetime, timedelta

import pytest

from backend.research.opening_gap_shadow import (
    ET,
    FROZEN_THRESHOLD_PCT,
    DecisionObservation,
    OpeningGapShadowStore,
    QuoteObservation,
)


DAY = date(2026, 8, 19)
PRIOR = date(2026, 8, 18)


def at(day, hour, minute, second=0, microsecond=0):
    return datetime(day.year, day.month, day.day, hour, minute, second, microsecond, tzinfo=ET)


def decision(*, gap=1.5, mode="live", prior_id=7, current_id=7, captured=None):
    prior_close = 20_000.0
    return DecisionObservation(
        str(DAY),
        mode,
        (captured or at(DAY, 9, 29, 1)).isoformat(),
        at(PRIOR, 15, 59).isoformat(),
        prior_close,
        prior_id,
        at(DAY, 9, 28).isoformat(),
        prior_close * (1 + gap / 100),
        current_id,
        "test-fixture",
        "abc123",
    )


def quote(ts, bid, ask):
    return QuoteObservation(str(DAY), ts.isoformat(), bid, ask, "test-fixture", "qhash")


def test_frozen_positive_gap_signals_sell_and_exact_threshold_abstains(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    assert store.record_decision(decision(gap=1.5)) is True
    row = store.session(str(DAY))
    assert row["status"] == "SIGNAL_AWAITING_ENTRY"
    assert row["side"] == "sell"
    assert row["threshold_pct"] == pytest.approx(FROZEN_THRESHOLD_PCT)

    other_day = DAY + timedelta(days=1)
    exact = DecisionObservation(
        str(other_day), "historical_replay", at(other_day, 12, 0).isoformat(),
        at(DAY, 15, 59).isoformat(), 20_000, 7,
        at(other_day, 9, 28).isoformat(),
        20_000 * (1 + FROZEN_THRESHOLD_PCT / 100), 7, "test", "",
    )
    store.record_decision(exact)
    assert store.session(str(other_day))["status"] == "NO_SIGNAL"


def test_negative_gap_signals_buy_and_uses_executable_ask_then_bid(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision(gap=-1.5))
    store.record_entry(quote(at(DAY, 9, 30, 1), 19_699.75, 19_700.25))
    store.record_delayed_entry(5, quote(at(DAY, 9, 30, 5), 19_700.75, 19_701.25))
    store.record_delayed_entry(10, quote(at(DAY, 9, 30, 10), 19_701.75, 19_702.25))
    store.record_exit(quote(at(DAY, 9, 32, 2), 19_710.75, 19_711.25))
    row = store.session(str(DAY))
    assert row["entry_price"] == pytest.approx(19_700.25)
    assert row["exit_price"] == pytest.approx(19_710.75)
    assert row["gross_points"] == pytest.approx(10.5)
    summary = store.summary()
    assert summary["cost_scenarios"]["1.0"]["mean_points"] == pytest.approx(9.5)
    assert summary["delayed_entry_diagnostics"]["5"]["mean_points"] == pytest.approx(9.5)
    assert summary["execution_authorized"] is False


def test_positive_gap_short_uses_bid_then_ask(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision(gap=1.5))
    store.record_entry(quote(at(DAY, 9, 30), 20_299.75, 20_300.25))
    store.record_exit(quote(at(DAY, 9, 32), 20_289.75, 20_290.25))
    row = store.session(str(DAY))
    assert row["entry_price"] == pytest.approx(20_299.75)
    assert row["exit_price"] == pytest.approx(20_290.25)
    assert row["gross_points"] == pytest.approx(9.5)


def test_live_decision_outside_0929_is_rejected_before_write(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    with pytest.raises(ValueError, match="09:29"):
        store.record_decision(decision(captured=at(DAY, 9, 30)))
    assert store.session(str(DAY)) is None


def test_historical_replay_is_permanently_ineligible(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision(mode="historical_replay", captured=at(DAY, 12, 0)))
    assert store.session(str(DAY))["operationally_eligible"] == 0


def test_roll_transition_is_recorded_as_refusal_not_signal(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision(prior_id=7, current_id=8))
    row = store.session(str(DAY))
    assert row["status"] == "REFUSED_DECISION"
    assert "instrument changed" in row["refusal_reason"]
    assert row["side"] is None


def test_late_entry_quote_is_durably_refused(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision())
    assert store.record_entry(quote(at(DAY, 9, 30, 6), 20_299.75, 20_300.25)) is False
    row = store.session(str(DAY))
    assert row["status"] == "REFUSED_ENTRY"
    assert row["operationally_eligible"] == 0
    assert "five seconds" in row["refusal_reason"]


def test_invalid_quote_is_durably_refused(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision())
    store.record_entry(quote(at(DAY, 9, 30), 20_300.25, 20_299.75))
    assert store.session(str(DAY))["status"] == "REFUSED_ENTRY"


def test_restart_and_identical_retry_are_idempotent(tmp_path):
    path = tmp_path / "shadow.db"
    first = OpeningGapShadowStore(path)
    observation = decision()
    assert first.record_decision(observation) is True
    second = OpeningGapShadowStore(path)
    assert second.record_decision(observation) is False
    count = second._connect().execute("SELECT COUNT(*) FROM opening_gap_sessions").fetchone()[0]
    assert count == 1


def test_conflicting_retry_is_rejected(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision(gap=1.5))
    with pytest.raises(ValueError, match="conflicting DECISION"):
        store.record_decision(decision(gap=1.6))


def test_stage_ordering_refuses_exit_before_entry(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision())
    with pytest.raises(ValueError, match="not allowed"):
        store.record_exit(quote(at(DAY, 9, 32), 20_290, 20_290.5))


def test_explicit_missing_decision_is_accounted_for(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision_refusal(
        str(DAY), "live", at(DAY, 9, 29, 30).isoformat(), "source unavailable", "test"
    )
    summary = store.summary()
    assert summary["states"] == {"REFUSED_DECISION": 1}
    assert summary["eligible_completions"] == 0


def test_schema_has_no_broker_order_or_account_fields(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    columns = {
        row[1]
        for row in store._connect().execute("PRAGMA table_info(opening_gap_sessions)")
    }
    forbidden = {"order_id", "broker_order_id", "quantity", "account", "account_id"}
    assert columns.isdisjoint(forbidden)


def test_event_audit_is_append_only_at_database_level(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision())
    conn = store._connect()
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE opening_gap_events SET event_type='X'")
    conn.rollback()
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM opening_gap_events")
    conn.rollback()


def test_commission_is_separate_from_slippage_grid(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision(gap=1.5))
    store.record_entry(quote(at(DAY, 9, 30), 20_299.75, 20_300.25))
    store.record_exit(quote(at(DAY, 9, 32), 20_289.75, 20_290.25))
    result = store.summary(commission_round_trip_usd=5.0, point_value_usd=20.0)
    assert result["commission_points"] == pytest.approx(0.25)
    assert result["cost_scenarios"]["1.0"]["mean_points"] == pytest.approx(8.25)


def test_gate_cannot_pass_with_zero_commission_or_one_month(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    result = store.summary()
    assert "actual round-trip commission must be supplied" in result["blockers"]
    assert any("calendar months" in blocker for blocker in result["blockers"])
    assert result["research_gate_passed"] is False


def test_weekend_session_is_rejected(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    saturday = date(2026, 8, 22)
    observation = DecisionObservation(
        str(saturday), "historical_replay", at(saturday, 12, 0).isoformat(),
        at(DAY, 15, 59).isoformat(), 20_000, 7,
        at(saturday, 9, 28).isoformat(), 20_300, 7, "test", "",
    )
    with pytest.raises(ValueError, match="weekend"):
        store.record_decision(observation)


def test_manual_stage_failure_is_a_terminal_audited_state(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    store.record_decision(decision())
    assert store.mark_stage_failure(str(DAY), "ENTRY", "quote feed unavailable", "test")
    row = store.session(str(DAY))
    assert row["status"] == "REFUSED_ENTRY"
    assert row["operationally_eligible"] == 0


def test_reference_close_is_durable_idempotent_and_retrievable(tmp_path):
    path = tmp_path / "shadow.db"
    store = OpeningGapShadowStore(path)
    args = (
        str(PRIOR), at(PRIOR, 15, 59).isoformat(), 20_000, 7,
        at(PRIOR, 16, 0, 1).isoformat(), "live", "test", "hash",
    )
    assert store.record_reference_close(*args) is True
    assert OpeningGapShadowStore(path).record_reference_close(*args) is False
    latest = store.latest_reference_close(str(DAY))
    assert latest["session_date"] == str(PRIOR)
    assert latest["close_price"] == pytest.approx(20_000)
    assert latest["instrument_id"] == 7
