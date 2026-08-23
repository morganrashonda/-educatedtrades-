"""Leakage, symmetry, and execution tests for the level-reaction observer."""

from __future__ import annotations

import inspect
import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.research import opening_level_reaction as observer


DAY = date(2026, 1, 2)
OPEN_NS = observer._ns(DAY, observer.OPEN)


def state(
    second: int,
    close: float,
    *,
    open_mid: float | None = None,
    high: float | None = None,
    low: float | None = None,
    buys: float = 10.0,
    sells: float = 2.0,
    ofi: float = 8.0,
    bid_add: float = 4.0,
    bid_remove: float = 2.0,
    ask_add: float = 2.0,
    ask_remove: float = 2.0,
) -> observer.SecondState:
    return observer.SecondState(
        bucket_ns=OPEN_NS + second * 1_000_000_000,
        instrument_id=77,
        event_count=10,
        trade_count=4,
        buy_volume=buys,
        sell_volume=sells,
        open_mid=close if open_mid is None else open_mid,
        high_mid=close if high is None else high,
        low_mid=close if low is None else low,
        close_mid=close,
        ofi=ofi,
        bid_queue_add=bid_add,
        bid_queue_remove=bid_remove,
        ask_queue_add=ask_add,
        ask_queue_remove=ask_remove,
        mean_depth=20.0,
        mean_queue_imbalance=0.1,
        mean_spread=0.25,
        mean_microprice_displacement=0.05,
    )


def quote(second: int, bid: float, ask: float) -> observer.Quote:
    return observer.Quote(
        ts_ns=OPEN_NS + second * 1_000_000_000,
        bid=bid,
        ask=ask,
    )


def upper_break_states(post_close: float = 100.50) -> list[observer.SecondState]:
    rows = [
        state(0, 99.75),
        state(1, 100.25, open_mid=99.75),
    ]
    rows.extend(state(second, post_close) for second in range(2, 70))
    return rows


def outcome_quotes() -> list[observer.Quote]:
    return [
        quote(second, 100.00 + second * 0.01, 100.25 + second * 0.01)
        for second in range(0, 370)
    ]


def observe(
    seconds: list[observer.SecondState],
    *,
    level: observer.KnownLevel | None = None,
    headlines: list[observer.Headline] | None = None,
    headline_status: str = "DATA_GATED",
) -> dict:
    events = observer.observe_level(
        day=DAY,
        level=level or observer.KnownLevel(100.0, ("test_high",), 0, (1,)),
        seconds=seconds,
        quotes=outcome_quotes(),
        headlines=headlines or [],
        headline_status=headline_status,
        context={"frozen": True},
    )
    assert len(events) == 1
    return events[0]


def test_level_clustering_respects_eligibility_clock() -> None:
    levels = observer._cluster_levels([
        observer.KnownLevel(100.0, ("prior_rth_high",), 0, (1,)),
        observer.KnownLevel(100.5, ("overnight_high",), 0, (1,)),
        observer.KnownLevel(100.25, ("opening_range_60s_high",), 60, (1,)),
    ])
    assert len(levels) == 2
    assert levels[0].eligible_seconds == 0
    assert levels[0].names == ("overnight_high", "prior_rth_high")
    assert levels[1].eligible_seconds == 60


def test_opening_range_is_unavailable_until_first_minute_completes() -> None:
    levels = observer._opening_range_level(
        [
            state(0, 100.0, high=101.0, low=99.0),
            state(59, 100.0, high=102.0, low=98.0),
            state(60, 100.0, high=500.0, low=1.0),
        ],
        OPEN_NS,
    )
    assert {item.eligible_seconds for item in levels} == {60}
    assert {item.price for item in levels} == {98.0, 102.0}


