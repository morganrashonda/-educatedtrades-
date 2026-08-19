"""Optional Databento live collector for the NQ opening-gap shadow store.

Run this with a separate Python 3.10+ environment containing ``databento``.
The Main 5 backend remains on its pinned Python 3.9 environment.  This module
collects market observations only; it has no broker or order capability.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from backend.research.opening_gap_shadow import (
    ET,
    DecisionObservation,
    OpeningGapShadowStore,
    QuoteObservation,
    _at,
)


PRICE_SCALE = 1_000_000_000
DATASET = "GLBX.MDP3"
SYMBOL = "NQ.v.0"
STYPE_IN = "continuous"


def _timestamp_ns(record) -> int:
    value = getattr(record, "ts_event", None)
    if value is None:
        header = getattr(record, "hd", None)
        value = getattr(header, "ts_event", None) if header is not None else None
    if value is None and isinstance(record, dict):
        value = (record.get("hd") or {}).get("ts_event") or record.get("ts_event")
    if value is None:
        raise ValueError("record has no event timestamp")
    return int(value)


def _instrument_id(record) -> int:
    value = getattr(record, "instrument_id", None)
    if value is None:
        header = getattr(record, "hd", None)
        value = getattr(header, "instrument_id", None) if header is not None else None
    if value is None and isinstance(record, dict):
        value = (record.get("hd") or {}).get("instrument_id") or record.get("instrument_id")
    if value is None:
        raise ValueError("record has no instrument_id")
    return int(value)


def _scaled_price(record, name: str) -> float:
    pretty = getattr(record, f"pretty_{name}", None)
    if pretty is not None:
        return float(pretty)
    value = getattr(record, name, None)
    if value is None and isinstance(record, dict):
        value = record.get(name)
    if value is None:
        raise ValueError(f"record has no {name}")
    return float(value) / PRICE_SCALE


def _bbo(record) -> tuple[float, float]:
    levels = getattr(record, "levels", None)
    if levels:
        level = levels[0]
        bid = getattr(level, "pretty_bid_px", None)
        ask = getattr(level, "pretty_ask_px", None)
        if bid is None:
            bid = float(getattr(level, "bid_px")) / PRICE_SCALE
        if ask is None:
            ask = float(getattr(level, "ask_px")) / PRICE_SCALE
        return float(bid), float(ask)
    if isinstance(record, dict):
        level = (record.get("levels") or [{}])[0]
        return float(level["bid_px"]) / PRICE_SCALE, float(level["ask_px"]) / PRICE_SCALE
    return _scaled_price(record, "bid_px_00"), _scaled_price(record, "ask_px_00")


def _source_hash(values: dict) -> str:
    text = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class CollectorResult:
    action: str
    recorded: bool
    session_date: str


class CollectorProcessLock:
    """Kernel-held singleton lock; a stale file alone never blocks startup."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise RuntimeError("another opening-gap shadow collector is already running") from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()

    def release(self) -> None:
        if self._fh is None:
            return
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


