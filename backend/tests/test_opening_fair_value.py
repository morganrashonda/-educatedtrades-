from datetime import datetime, timedelta

from backend.research.opening_fair_value import (
    ET,
    MinuteBar,
    PathSession,
    _direction_map,
    path_features,
    rolling_fair_values,
)


def _snapshot(near: float) -> dict:
    return {
        "ref_price": "100.0",
        "cont_book_clr_price": str(near),
        "paired_qty": 100,
        "total_imbalance_qty": 10,
        "side": "B",
    }


def test_rolling_fair_value_never_uses_current_outcome():
    start = datetime(2024, 1, 1)
    rows = []
    snapshots = {}
    for index in range(42):
        day = (start + timedelta(days=index)).date().isoformat()
        rows.append(
            {
                "day": day,
                "qqq_prior_close": 100.0,
                "nq_prior_close": 10_000.0,
                "nq_entry": 10_000.0 + index,
                "nq_0928_close": 10_000.0 + index / 2,
            }
        )
        snapshots[day] = {"snapshot_2900": _snapshot(100.0 + index / 100)}

    original = rolling_fair_values(rows, snapshots, minimum=40)
    changed = [dict(row) for row in rows]
    changed[40]["nq_entry"] = 99_999.0
    rerun = rolling_fair_values(changed, snapshots, minimum=40)

    day_40 = rows[40]["day"]
    day_41 = rows[41]["day"]
    assert original[day_40] == rerun[day_40]
    assert original[day_41] != rerun[day_41]


def test_path_features_use_latest_directional_extreme():
    session = PathSession()
    marks = [(1, 59), (2, 0), (8, 29), (8, 30), (8, 31), (8, 59), (9, 0), (9, 28)]
    for hour, minute in marks:
        local = datetime(2026, 8, 18, hour, minute, tzinfo=ET)
        high = 102.0 if (hour, minute) in {(8, 30), (9, 28)} else 101.0
        bar = MinuteBar(local, 100.0, high, 99.0, 101.0)
        session.bars.append(bar)
        session.marks[(hour, minute)] = bar

    result = path_features(session, prior_close=100.0, total_gap_pct=1.0)
    assert result is not None
    assert result["minutes_since_directional_extreme"] == 1.0


def test_frozen_direction_rules_do_not_infer_unregistered_filters():
    row = {
        "base_direction": -1,
        "fair_value_direction": 1,
        "fair_value_residual": -0.002,
        "late_confirmation": -0.1,
        "normalized_gap": 1.5,
    }
    assert _direction_map(row, 0.001, 1.2) == {
        "base_gap_fade": -1,
        "fair_value_direction": 1,
        "fair_value_selective": 1,
        "late_rejection_fade": -1,
        "residual_and_rejection": 0,
        "high_normalized_gap_fade": -1,
    }