def test_prior_week_levels_use_only_completed_point_in_time_week(tmp_path) -> None:
    path = tmp_path / "bars.jsonl"
    rows = []
    daily = {
        date(2026, 1, 2): (100.0, 105.0, 95.0),
        date(2026, 1, 5): (100.0, 110.0, 90.0),
        date(2026, 1, 6): (105.0, 120.0, 80.0),
        date(2026, 1, 12): (110.0, 115.0, 100.0),
    }
    for day, (open_price, high, low) in daily.items():
        stamp = datetime.combine(day, observer.OPEN, observer.ET).timestamp()
        rows.append(json.dumps({
            "hd": {"ts_event": str(int(stamp * 1e9)), "instrument_id": 77},
            "open": str(int(open_price * observer.PRICE_SCALE)),
            "high": str(int(high * observer.PRICE_SCALE)),
            "low": str(int(low * observer.PRICE_SCALE)),
            "close": str(int(open_price * observer.PRICE_SCALE)),
        }))
    path.write_text("\n".join(rows) + "\n")
    maps, quality = observer.build_session_maps(path)
    weekly = {
        name: level.price
        for level in maps[date(2026, 1, 12)].levels
        for name in level.names
        if name.startswith("prior_week_rth")
    }
    assert weekly == {"prior_week_rth_low": 80.0, "prior_week_rth_high": 120.0}
    assert quality["excluded"].get("prior_week_rth_instrument_mismatch_or_missing", 0) < 3


def test_accepted_break_requires_price_trade_flow_and_ofi_agreement() -> None:
    decision = observe(upper_break_states())["decisions"]["5"]
    assert decision["features"]["classification"] == "accepted_break"
    assert decision["features"]["mechanism"] == "accepted_flow"
    assert decision["expected_side"] == 1


def test_failed_lower_break_is_directionally_symmetric() -> None:
    rows = [state(0, 100.25), state(1, 99.75, open_mid=100.25)]
    rows.extend(
        state(second, 100.25, buys=10, sells=2, ofi=8)
        for second in range(2, 70)
    )
    event = observe(
        rows,
        level=observer.KnownLevel(100.0, ("test_low",), 0, (-1,)),
    )
    decision = event["decisions"]["5"]
    assert decision["features"]["classification"] == "failed_break"
    assert decision["features"]["mechanism"] == "opposite_dominance"
    assert decision["expected_side"] == 1


def test_absorption_is_descriptive_and_does_not_force_a_trade() -> None:
    rows = upper_break_states(post_close=100.0)
    rows = [
        replace(row, bid_queue_add=0.0, ask_queue_add=100.0)
        if row.bucket_ns >= OPEN_NS + 2_000_000_000 else row
        for row in rows
    ]
    decision = observe(rows)["decisions"]["5"]
    assert decision["features"]["classification"] == "unresolved"
    assert decision["features"]["mechanism"] == "absorption_divergence"
    assert decision["expected_side"] == 0


def test_exact_decision_and_future_rows_cannot_poison_earlier_features() -> None:
    baseline = upper_break_states()
    first = observe(baseline)["decisions"]["5"]["features"]
    # Break is observed at second 2, making second 7 the exact five-second cutoff.
    poisoned = [
        replace(row, close_mid=50.0, buy_volume=0.0, sell_volume=999.0, ofi=-999.0)
        if row.bucket_ns >= OPEN_NS + 7_000_000_000 else row
        for row in baseline
    ]
    second = observe(poisoned)["decisions"]["5"]["features"]
    assert first == second
    assert second["classification"] == "accepted_break"


def test_incomplete_decision_evidence_fails_closed() -> None:
    event = observe(upper_break_states()[:6])
    decision = event["decisions"]["5"]
    assert decision["features"]["status"] == "MISSING_EVIDENCE_AT_DECISION"
    assert decision["features"]["classification"] == "unresolved"
    assert decision["expected_side"] == 0


def test_missing_start_of_decision_window_fails_closed() -> None:
    rows = upper_break_states()
    rows = [row for row in rows if row.bucket_ns != OPEN_NS + 2_000_000_000]
    decision = observe(rows)["decisions"]["5"]
    assert decision["features"]["status"] == "MISSING_EVIDENCE_AT_DECISION"
    assert decision["expected_side"] == 0