class DatabentoGapCollector:
    """Translate ordered Databento records into strict shadow-store stages."""

    def __init__(self, store: OpeningGapShadowStore, capture_mode: str = "live"):
        if capture_mode not in ("live", "historical_replay"):
            raise ValueError("capture_mode must be live or historical_replay")
        self.store = store
        self.capture_mode = capture_mode
        self._guard = Lock()
        self.errors: list[str] = []

    def handle_ohlcv(self, record, captured_at: datetime | None = None) -> CollectorResult | None:
        has_close = (
            hasattr(record, "close")
            or hasattr(record, "pretty_close")
            or (isinstance(record, dict) and "close" in record)
        )
        if not has_close:  # SymbolMappingMsg and other control records are not bars.
            return None
        captured_at = captured_at or datetime.now(timezone.utc)
        ts_ns = _timestamp_ns(record)
        ts = datetime.fromtimestamp(ts_ns / 1e9, timezone.utc)
        local = ts.astimezone(ET)
        instrument_id = _instrument_id(record)
        close = _scaled_price(record, "close")
        source_values = {
            "schema": "ohlcv-1m", "ts_event": ts_ns,
            "instrument_id": instrument_id, "close": close,
        }
        digest = _source_hash(source_values)
        if (local.hour, local.minute) == (15, 59):
            recorded = self.store.record_reference_close(
                str(local.date()), ts.isoformat(), close, instrument_id,
                captured_at.isoformat(), self.capture_mode,
                "databento-live:GLBX.MDP3:ohlcv-1m:NQ.v.0", digest,
            )
            return CollectorResult("reference_close", recorded, str(local.date()))
        if (local.hour, local.minute) != (9, 28):
            return None
        day = local.date()
        existing = self.store.session(str(day))
        if existing is not None:
            return CollectorResult("decision", False, str(day))
        reference = self.store.latest_reference_close(str(day))
        if reference is None:
            recorded = self.store.record_decision_refusal(
                str(day), self.capture_mode, captured_at.isoformat(),
                "no prior 15:59 reference close is available",
                "databento-live:GLBX.MDP3:ohlcv-1m:NQ.v.0",
            )
            return CollectorResult("decision_refusal", recorded, str(day))
        decision = DecisionObservation(
            str(day), self.capture_mode, captured_at.isoformat(),
            datetime.fromtimestamp(reference["bar_ts"], timezone.utc).isoformat(),
            float(reference["close_price"]), int(reference["instrument_id"]),
            ts.isoformat(), close, instrument_id,
            "databento-live:GLBX.MDP3:ohlcv-1m:NQ.v.0", digest,
        )
        recorded = self.store.record_decision(decision)
        return CollectorResult("decision", recorded, str(day))

    def handle_mbp1(self, record) -> list[CollectorResult]:
        ts_ns = _timestamp_ns(record)
        ts = datetime.fromtimestamp(ts_ns / 1e9, timezone.utc)
        local = ts.astimezone(ET)
        day = str(local.date())
        row = self.store.session(day)
        if row is None or row["current_instrument_id"] != _instrument_id(record):
            return []
        try:
            bid, ask = _bbo(record)
        except (KeyError, TypeError, ValueError, OverflowError):
            return []
        if not (0 < bid < ask):
            return []
        source_values = {
            "schema": "mbp-1", "ts_event": ts_ns,
            "instrument_id": _instrument_id(record), "bid": bid, "ask": ask,
        }
        observation = QuoteObservation(
            day, ts.isoformat(), bid, ask,
            "databento-live:GLBX.MDP3:mbp-1:NQ.v.0", _source_hash(source_values),
        )
        results: list[CollectorResult] = []
        with self._guard:
            row = self.store.session(day)
            if row["status"] == "SIGNAL_AWAITING_ENTRY" and _at(local.date(), 9, 30) <= local <= _at(local.date(), 9, 30, 5):
                results.append(CollectorResult("entry", self.store.record_entry(observation), day))
                row = self.store.session(day)
            if row["status"] in ("SIGNAL_OPEN", "COMPLETE"):
                if _at(local.date(), 9, 30, 5) <= local <= _at(local.date(), 9, 30, 10):
                    try:
                        recorded = self.store.record_delayed_entry(5, observation)
                        results.append(CollectorResult("delayed_entry_5", recorded, day))
                    except ValueError as exc:
                        if "conflicting" not in str(exc):
                            self.errors.append(str(exc))
                if _at(local.date(), 9, 30, 10) <= local <= _at(local.date(), 9, 30, 15):
                    try:
                        recorded = self.store.record_delayed_entry(10, observation)
                        results.append(CollectorResult("delayed_entry_10", recorded, day))
                    except ValueError as exc:
                        if "conflicting" not in str(exc):
                            self.errors.append(str(exc))
            row = self.store.session(day)
            if row["status"] == "SIGNAL_OPEN" and _at(local.date(), 9, 32) <= local <= _at(local.date(), 9, 32, 5):
                results.append(CollectorResult("exit", self.store.record_exit(observation), day))
        return results

    def check_deadlines(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(ET)
        day = str(local.date())
        with self._guard:
            row = self.store.session(day)
            if row is None:
                if _at(local.date(), 9, 29, 59) <= local < _at(local.date(), 9, 30):
                    try:
                        self.store.record_decision_refusal(
                            day, self.capture_mode, now.isoformat(),
                            "09:28 completed bar was not received before decision deadline",
                            "databento-live:deadline-monitor",
                        )
                    except ValueError as exc:
                        self.errors.append(str(exc))
                return
            if row["status"] == "SIGNAL_AWAITING_ENTRY" and local > _at(local.date(), 9, 30, 5):
                try:
                    self.store.mark_stage_failure(
                        day, "ENTRY", "no valid MBP-1 BBO within five seconds of 09:30",
                        "databento-live:deadline-monitor",
                    )
                except ValueError as exc:
                    self.errors.append(str(exc))
            row = self.store.session(day)
            if row["status"] == "SIGNAL_OPEN" and local > _at(local.date(), 9, 32, 5):
                try:
                    self.store.mark_stage_failure(
                        day, "EXIT", "no valid MBP-1 BBO within five seconds of 09:32",
                        "databento-live:deadline-monitor",
                    )
                except ValueError as exc:
                    self.errors.append(str(exc))


def _run_live(db_path: Path) -> None:
    if not os.environ.get("DATABENTO_API_KEY"):
        raise RuntimeError("DATABENTO_API_KEY is not set")
    try:
        import databento as db  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "install backend/research/requirements-opening-gap-live.txt in a separate Python 3.10+ environment"
        ) from exc

    start_local = datetime.now(timezone.utc).astimezone(ET)
    if start_local.weekday() >= 5:
        raise RuntimeError("live collector cannot start on a weekend")
    if not (_at(start_local.date(), 9, 15) <= start_local < _at(start_local.date(), 9, 29, 45)):
        raise RuntimeError("live collector must start between 09:15:00 and 09:29:44 ET")
    process_lock = CollectorProcessLock(
        db_path.with_name(db_path.name + ".collector.lock")
    )
    process_lock.acquire()
    store = OpeningGapShadowStore(db_path)
    collector = DatabentoGapCollector(store, "live")
    bars = db.Live()
    bars.subscribe(
        dataset=DATASET, schema="ohlcv-1m", symbols=SYMBOL, stype_in=STYPE_IN
    )
    bars.add_callback(lambda record: _safe_ohlcv(collector, record))
    bars.start()
    quotes = None
    quotes_started = False
    try:
        while True:
            now = datetime.now(timezone.utc)
            local = now.astimezone(ET)
            collector.check_deadlines(now)
            if (
                not quotes_started
                and _at(local.date(), 9, 29, 55) <= local < _at(local.date(), 9, 32, 6)
                and (store.session(str(local.date())) or {}).get("status")
                == "SIGNAL_AWAITING_ENTRY"
            ):
                quotes = db.Live()
                quotes.subscribe(
                    dataset=DATASET, schema="mbp-1", symbols=SYMBOL, stype_in=STYPE_IN
                )
                quotes.add_callback(lambda record: _safe_mbp1(collector, record))
                quotes.start()
                quotes_started = True
            if quotes is not None and local >= _at(local.date(), 9, 32, 6):
                quotes.stop()
                quotes = None
            if local >= _at(local.date(), 16, 1):
                break
            time.sleep(0.20)
    finally:
        if quotes is not None:
            quotes.stop()
        bars.stop()
        process_lock.release()
    if collector.errors:
        raise RuntimeError("collector errors: " + "; ".join(collector.errors))


def _safe_ohlcv(collector: DatabentoGapCollector, record) -> None:
    try:
        collector.handle_ohlcv(record)
    except Exception as exc:  # callback must preserve the live stream
        collector.errors.append(f"ohlcv: {exc}")


def _safe_mbp1(collector: DatabentoGapCollector, record) -> None:
    try:
        collector.handle_mbp1(record)
    except Exception as exc:  # callback must preserve the live stream
        collector.errors.append(f"mbp1: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--check", action="store_true",
        help="validate configuration without connecting to Databento",
    )
    args = parser.parse_args()
    store = OpeningGapShadowStore(args.db)
    if args.check:
        print(json.dumps({
            "status": "READY_NO_NETWORK",
            "dataset": DATASET,
            "symbol": SYMBOL,
            "schemas": ["ohlcv-1m", "mbp-1"],
            "db": str(store.db_path),
            "execution_capability": False,
            "api_key_present": bool(os.environ.get("DATABENTO_API_KEY")),
        }, indent=2, sort_keys=True))
        return
    _run_live(args.db)


if __name__ == "__main__":
    main()
