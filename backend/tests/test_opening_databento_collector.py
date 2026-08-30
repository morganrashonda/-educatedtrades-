from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from backend.research.opening_databento_collector import (
    CollectionRefusal,
    _bbo_rows,
    _mbp_rows,
    _ohlcv_rows,
    _reserve,
    _trade_rows,
    authorize_retry,
    cash_session_dates_from_csv,
    completed_session_dates,
    connect,
    estimate_cost,
    request_window,
    reserved_cost,
    year_balanced_order,
)


def hd(ts: int = 1_000_000_000, instrument: int = 7) -> dict:
    return {"ts_event": ts, "instrument_id": instrument}


def test_compact_transforms() -> None:
    day = date(2026, 8, 10)
    ohlcv = _ohlcv_rows([{
        "hd": hd(), "open": 10_000_000_000, "high": 11_000_000_000,
        "low": 9_000_000_000, "close": 10_500_000_000, "volume": 3,
    }], day)
    assert ohlcv[0][-1] == 3

    bbo = _bbo_rows([{
        "hd": hd(), "ts_recv": 2_000_000_000, "levels": [{
            "bid_px": 10_000_000_000, "ask_px": 10_250_000_000,
            "bid_sz": 4, "ask_sz": 2,
        }],
    }], day)
    assert bbo[0][1] == 2_000_000_000
    assert bbo[0][-2:] == (4.0, 2.0)

    trades = _trade_rows([
        {"hd": hd(), "price": 10_000_000_000, "size": 2, "side": "B"},
        {"hd": hd(1_500_000_000), "price": 11_000_000_000, "size": 1, "side": "A"},
    ], day)
    assert len(trades) == 1
    assert trades[0][3:7] == (2, 2.0, 1.0, 0.0)


def test_mbp_aggregation_keeps_flow_components_separate() -> None:
    day = date(2026, 8, 10)
    rows = [
        {
            "hd": hd(), "action": "A", "side": "B", "size": 3,
            "levels": [{"bid_px": 10_000_000_000, "ask_px": 11_000_000_000,
                        "bid_sz": 5, "ask_sz": 5}],
        },
        {
            "hd": hd(1_100_000_000), "action": "T", "side": "B", "size": 2,
            "levels": [{"bid_px": 10_000_000_000, "ask_px": 11_000_000_000,
                        "bid_sz": 5, "ask_sz": 3}],
        },
    ]
    out = _mbp_rows(rows, day)
    assert len(out) == 1
    assert out[0][4] == 1
    assert out[0][5] == 2.0
    assert out[0][16] == 3.0


def test_budget_reservation_is_hard_capped(tmp_path: Path) -> None:
    conn = connect(tmp_path / "collector.sqlite")
    window = request_window(date(2026, 8, 10))
    _reserve(conn, "bbo-1s", window, 0.60, 1.00)
    assert reserved_cost(conn) == pytest.approx(0.60)
    with pytest.raises(CollectionRefusal):
        _reserve(conn, "mbp-1", request_window(date(2026, 8, 11)), 0.41, 1.00)
    assert reserved_cost(conn) == pytest.approx(0.60)
    conn.close()


def test_authorized_retry_preserves_history_and_rebills(tmp_path: Path) -> None:
    conn = connect(tmp_path / "collector.sqlite")
    window = request_window(date(2026, 8, 10))
    key = _reserve(conn, "ohlcv-1s", window, 0.20, 1.00)
    conn.execute(
        "UPDATE requests SET status = 'failed', error = 'timeout' WHERE request_key = ?",
        (key,),
    )
    conn.commit()

    authorize_retry(conn, key)
    assert conn.execute(
        "SELECT status FROM requests WHERE request_key = ?", (key,)
    ).fetchone()[0] == "retry_authorized"

    _reserve(conn, "ohlcv-1s", window, 0.25, 1.00)
    row = conn.execute(
        """
        SELECT status, estimated_cost, attempt_count, attempt_history
        FROM requests WHERE request_key = ?
        """,
        (key,),
    ).fetchone()
    assert row[:3] == ("reserved", pytest.approx(0.45), 2)
    history = json.loads(row[3])
    assert history[0]["status"] == "failed"
    assert history[0]["error"] == "timeout"
    assert history[0]["estimated_cost"] == pytest.approx(0.20)
    assert reserved_cost(conn) == pytest.approx(0.45)
    conn.close()


