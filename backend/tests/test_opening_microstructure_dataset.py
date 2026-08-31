from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.research.opening_databento_collector import connect
from backend.research.opening_microstructure_dataset import build_rows


ET = ZoneInfo("America/New_York")
SCALE = 1_000_000_000


def ns(day: date, value: time) -> int:
    return int(datetime.combine(day, value, ET).timestamp() * SCALE)


def test_features_stop_before_decision_and_marks_use_executable_bbo(tmp_path: Path) -> None:
    db = tmp_path / "evidence.sqlite"
    conn = connect(db)
    day = date(2026, 8, 10)
    text = day.isoformat()
    for schema in ("ohlcv-1s", "bbo-1s", "trades"):
        conn.execute(
            """
            INSERT INTO requests (
                request_key, schema_name, session_date, start_utc, end_utc,
                estimated_cost, status, updated_at
            ) VALUES (?, ?, ?, '', '', 0, 'complete', '')
            """,
            (f"{schema}:{text}", schema, text),
        )
    quotes = [
        (time(9, 28), 100.00, 100.25, 8, 2),
        (time(9, 29, 59), 100.25, 100.50, 7, 3),
        (time(9, 30), 100.50, 100.75, 5, 5),
        (time(9, 30, 4), 100.75, 101.00, 6, 4),
        (time(9, 30, 5), 101.00, 101.25, 4, 6),
        (time(9, 32, 5), 102.00, 102.25, 5, 5),
        (time(9, 35, 5), 103.00, 103.25, 5, 5),
    ]
    conn.executemany(
        "INSERT INTO bbo_1s VALUES (?, ?, 7, ?, ?, ?, ?)",
        [(text, ns(day, stamp), int(bid * SCALE), int(ask * SCALE), bid_sz, ask_sz)
         for stamp, bid, ask, bid_sz, ask_sz in quotes],
    )
    conn.executemany(
        "INSERT INTO trades_1s VALUES (?, ?, 7, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (text, ns(day, time(9, 29, 59)), 1, 2, 1, 0,
             int(100.25 * SCALE), int(100.25 * SCALE), int(100.25 * SCALE),
             int(100.25 * SCALE), int(100.25 * SCALE)),
            (text, ns(day, time(9, 30, 4)), 1, 10, 0, 0,
             int(100.75 * SCALE), int(100.75 * SCALE), int(100.75 * SCALE),
             int(100.75 * SCALE), int(100.75 * SCALE)),
            # This extreme future value is timestamped at the decision and
            # must not enter features whose cutoff is 09:30:05.
            (text, ns(day, time(9, 30, 5)), 1, 10_000, 0, 0,
             int(101 * SCALE), int(101 * SCALE), int(101 * SCALE),
             int(101 * SCALE), int(101 * SCALE)),
        ],
    )
    conn.commit()
    conn.close()

    rows, quality = build_rows(db, decisions=(5,), horizons=(120, 300))
    assert quality["rows"] == 2
    assert {row["observed_trades_buy_volume"] for row in rows} == {10.0}
    first = next(row for row in rows if row["horizon_seconds"] == 120)
    assert first["entry_ask"] == pytest.approx(101.25)
    assert first["exit_bid"] == pytest.approx(102.00)
    assert first["long_executable_points"] == pytest.approx(0.75)
    assert first["short_executable_points"] == pytest.approx(-1.25)
    assert first["feature_cutoff_ns"] <= first["entry_quote_ns"] < first["exit_quote_ns"]


def test_missing_late_mark_is_reported_not_imputed(tmp_path: Path) -> None:
    db = tmp_path / "evidence.sqlite"
    conn = connect(db)
    day = date(2026, 8, 10)
    text = day.isoformat()
    for schema in ("ohlcv-1s", "bbo-1s", "trades"):
        conn.execute(
            """
            INSERT INTO requests (
                request_key, schema_name, session_date, start_utc, end_utc,
                estimated_cost, status, updated_at
            ) VALUES (?, ?, ?, '', '', 0, 'complete', '')
            """,
            (f"{schema}:{text}", schema, text),
        )
    conn.executemany(
        "INSERT INTO bbo_1s VALUES (?, ?, 7, ?, ?, 1, 1)",
        [
            (text, ns(day, time(9, 28)), int(100 * SCALE), int(100.25 * SCALE)),
            (text, ns(day, time(9, 30)), int(101 * SCALE), int(101.25 * SCALE)),
            (text, ns(day, time(9, 32)), int(102 * SCALE), int(102.25 * SCALE)),
        ],
    )
    conn.execute(
        "INSERT INTO trades_1s VALUES (?, ?, 7, 1, 1, 0, 0, ?, ?, ?, ?, ?)",
        (text, ns(day, time(9, 29)), *(int(100 * SCALE) for _ in range(5))),
    )
    conn.commit()
    conn.close()

    rows, quality = build_rows(db, decisions=(0,), horizons=(120, 300))
    assert len(rows) == 1
    assert rows[0]["horizon_seconds"] == 120
    assert quality["excluded"]["missing_exit_quote"] == 1


def test_mbp_features_are_cut_off_before_decision(tmp_path: Path) -> None:
    db = tmp_path / "evidence.sqlite"
    conn = connect(db)
    day = date(2026, 8, 10)
    text = day.isoformat()
    for schema in ("ohlcv-1s", "bbo-1s", "trades", "mbp-1"):
        conn.execute(
            """
            INSERT INTO requests (
                request_key, schema_name, session_date, start_utc, end_utc,
                estimated_cost, status, updated_at
            ) VALUES (?, ?, ?, '', '', 0, 'complete', '')
            """,
            (f"{schema}:{text}", schema, text),
        )
    conn.executemany(
        "INSERT INTO bbo_1s VALUES (?, ?, 7, ?, ?, 1, 1)",
        [
            (text, ns(day, time(9, 28)), int(100 * SCALE), int(100.25 * SCALE)),
            (text, ns(day, time(9, 30)), int(100.5 * SCALE), int(100.75 * SCALE)),
            (text, ns(day, time(9, 30, 5)), int(101 * SCALE), int(101.25 * SCALE)),
            (text, ns(day, time(9, 32, 5)), int(102 * SCALE), int(102.25 * SCALE)),
        ],
    )
    conn.execute(
        "INSERT INTO trades_1s VALUES (?, ?, 7, 1, 1, 0, 0, ?, ?, ?, ?, ?)",
        (text, ns(day, time(9, 29)), *(int(100 * SCALE) for _ in range(5))),
    )
    base = (
        text, ns(day, time(9, 30, 4)), 7, 2, 1, 10.0, 0.0,
        100.0, 101.0, 100.0, 101.0, 5.0, 2.0, 1.0, 1.0, 2.0,
        1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 5.0, 0.2, 0.25, 0.05,
    )
    future = list(base)
    future[1] = ns(day, time(9, 30, 5))
    future[11] = 10_000.0
    conn.execute("INSERT INTO mbp1_1s VALUES (" + ",".join("?" for _ in base) + ")", base)
    conn.execute("INSERT INTO mbp1_1s VALUES (" + ",".join("?" for _ in future) + ")", future)
    conn.commit()
    conn.close()

    rows, quality = build_rows(db, decisions=(5,), horizons=(120,), require_mbp=True)
    assert quality["rows"] == 1
    assert rows[0]["observed_mbp_ofi"] == 5.0
