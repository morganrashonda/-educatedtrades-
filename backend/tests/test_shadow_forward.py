from datetime import date, datetime, timedelta, timezone

import pytest

from backend.shadow_forward import ShadowCandidate, ShadowForwardStore


def _ohlc(start, rows):
    return {
        "bar_dates": [start + timedelta(minutes=30 * i) for i in range(len(rows))],
        "opens": [row[0] for row in rows],
        "highs": [row[1] for row in rows],
        "lows": [row[2] for row in rows],
        "closes": [row[3] for row in rows],
    }


def _candidate(ts, pattern="p", side="buy"):
    return ShadowCandidate(
        "QQQ", ts.timestamp(), side, "trend_following", 0.4,
        "trending", pattern, 55.0, 30.0, 101.0, 100.0,
    )


def test_shadow_enters_only_on_bar_after_signal_and_deduplicates(tmp_path):
    store = ShadowForwardStore(tmp_path / "shadow.db", max_hold_bars=2)
    start = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    assert store.record_candidate(_candidate(start)) is True
    assert store.record_candidate(_candidate(start)) is False
    store.observe_bars("QQQ", _ohlc(start, [(100, 101, 99, 100)]))
    row = store._connect().execute("SELECT * FROM shadow_signals").fetchone()
    assert row["status"] == "awaiting_entry"
    store.observe_bars("QQQ", _ohlc(start, [(100, 101, 99, 100), (102, 103, 101, 102)]))
    row = store._connect().execute("SELECT * FROM shadow_signals").fetchone()
    assert row["entry_bar_ts"] == pytest.approx((start + timedelta(minutes=30)).timestamp())
    assert row["entry_price"] > 102


def test_stop_wins_when_one_bar_touches_stop_and_target(tmp_path):
    store = ShadowForwardStore(
        tmp_path / "shadow.db", max_hold_bars=2,
        round_trip_cost_bps=0, slippage_bps_per_side=0,
    )
    start = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    store.record_candidate(_candidate(start))
    store.observe_bars("QQQ", _ohlc(start, [
        (100, 101, 99, 100),
        (100, 104, 97, 100),
    ]))
    row = store._connect().execute("SELECT * FROM shadow_signals").fetchone()
    assert row["status"] == "closed"
    assert row["exit_reason"] == "stop"
    assert row["net_return"] == pytest.approx(-0.025)


def test_reobserving_same_bars_does_not_advance_hold_count(tmp_path):
    store = ShadowForwardStore(tmp_path / "shadow.db", max_hold_bars=3)
    start = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    store.record_candidate(_candidate(start))
    bars = _ohlc(start, [(100, 101, 99, 100), (100, 101, 99, 100)])
    store.observe_bars("QQQ", bars)
    before = store._connect().execute("SELECT bars_held FROM shadow_signals").fetchone()[0]
    store.observe_bars("QQQ", bars)
    after = store._connect().execute("SELECT bars_held FROM shadow_signals").fetchone()[0]
    assert before == after == 1


def test_gap_through_stop_uses_adverse_open_not_optimistic_stop(tmp_path):
    store = ShadowForwardStore(
        tmp_path / "shadow.db", max_hold_bars=3,
        round_trip_cost_bps=0, slippage_bps_per_side=0,
    )
    start = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    store.record_candidate(_candidate(start))
    store.observe_bars("QQQ", _ohlc(start, [
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (95, 96, 94, 95),
    ]))

    row = store._connect().execute("SELECT * FROM shadow_signals").fetchone()
    assert row["exit_reason"] == "stop"
    assert row["exit_price"] == pytest.approx(95.0)
    assert row["net_return"] == pytest.approx(-0.05)


def test_promotion_requires_every_gate(tmp_path):
    store = ShadowForwardStore(tmp_path / "shadow.db")
    evidence = store.evidence("missing", minimum_trades=2, minimum_days=2)
    assert evidence["paper_exploration_eligible"] is False
    assert len(evidence["blockers"]) >= 4


def test_shadow_database_is_separate_and_contains_no_broker_order_fields(tmp_path):
    store = ShadowForwardStore(tmp_path / "shadow.db")
    columns = {
        row[1] for row in store._connect().execute("PRAGMA table_info(shadow_signals)")
    }
    assert "broker_order_id" not in columns
    assert store.db_path.name == "shadow.db"


def test_daily_bar_dates_are_supported(tmp_path):
    store = ShadowForwardStore(tmp_path / "shadow.db")
    bars = {
        "bar_dates": [date(2026, 1, 2)],
        "opens": [100], "highs": [101], "lows": [99], "closes": [100],
    }
    normalised = store._normalise_bars(bars)
    assert len(normalised) == 1
    assert normalised[0]["ts"] > 0


def test_promotion_evidence_is_scoped_to_side_strategy_and_regime(tmp_path):
    store = ShadowForwardStore(tmp_path / "shadow.db")
    start = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    store.record_candidate(_candidate(start, pattern="same", side="buy"))
    store.record_candidate(_candidate(
        start + timedelta(minutes=30), pattern="same", side="sell"))
    conn = store._connect()
    conn.execute(
        """UPDATE shadow_signals SET status='closed', operationally_eligible=1,
           entry_bar_ts=?, net_return=0.01, outcome='win'""",
        (start.timestamp(),),
    )
    conn.commit()

    buy = store.evidence(
        "same", minimum_trades=1, minimum_days=1, side="buy",
        strategy="trend_following", regime="trending")
    sell = store.evidence(
        "same", minimum_trades=1, minimum_days=1, side="sell",
        strategy="trend_following", regime="trending")

    assert buy["completed"] == 1
    assert sell["completed"] == 1
    assert buy["side"] == "buy"


def test_all_winning_sample_can_clear_profit_factor_gate(tmp_path):
    store = ShadowForwardStore(tmp_path / "shadow.db")
    start = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
    store.record_candidate(_candidate(start, pattern="wins", side="buy"))
    store.record_candidate(_candidate(
        start + timedelta(days=1), pattern="wins", side="buy"))
    conn = store._connect()
    rows = conn.execute("SELECT id FROM shadow_signals ORDER BY id").fetchall()
    for index, row in enumerate(rows):
        conn.execute(
            """UPDATE shadow_signals SET status='closed',
               operationally_eligible=1, entry_bar_ts=?, net_return=0.01,
               outcome='win' WHERE id=?""",
            ((start + timedelta(days=index)).timestamp(), row["id"]),
        )
    conn.commit()

    evidence = store.evidence(
        "wins", minimum_trades=2, minimum_days=2, side="buy",
        strategy="trend_following", regime="trending")

    assert evidence["profit_factor_infinite"] is True
    assert evidence["paper_exploration_eligible"] is True
