import csv
import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from backend.research.opening_executable_bbo import (
    AuditRefusal,
    BudgetRefusal,
    ET,
    ExecutableSession,
    Quote,
    SignalSession,
    analyze,
    bracket_points,
    download,
    evaluate_session,
    load_signals,
    request_params,
)


def at(day, hour, minute, second):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ET)


def record(ts, bid, ask, instrument=7):
    return {
        "ts_recv": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "hd": {"instrument_id": instrument},
        "levels": [{"bid_px": str(bid), "ask_px": str(ask)}],
        "symbol": "NQ.v.0",
    }


def write_bbo(path, day, direction=1, instrument=7):
    start = at(day, 9, 29, 55)
    rows = []
    for offset in range(132):
        ts = start + timedelta(seconds=offset)
        step = max(0, offset - 6)
        midpoint = 20_000 + direction * step
        rows.append(record(ts, midpoint - 0.25, midpoint + 0.25, instrument))
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_signal_population_is_strictly_greater_and_sorted(tmp_path):
    path = tmp_path / "sessions.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["day", "nq_overnight_ret"])
        writer.writeheader()
        writer.writerows([
            {"day": "2026-01-03", "nq_overnight_ret": "-1.4"},
            {"day": "2026-01-01", "nq_overnight_ret": "1.278097837"},
            {"day": "2026-01-02", "nq_overnight_ret": "1.5"},
        ])
    signals = load_signals(path)
    assert [str(row.day) for row in signals] == ["2026-01-02", "2026-01-03"]
    assert [row.direction for row in signals] == [-1, 1]


def test_request_window_is_only_the_frozen_131_seconds_and_dst_safe():
    winter = request_params(date(2026, 1, 5))
    summer = request_params(date(2026, 7, 6))
    assert winter["start"] == "2026-01-05T14:29:55Z"
    assert winter["end"] == "2026-01-05T14:32:06Z"
    assert summer["start"] == "2026-07-06T13:29:55Z"
    assert summer["end"] == "2026-07-06T13:32:06Z"
    assert winter["schema"] == "bbo-1s"


def test_long_uses_ask_entry_bid_exit_and_exact_delayed_marks(tmp_path):
    day = date(2026, 1, 5)
    path = tmp_path / "quotes.jsonl"
    write_bbo(path, day, direction=1)
    result = evaluate_session(SignalSession(day, -1.5, 1), path)
    assert result.entry.ask == 20_000.25
    assert result.exit.bid == 20_119.75
    assert result.gross_points == pytest.approx(119.5)
    assert result.delayed_5_points == pytest.approx(114.5)
    assert result.delayed_10_points == pytest.approx(109.5)
    assert result.mae_points == pytest.approx(-0.5)
    assert result.mfe_points == pytest.approx(119.5)


def test_short_uses_bid_entry_ask_exit(tmp_path):
    day = date(2026, 1, 5)
    path = tmp_path / "quotes.jsonl"
    write_bbo(path, day, direction=-1)
    result = evaluate_session(SignalSession(day, 1.5, -1), path)
    assert result.entry.bid == 19_999.75
    assert result.exit.ask == 19_880.25
    assert result.gross_points == pytest.approx(119.5)


def test_missing_exact_mark_and_mismatched_instrument_refuse(tmp_path):
    day = date(2026, 1, 5)
    path = tmp_path / "quotes.jsonl"
    write_bbo(path, day)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows = [row for row in rows if not row["ts_recv"].endswith("14:30:06Z")]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(AuditRefusal, match="09:30:06"):
        evaluate_session(SignalSession(day, -1.5, 1), path)

    write_bbo(path, day)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row["ts_recv"].endswith("14:32:01Z"):
            row["hd"]["instrument_id"] = 8
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(AuditRefusal, match="different instrument"):
        evaluate_session(SignalSession(day, -1.5, 1), path)


def test_bracket_executes_at_observed_adverse_mark_not_trigger():
    day = date(2026, 1, 5)
    entry = Quote(at(day, 9, 30, 1), 7, 99.75, 100.25)
    adverse = Quote(at(day, 9, 30, 2), 7, 92.75, 93.25)
    exit_quote = Quote(at(day, 9, 32, 1), 7, 101.75, 102.25)
    session = ExecutableSession(
        SignalSession(day, -1.5, 1), 7, entry, entry, entry, exit_quote,
        [entry, adverse, exit_quote], 1.5, 1.5, 1.5, -7.5, 1.5, "abc",
    )
    assert bracket_points(session, stop=4, target=48) == pytest.approx(-7.5)


class FakeResponse:
    def __init__(self, payload=None, body=b"", status=200, headers=None):
        self.payload = payload
        self.body = body
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload

    def iter_content(self, _size):
        yield self.body

    def close(self):
        pass


class FakeSession:
    def __init__(self, cost, body=b""):
        self.cost = cost
        self.body = body
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        if url.endswith("metadata.get_cost"):
            return FakeResponse(self.cost)
        return FakeResponse(body=self.body, headers={"Content-Length": str(len(self.body))})


def test_download_refuses_expensive_session_before_data_request(tmp_path):
    client = FakeSession(0.003)
    signals = [SignalSession(date(2026, 1, 5), -1.5, 1)]
    with pytest.raises(BudgetRefusal, match="per-session ceiling"):
        download(signals, tmp_path, "not-a-real-key", session=client)
    assert len(client.urls) == 1
    assert client.urls[0].endswith("metadata.get_cost")
    assert list(tmp_path.iterdir()) == []


def test_download_is_atomic_validated_and_resumable(tmp_path):
    day = date(2026, 1, 5)
    source = tmp_path / "source.jsonl"
    write_bbo(source, day)
    body = source.read_bytes()
    raw = tmp_path / "raw"
    signal = SignalSession(day, -1.5, 1)
    first = download([signal], raw, "not-a-real-key", session=FakeSession(0.0016, body))
    assert first["downloaded_sessions"] == 1
    assert first["estimated_new_cost_usd"] == pytest.approx(0.0016)
    assert not list(raw.glob("*.part"))
    assert evaluate_session(signal, raw / f"nq_bbo_1s_{day}.jsonl").gross_points > 0

    second_client = FakeSession(999)
    second = download([signal], raw, "not-a-real-key", session=second_client)
    assert second["reused_sessions"] == 1
    assert second["estimated_new_cost_usd"] == 0
    assert second_client.urls == []


def test_analysis_remains_research_only_and_cannot_pass_small_sample(tmp_path):
    day = date(2026, 1, 5)
    write_bbo(tmp_path / f"nq_bbo_1s_{day}.jsonl", day)
    report = analyze([SignalSession(day, -1.5, 1)], tmp_path)
    assert report["research_only"] is True
    assert report["execution_authorized"] is False
    assert report["status"] == "EXECUTION_AUDIT_FAIL"
    assert report["gates"]["at_least_50"] is False
    assert report["primary_cost_contract"]["total_additional_points_nq"] == 1.25
