from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from backend.research.opening_gap_databento import CollectorProcessLock, DatabentoGapCollector
from backend.research.opening_gap_shadow import ET, OpeningGapShadowStore


SCALE = 1_000_000_000


def ns(value):
    return int(value.timestamp() * 1e9)


@dataclass
class Bar:
    ts_event: int
    pretty_close: float
    instrument_id: int


@dataclass
class Level:
    pretty_bid_px: float
    pretty_ask_px: float


@dataclass
class Book:
    ts_event: int
    instrument_id: int
    levels: list


@dataclass
class Mapping:
    ts_event: int
    instrument_id: int


def at(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=ET)


def test_collector_builds_live_decision_and_executable_slippage_path(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    collector = DatabentoGapCollector(store, "live")
    prior = at(2026, 8, 18, 15, 59)
    captured_close = at(2026, 8, 18, 16, 0, 1)
    collector.handle_ohlcv(Bar(ns(prior), 20_000, 7), captured_close)

    decision_bar = at(2026, 8, 19, 9, 28)
    captured_decision = at(2026, 8, 19, 9, 29, 1)
    result = collector.handle_ohlcv(Bar(ns(decision_bar), 20_300, 7), captured_decision)
    assert result.action == "decision"
    assert store.session("2026-08-19")["side"] == "sell"

    collector.handle_mbp1(Book(ns(at(2026, 8, 19, 9, 30, 0)), 7, [Level(20_299.75, 20_300.25)]))
    collector.handle_mbp1(Book(ns(at(2026, 8, 19, 9, 30, 5)), 7, [Level(20_298.75, 20_299.25)]))
    collector.handle_mbp1(Book(ns(at(2026, 8, 19, 9, 30, 10)), 7, [Level(20_297.75, 20_298.25)]))
    collector.handle_mbp1(Book(ns(at(2026, 8, 19, 9, 32, 0)), 7, [Level(20_289.75, 20_290.25)]))
    row = store.session("2026-08-19")
    assert row["status"] == "COMPLETE"
    assert row["entry_price"] == pytest.approx(20_299.75)
    assert row["exit_price"] == pytest.approx(20_290.25)
    assert row["gross_points"] == pytest.approx(9.5)
    summary = store.summary()
    assert summary["delayed_entry_diagnostics"]["5"]["n"] == 1
    assert summary["delayed_entry_diagnostics"]["10"]["n"] == 1


def test_wrong_instrument_quotes_are_ignored(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    collector = DatabentoGapCollector(store, "historical_replay")
    collector.handle_ohlcv(
        Bar(ns(at(2026, 8, 18, 15, 59)), 20_000, 7),
        datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    collector.handle_ohlcv(
        Bar(ns(at(2026, 8, 19, 9, 28)), 20_300, 7),
        datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    results = collector.handle_mbp1(
        Book(ns(at(2026, 8, 19, 9, 30)), 8, [Level(20_299.75, 20_300.25)])
    )
    assert results == []
    assert store.session("2026-08-19")["status"] == "SIGNAL_AWAITING_ENTRY"


def test_deadline_monitor_refuses_missing_entry(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    collector = DatabentoGapCollector(store, "historical_replay")
    collector.handle_ohlcv(
        Bar(ns(at(2026, 8, 18, 15, 59)), 20_000, 7),
        datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    collector.handle_ohlcv(
        Bar(ns(at(2026, 8, 19, 9, 28)), 20_300, 7),
        datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    collector.check_deadlines(at(2026, 8, 19, 9, 30, 6))
    assert store.session("2026-08-19")["status"] == "REFUSED_ENTRY"


def test_missing_reference_is_recorded_as_decision_refusal(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    collector = DatabentoGapCollector(store, "historical_replay")
    result = collector.handle_ohlcv(
        Bar(ns(at(2026, 8, 19, 9, 28)), 20_300, 7),
        datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert result.action == "decision_refusal"
    assert store.session("2026-08-19")["status"] == "REFUSED_DECISION"


def test_symbol_mapping_control_record_is_ignored_not_reported_as_bar_error(tmp_path):
    store = OpeningGapShadowStore(tmp_path / "shadow.db")
    collector = DatabentoGapCollector(store, "historical_replay")
    assert collector.handle_ohlcv(Mapping(ns(at(2026, 8, 19, 9, 28)), 7)) is None
    assert collector.errors == []


def test_duplicate_collector_process_is_refused_by_kernel_lock(tmp_path):
    path = tmp_path / "collector.lock"
    first = CollectorProcessLock(path)
    second = CollectorProcessLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
