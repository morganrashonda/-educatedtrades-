"""Integrity tests for the accepted-break shadow-forward observer."""

from __future__ import annotations

import inspect
import json
import plistlib
import sqlite3
from dataclasses import replace
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from backend.research import opening_accepted_break_forward as forward
from backend.research.opening_level_reaction import Bar, Quote, SecondState


DAY = date(2026, 8, 20)
INSTRUMENT = 77


def at(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), forward.ET)


def bar(
    day: date,
    hour: int,
    minute: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> Bar:
    return Bar(at(day, hour, minute), open_price, high, low, close, INSTRUMENT)


def state(
    offset: int,
    close: float,
    *,
    buys: float = 10.0,
    sells: float = 2.0,
    ofi: float = 8.0,
) -> SecondState:
    stamp = forward._ns(DAY, time(9, 30)) + offset * 1_000_000_000
    return SecondState(
        bucket_ns=stamp,
        instrument_id=INSTRUMENT,
        event_count=10,
        trade_count=4,
        buy_volume=buys,
        sell_volume=sells,
        open_mid=close,
        high_mid=close,
        low_mid=close,
        close_mid=close,
        ofi=ofi,
        bid_queue_add=4.0,
        bid_queue_remove=2.0,
        ask_queue_add=2.0,
        ask_queue_remove=2.0,
        mean_depth=20.0,
        mean_queue_imbalance=0.1,
        mean_spread=0.25,
        mean_microprice_displacement=0.05,
    )


def source_bundle(kind: str = "accepted") -> forward.SourceBundle:
    previous = date(2026, 8, 19)
    bars = (
        bar(previous, 9, 30, open_price=99, high=100, low=98, close=99),
        bar(previous, 15, 59, open_price=99, high=99.5, low=98.5, close=99),
        bar(previous, 18, 0, open_price=99, high=100, low=98.5, close=99),
        bar(DAY, 9, 0, open_price=99, high=100, low=98.5, close=99.5),
        bar(DAY, 9, 30, open_price=99.5, high=100.5, low=99.5, close=100.25),
    )
    if kind == "none":
        seconds = [state(-1, 99.0)] + [state(value, 99.0) for value in range(0, 100)]
    else:
        seconds = [state(-1, 99.5), state(0, 100.25)]
        if kind == "accepted":
            seconds.extend(state(value, 100.5) for value in range(1, 100))
        elif kind == "failed":
            seconds.extend(
                state(value, 99.75, buys=2.0, sells=10.0, ofi=-8.0)
                for value in range(1, 100)
            )
        else:
            raise ValueError(kind)
    quotes = (
        (INSTRUMENT, Quote(forward._ns(DAY, time(9, 30, 31)), 100.50, 100.75)),
        (INSTRUMENT, Quote(forward._ns(DAY, time(9, 30, 45)), 101.00, 101.25)),
        (INSTRUMENT, Quote(forward._ns(DAY, time(9, 31, 1)), 101.50, 101.75)),
    )
    return forward.SourceBundle(
        bars=bars,
        seconds=tuple(seconds),
        quotes=quotes,
        provenance={"fixture": True},
    )


def payload(status: str, *, reason: str | None = None) -> dict:
    result = forward._base_payload(DAY, status)
    if reason:
        result["reason"] = reason
    return result


def test_forward_boundary_and_same_day_time_gate() -> None:
    with pytest.raises(forward.ForwardRefusal, match="predates"):
        forward.validate_collection_time(
            date(2026, 8, 19), datetime(2026, 8, 20, 22, tzinfo=timezone.utc)
        )
    with pytest.raises(forward.ForwardRefusal, match="16:20"):
        forward.validate_collection_time(
            DAY, datetime(2026, 8, 20, 19, tzinfo=timezone.utc)
        )
    forward.validate_collection_time(
        DAY, datetime(2026, 8, 20, 21, tzinfo=timezone.utc)
    )


def test_only_accepted_break_becomes_candidate() -> None:
    result = forward.evaluate_bundle(DAY, source_bundle("accepted"))
    candidates = [
        item for item in result["attempts"]
        if item["candidate_status"] == "ACCEPTED_CANDIDATE"
    ]
    assert len(candidates) == 1
    assert candidates[0]["decisions"]["30"]["expected_side"] == 1
    assert candidates[0]["primary_net_points"] == pytest.approx(0.25)
    assert result["execution_authorized"] is False
    assert result["overlap_rule_defined"] is False


def test_failed_break_is_retained_as_abstention() -> None:
    result = forward.evaluate_bundle(DAY, source_bundle("failed"))
    assert result["attempted_breaks"] >= 1
    assert result["accepted_candidates"] == 0
    assert {
        item["candidate_status"] for item in result["attempts"]
    } == {"ABSTAINED_NOT_ACCEPTED"}


def test_no_attempt_session_and_level_inventory_are_retained() -> None:
    result = forward.evaluate_bundle(DAY, source_bundle("none"))
    assert result["status"] == "COMPLETE"
    assert result["no_attempt_session"] is True
    assert result["attempts"] == []
    assert result["level_inventory"]
    assert all(item["attempts"] == 0 for item in result["level_inventory"])


def test_opening_60s_ofi_candidates_are_measurement_only() -> None:
    bundle = source_bundle("none")
    seconds = [state(-1, 99.0)] + [
        state(value, 100.0 if value < 60 else 100.25)
        for value in range(120)
    ]
    result = forward.evaluate_bundle(DAY, replace(bundle, seconds=tuple(seconds)))
    opening = result["opening_60s"]
    assert opening["status"] == "COMPLETE"
    assert opening["seconds_observed"] == 60
    assert opening["outcome_seconds_observed"] == 120
    assert opening["ofi_score"] == pytest.approx(0.8)
    assert opening["forward_mid_move_points"] == pytest.approx(0.25)
    assert opening["forward_direction"] == 1
    assert opening["candidates"]["ofi_direction_all"] == {
        "eligible": True,
        "side": 1,
    }
    threshold = opening["candidates"]["ofi_direction_abs_ge_0.005"]
    assert threshold["eligible"] is True
    assert threshold["side"] == 1
    assert result["accepted_candidates"] == 0


def test_opening_60s_missing_evidence_abstains() -> None:
    result = forward.evaluate_bundle(DAY, source_bundle("none"))
    opening = result["opening_60s"]
    assert opening["status"] == "MISSING_OPENING_60S_EVIDENCE"
    assert opening["candidates"]["ofi_direction_all"]["eligible"] is False
    assert result["accepted_candidates"] == 0


def test_missing_outcome_quote_never_becomes_candidate() -> None:
    bundle = source_bundle("accepted")
    bundle = replace(bundle, quotes=bundle.quotes[:1])
    result = forward.evaluate_bundle(DAY, bundle)
    accepted = [
        item for item in result["attempts"]
        if item["decisions"]["30"]["features"]["classification"] == "accepted_break"
    ]
    assert accepted
    assert accepted[0]["candidate_status"] == "ABSTAINED_MISSING_OUTCOME"
    assert accepted[0]["primary_net_points"] is None


def test_quote_from_wrong_instrument_is_not_used() -> None:
    bundle = source_bundle("accepted")
    wrong = tuple((999, item) for _, item in bundle.quotes)
    with pytest.raises(forward.ForwardRefusal, match="BBO evidence"):
        forward.evaluate_bundle(DAY, replace(bundle, quotes=wrong))


def test_refusal_can_retry_but_complete_session_is_immutable(tmp_path) -> None:
    store = forward.ForwardStore(tmp_path / "forward.sqlite")
    first = payload("REFUSED_DATABENTO_SOURCE", reason="outage")
    assert store.record(first, datetime.now(timezone.utc)) is True
    assert store.record(first, datetime.now(timezone.utc)) is False
    complete = forward.evaluate_bundle(DAY, source_bundle("accepted"))
    assert store.record(complete, datetime.now(timezone.utc)) is True
    assert len(store.events(DAY)) == 2
    summary = store.summary()
    assert summary["accepted_event_outcomes"] == 1
    assert summary["calendar_months"]["2026-08"]["complete_sessions"] == 1
    assert summary["signal_session_cluster_bootstrap_95"] == [0.25, 0.25]
    assert summary["execution_authorized"] is False
    assert store.record(complete, datetime.now(timezone.utc)) is False
    conflicting = json.loads(json.dumps(complete))
    conflicting["map_context"]["cash_open"] = 999.0
    with pytest.raises(forward.ForwardRefusal, match="immutable"):
        store.record(conflicting, datetime.now(timezone.utc))


def test_read_only_store_never_requires_write_access(tmp_path) -> None:
    path = tmp_path / "forward.sqlite"
    writer = forward.ForwardStore(path)
    complete = forward.evaluate_bundle(DAY, source_bundle("accepted"))
    assert writer.record(complete, datetime.now(timezone.utc)) is True
    writer.conn.close()

    reader = forward.ForwardStore(path, read_only=True)
    assert reader.summary()["complete_sessions"] == 1
    with pytest.raises(sqlite3.OperationalError):
        reader.conn.execute(
            "INSERT INTO accepted_break_forward_sessions VALUES "
            "('2099-01-01','x','COMPLETE',0,0,'{}','x','x','x',1)"
        )


def test_read_only_check_works_before_a_database_exists(tmp_path) -> None:
    reader = forward.ForwardStore(tmp_path / "not-created.sqlite", read_only=True)
    assert reader.summary()["complete_sessions"] == 0


def test_event_ledger_rejects_update_and_delete(tmp_path) -> None:
    store = forward.ForwardStore(tmp_path / "forward.sqlite")
    store.record(payload("REFUSED_DATABENTO_SOURCE", reason="x"), datetime.now(timezone.utc))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE accepted_break_forward_events SET event_type='x'")
    store.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM accepted_break_forward_events")
    store.conn.rollback()


class Response:
    def __init__(self, *, cost: float | None = None, lines: list[bytes] | None = None):
        self.cost = cost
        self.lines = lines or []

    def raise_for_status(self):
        return None

    def json(self):
        return self.cost

    def iter_lines(self):
        return iter(self.lines)

    def close(self):
        return None


def test_all_costs_are_preflighted_before_any_paid_fetch(tmp_path) -> None:
    class Client:
        def __init__(self):
            self.urls = []

        def get(self, url, **kwargs):
            self.urls.append(url)
            return Response(cost=0.40)

    client = Client()
    provider = forward.DatabentoSourceProvider("key", client=client)
    with pytest.raises(forward.ForwardRefusal, match="exceeds"):
        provider.fetch(DAY, tmp_path)
    assert len(client.urls) == 3
    assert all(url.endswith("metadata.get_cost") for url in client.urls)


def test_response_size_cap_fails_closed() -> None:
    class Client:
        def get(self, *args, **kwargs):
            return Response(lines=[b'{"value": "too large"}'])

    provider = forward.DatabentoSourceProvider(
        "key", client=Client(), max_response_bytes=5
    )
    with pytest.raises(forward.ForwardRefusal, match="256 MiB"):
        provider._fetch_transform(
            {"schema": "test", "start": "x", "end": "y"}, list
        )


def test_locked_bbo_row_is_discarded() -> None:
    stamp = int(at(DAY, 9, 30).timestamp() * 1e9)
    rows = [{
        "hd": {"ts_event": stamp, "instrument_id": INSTRUMENT},
        "levels": [{
            "bid_px": 100_000_000_000,
            "ask_px": 100_000_000_000,
            "bid_sz": 1,
            "ask_sz": 1,
        }],
    }]
    assert forward.DatabentoSourceProvider._quotes(iter(rows), DAY) == []


def test_free_disk_reserve_fails_before_cost_or_fetch(monkeypatch, tmp_path) -> None:
    class Usage:
        free = 1

    class Client:
        def get(self, *args, **kwargs):
            raise AssertionError("network must not be called")

    monkeypatch.setattr(forward.shutil, "disk_usage", lambda _: Usage())
    provider = forward.DatabentoSourceProvider("key", client=Client())
    with pytest.raises(forward.ForwardRefusal, match="free disk"):
        provider.fetch(DAY, tmp_path)


def test_contract_and_payload_hashes_are_deterministic() -> None:
    assert len(forward.CONTRACT_SHA256) == 64
    first = forward._canonical({"b": 2, "a": 1})
    second = forward._canonical({"a": 1, "b": 2})
    assert first == second


def test_calendar_closed_day_is_recorded_without_source_fetch(tmp_path) -> None:
    class Calendar:
        def is_cash_session(self, day):
            return False

    class Source:
        def fetch(self, day, data_dir):
            raise AssertionError("source must not be fetched")

    store = forward.ForwardStore(tmp_path / "forward.sqlite")
    result = forward.observe_session(
        DAY,
        store,
        Calendar(),
        Source(),
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        data_dir=tmp_path,
    )
    assert result["status"] == "NO_CASH_SESSION"
    assert store.session(DAY)["status"] == "NO_CASH_SESSION"


def test_source_refusal_retains_attempt_cost_metadata(tmp_path) -> None:
    class Calendar:
        def is_cash_session(self, day):
            return True

    class Source:
        def fetch(self, day, data_dir):
            raise OSError("network")

        def attempt_metadata(self):
            return {"estimated_cost_usd": 0.25, "request_count": 3}

    store = forward.ForwardStore(tmp_path / "forward.sqlite")
    result = forward.observe_session(
        DAY,
        store,
        Calendar(),
        Source(),
        now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        data_dir=tmp_path,
    )
    assert result["status"] == "REFUSED_DATABENTO_SOURCE"
    assert result["source_attempt"]["estimated_cost_usd"] == 0.25


def test_module_has_no_execution_or_learning_dependency() -> None:
    source = inspect.getsource(forward)
    forbidden = (
        "import main",
        "import trading",
        "import execution_safety",
        "import patterns",
        "submit_order",
        "get_account",
        "get_position",
        "patterns.db",
        "tier_3",
    )
    assert not any(value in source for value in forbidden)


def test_launch_schedule_is_post_close_and_uses_isolated_wrapper() -> None:
    root = Path(__file__).resolve().parents[2]
    wrapper = root / "scripts/run_opening_accepted_break_forward.sh"
    plist_path = root / "scripts/com.educatedtrades.opening-accepted-break-forward.plist"
    wrapper_text = wrapper.read_text()
    with plist_path.open("rb") as source:
        plist = plistlib.load(source)
    intervals = plist["StartCalendarInterval"]
    assert len(intervals) == 5
    assert {(item["Hour"], item["Minute"]) for item in intervals} == {(15, 35)}
    assert "opening_accepted_break_forward" in wrapper_text
    assert "opening_accepted_break_forward.db" in wrapper_text
    assert "main.py" not in wrapper_text
    assert "--live" not in wrapper_text