def test_signed_progress_per_effort_preserves_adverse_movement() -> None:
    rows = [state(2, 100.50, open_mid=100.50), state(3, 100.25)]
    result = observer._aggregate(rows, 100.0, 1)
    assert result["price_progress_points"] == pytest.approx(-0.25)
    assert result["progress_per_aggressive_contract"] < 0


def test_measurement_features_are_directional_and_cutoff_safe() -> None:
    rows = [state(2, 100.50, open_mid=100.25), state(3, 100.75)]
    upper = observer._aggregate(rows, 100.0, 1)
    lower = observer._aggregate(rows, 100.0, -1)

    assert upper["feature_cutoff_exclusive_ns"] == OPEN_NS + 4_000_000_000
    assert upper["total_aggressive_volume"] == 24.0
    assert upper["same_side_refill_proxy"] == pytest.approx(1.0)
    assert upper["opposing_side_refill_proxy"] == pytest.approx(1 / 6)
    assert lower["same_side_refill_proxy"] == pytest.approx(1 / 6)
    assert lower["opposing_side_refill_proxy"] == pytest.approx(1.0)
    assert upper["maximum_distance_from_level_points"] == pytest.approx(0.75)
    assert upper["minimum_distance_from_level_points"] == pytest.approx(0.5)


def test_outcomes_use_executable_sides_and_record_dwell() -> None:
    decision_ns = OPEN_NS + 10_000_000_000
    quotes = [
        quote(10, 99.75, 100.00),
        quote(20, 101.00, 105.00),
        quote(40, 102.00, 102.25),
    ]
    result = observer._outcomes(quotes, decision_ns, 1, 100.0)
    horizon = result["horizons"]["30"]
    assert horizon["entry_quote_ns"] >= decision_ns
    assert horizon["continuation"]["entry_price"] == 100.0
    assert horizon["continuation"]["exit_price"] == 102.0
    assert horizon["continuation"]["mfe_points"] == 2.0
    assert horizon["reversal"]["entry_price"] == 99.75
    assert horizon["reversal"]["exit_price"] == 102.25
    assert horizon["outside_observations"] == 2
    assert horizon["observed_path_seconds"] == 30.0
    assert horizon["outside_seconds"] == 20.0
    assert horizon["boundary_seconds"] == 10.0


def test_missing_or_late_entry_quote_is_not_fabricated() -> None:
    result = observer._outcomes(
        [quote(13, 100.0, 100.25)], OPEN_NS + 10_000_000_000, 1, 100.0
    )
    assert result == {"status": "MISSING_ENTRY_QUOTE", "horizons": {}}


def test_future_headline_is_excluded_at_decision() -> None:
    observed = OPEN_NS + 2_000_000_000
    decision = observed + 5_000_000_000

    def headline(event_id: str, offset: int) -> observer.Headline:
        return observer.Headline(
            event_id=event_id,
            published_at=datetime.fromtimestamp(
                (decision + offset * 1_000_000_000) / 1e9, timezone.utc
            ),
            scope="market",
            sentiment="unknown",
            significance="unknown",
            source="test",
            symbols=("NQ",),
        )

    event = observe(
        upper_break_states(),
        headlines=[headline("known", -1), headline("future", 1)],
        headline_status="PROVIDED",
    )
    events = event["decisions"]["5"]["headline_context"]["events"]
    assert [item["event_id"] for item in events] == ["known"]


def test_readonly_connection_rejects_writes(tmp_path) -> None:
    path = tmp_path / "evidence.sqlite"
    writable = sqlite3.connect(path)
    writable.execute("CREATE TABLE evidence (value INTEGER)")
    writable.commit()
    writable.close()
    readonly = observer._readonly_connection(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("INSERT INTO evidence VALUES (1)")
    finally:
        readonly.close()


def test_module_has_no_production_execution_or_learning_dependencies() -> None:
    source = inspect.getsource(observer)
    forbidden = (
        "import main",
        "import trading",
        "import execution_safety",
        "import patterns",
        "submit_order",
        "patterns.db",
    )
    assert not any(value in source for value in forbidden)