def test_retry_rebill_still_obeys_total_cap(tmp_path: Path) -> None:
    conn = connect(tmp_path / "collector.sqlite")
    window = request_window(date(2026, 8, 10))
    key = _reserve(conn, "ohlcv-1s", window, 0.60, 1.00)
    conn.execute(
        "UPDATE requests SET status = 'failed' WHERE request_key = ?", (key,)
    )
    conn.commit()
    authorize_retry(conn, key)
    with pytest.raises(CollectionRefusal):
        _reserve(conn, "ohlcv-1s", window, 0.41, 1.00)
    row = conn.execute(
        "SELECT status, estimated_cost, attempt_count FROM requests WHERE request_key = ?",
        (key,),
    ).fetchone()
    assert row == ("retry_authorized", 0.60, 1)
    conn.close()


def test_year_balanced_order_is_deterministic_and_interleaved() -> None:
    days = [date(2024, 1, 2), date(2024, 1, 3), date(2025, 1, 2), date(2025, 1, 3)]
    first = year_balanced_order(days)
    second = year_balanced_order(reversed(days))
    assert first == second
    assert [value.year for value in first] == [2024, 2025, 2024, 2025]


def test_cash_calendar_requires_actual_0930_bar(tmp_path: Path) -> None:
    path = tmp_path / "qqq.csv"
    path.write_text(
        "timestamp,open\n"
        "2021-09-03T13:30:00Z,1\n"
        "2021-09-06T13:25:00Z,1\n"
        "2021-09-07T13:30:00Z,1\n"
    )
    days = cash_session_dates_from_csv(
        path, start=date(2021, 9, 1), end=date(2021, 9, 8)
    )
    assert days == [date(2021, 9, 3), date(2021, 9, 7)]


def test_completed_calendar_reuses_only_successful_ohlcv_dates(tmp_path: Path) -> None:
    conn = connect(tmp_path / "collector.sqlite")
    for day, status in (("2026-08-10", "complete"), ("2026-08-11", "failed")):
        conn.execute(
            """
            INSERT INTO requests (
                request_key, schema_name, session_date, start_utc, end_utc,
                estimated_cost, status, updated_at
            ) VALUES (?, 'ohlcv-1s', ?, '', '', 0, ?, '')
            """,
            (f"ohlcv-1s:{day}", day, status),
        )
    conn.execute(
        """
        INSERT INTO requests (
            request_key, schema_name, session_date, start_utc, end_utc,
            estimated_cost, status, updated_at
        ) VALUES ('bbo:2026-08-12', 'bbo-1s', '2026-08-12', '', '', 0, 'complete', '')
        """
    )
    conn.commit()
    assert completed_session_dates(
        conn, start=date(2026, 8, 1), end=date(2026, 8, 31)
    ) == [date(2026, 8, 10)]
    conn.close()


def test_cost_estimate_retries_transient_connection_failure(monkeypatch) -> None:
    class Response:
        def json(self):
            return 0.125

        def raise_for_status(self):
            return None

        def close(self):
            return None

    class Client:
        calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                import requests
                raise requests.ConnectionError("temporary")
            return Response()

    monkeypatch.setattr("backend.research.opening_databento_collector.time.sleep", lambda _: None)
    client = Client()
    assert estimate_cost(
        client,
        schema="mbp-1",
        window=request_window(date(2026, 8, 10)),
        api_key="test",
    ) == pytest.approx(0.125)
    assert client.calls == 2
