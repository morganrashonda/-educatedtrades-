from datetime import datetime, timedelta, timezone

from backend.research.opening_failed_auction import ET, Bucket, Level, _q, detect_level


def bucket(index, close, *, buy=10, sell=10, imbalance=0.0, ofi=0.0, spread=0.25):
    ts = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(seconds=5 * index)
    return Bucket(ts, close, close, buy, sell, imbalance, ofi, spread)


def baseline(rows):
    out = {}
    for row in rows:
        local = row.ts.astimezone(ET)
        seconds = local.hour * 3600 + local.minute * 60 + local.second
        out[(seconds, 1)] = {"effort": [10.0] * 20, "result": [0.1] * 20}
        out[(seconds, -1)] = {"effort": [10.0] * 20, "result": [0.1] * 20}
    return out


def test_quantile_is_deterministic():
    assert _q([0, 10], 0.25) == 2.5
    assert _q([4], 0.75) == 4
    assert _q([], 0.5) is None


def test_full_upper_failed_auction_sequence_is_detected_after_confirmation():
    rows = [bucket(i, 99.0) for i in range(50)]
    rows[1] = bucket(1, 100.25, buy=20, sell=1, imbalance=0.9, ofi=20)
    rows[2] = bucket(2, 100.50, buy=30, sell=1, imbalance=0.9, ofi=20)
    rows[3] = bucket(3, 99.75, buy=1, sell=20, imbalance=-0.9, ofi=-20)
    rows[4] = bucket(4, 100.00, buy=5, sell=5)
    rows[5] = bucket(5, 99.25, buy=1, sell=20, imbalance=-0.9, ofi=-20)
    events = detect_level(rows, Level(100.0, ("overnight_high",), 0), baseline(rows))
    assert len(events) == 1
    event = events[0]
    assert event["stage"] == "full_sequence"
    assert event["shift_reclaim_index"] == 3
    assert event["full_sequence_index"] == 5
    assert event["outcomes"]["full_sequence"]["120"]["entry_timestamp"] == rows[6].ts.isoformat()


def test_no_absorption_when_baseline_is_insufficient():
    rows = [bucket(i, 99.0) for i in range(10)]
    rows[1] = bucket(1, 100.25, buy=100, sell=1, imbalance=0.9, ofi=20)
    events = detect_level(rows, Level(100.0, ("prior_rth_high",), 0), {})
    assert len(events) == 1
    assert events[0]["stage"] == "bare_break"
    assert events[0]["absorption_index"] is None


def test_zero_progress_quantile_is_valid_not_missing():
    rows = [bucket(i, 99.0) for i in range(20)]
    rows[1] = bucket(1, 100.25, buy=100, sell=1, imbalance=0.9, ofi=20)
    rows[2] = bucket(2, 100.25, buy=100, sell=1, imbalance=0.9, ofi=20)
    pool = baseline(rows)
    for values in pool.values():
        values["result"] = [0.0] * 20
    events = detect_level(rows, Level(100.0, ("prior_rth_high",), 0), pool)
    assert events[0]["absorption_index"] is not None


def test_future_flow_cannot_move_entry_before_decision():
    rows = [bucket(i, 99.0) for i in range(50)]
    rows[1] = bucket(1, 100.25, buy=20, sell=1, imbalance=0.9, ofi=20)
    rows[2] = bucket(2, 100.50, buy=30, sell=1, imbalance=0.9, ofi=20)
    rows[3] = bucket(3, 99.75, buy=1, sell=20, imbalance=-0.9, ofi=-20)
    rows[4] = bucket(4, 100.00)
    rows[5] = bucket(5, 99.25)
    event = detect_level(rows, Level(100.0, ("overnight_high",), 0), baseline(rows))[0]
    decision = datetime.fromisoformat(rows[event["full_sequence_index"]].ts.isoformat())
    entry = datetime.fromisoformat(event["outcomes"]["full_sequence"]["120"]["entry_timestamp"])
    assert entry > decision


def test_full_lower_failed_auction_is_symmetric():
    rows = [bucket(i, 101.0) for i in range(50)]
    rows[1] = bucket(1, 99.75, buy=1, sell=20, imbalance=-0.9, ofi=-20)
    rows[2] = bucket(2, 99.50, buy=1, sell=30, imbalance=-0.9, ofi=-20)
    rows[3] = bucket(3, 100.25, buy=20, sell=1, imbalance=0.9, ofi=20)
    rows[4] = bucket(4, 100.00)
    rows[5] = bucket(5, 100.75, buy=20, sell=1, imbalance=0.9, ofi=20)
    events = detect_level(rows, Level(100.0, ("overnight_low",), 0), baseline(rows))
    assert len(events) == 1
    assert events[0]["stage"] == "full_sequence"
    assert events[0]["break_side"] == -1


def test_low_level_does_not_accept_upward_cross_as_a_breakout_attempt():
    rows = [bucket(0, 99.0)] + [bucket(i, 100.25) for i in range(1, 20)]
    rows[1] = bucket(1, 100.25, buy=100, sell=1, imbalance=0.9, ofi=20)
    events = detect_level(rows, Level(100.0, ("overnight_low",), 0), baseline(rows))
    assert events == []
