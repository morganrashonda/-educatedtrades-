"""Research-only shadow observer for a pre-open conditional state.

This is intentionally separate from the accepted-break observer. It measures
whether strong pre-open aggressive flow, aligned with pre-open price direction,
improves a two-minute executable markout. It cannot place orders, update
production state, enable learning, or promote a Tier 3 rule.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import random
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from statistics import mean

from backend.research.opening_accepted_break_forward import (
    DATASET,
    ET,
    MAX_RESPONSE_BYTES,
    MIN_FREE_AFTER_PROCESSING,
    SYMBOL,
    STYPE_IN,
    AlpacaCalendarProvider,
    DatabentoSourceProvider,
    ForwardRefusal,
    Quote,
    SecondState,
    _at,
    _canonical,
    _ns,
)


FREEZE_DATE = date(2026, 8, 23)
FIRST_ELIGIBLE_SESSION = date(2026, 8, 24)
EARLIEST_SAME_DAY_RUN = time(16, 20)
PREOPEN_START = (9, 0, 0)
OPEN = time(9, 30)
OUTCOME_END = time(9, 32)
MAX_COST_USD = 2.00
FLOW_ABS_THRESHOLD = 0.10
EXTRA_COST_STRESS_POINTS = 2.25
MAX_REQUESTS = 2
TERMINAL_STATUSES = {"COMPLETE", "NO_CASH_SESSION"}

CONTRACT = {
    "frozen": str(FREEZE_DATE),
    "first_eligible_session": str(FIRST_ELIGIBLE_SESSION),
    "dataset": DATASET,
    "symbol": SYMBOL,
    "stype_in": STYPE_IN,
    "measurement": "preopen_flow_alignment_v1",
    "window_et": ["09:00:00", "09:30:00"],
    "outcome_window_et": ["09:30:00", "09:32:00"],
    "candidate": "high_flow_aligned_continuation",
    "baseline": "preopen_flow_direction_all",
    "flow_abs_threshold": FLOW_ABS_THRESHOLD,
    "extra_cost_stress_points": EXTRA_COST_STRESS_POINTS,
    "max_requests": MAX_REQUESTS,
    "max_cost_usd": MAX_COST_USD,
    "max_response_bytes": MAX_RESPONSE_BYTES,
    "minimum_free_after_processing_bytes": MIN_FREE_AFTER_PROCESSING,
    "execution_authorized": False,
}
CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(CONTRACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def _session_cluster_interval(values: list[float]) -> list[float] | None:
    """Deterministic session bootstrap keyed to this observer's contract."""
    if not values:
        return None
    rng = random.Random(f"preopen-conditional:{CONTRACT_SHA256}")
    draws = [mean(rng.choices(values, k=len(values))) for _ in range(2000)]
    return [_quantile(draws, 0.025), _quantile(draws, 0.975)]


@dataclass(frozen=True)
class PreopenBundle:
    seconds: tuple[SecondState, ...]
    quotes: tuple[tuple[int, Quote], ...]
    provenance: dict


def _exact_seconds(rows: list[SecondState], start_ns: int, count: int) -> bool:
    expected = {start_ns + offset * 1_000_000_000 for offset in range(count)}
    return len(rows) == count and {row.bucket_ns for row in rows} == expected


def _sign(value: float | None) -> int:
    if value is None or value == 0:
        return 0
    return 1 if value > 0 else -1


def _quote_at(
    quotes: list[Quote], timestamp_ns: int, max_delay_seconds: int = 2,
) -> Quote | None:
    limit = timestamp_ns + max_delay_seconds * 1_000_000_000
    for quote in sorted(quotes, key=lambda item: item.ts_ns):
        if timestamp_ns <= quote.ts_ns <= limit:
            return quote
    return None


def _base_payload(day: date, status: str) -> dict:
    return {
        "session_date": str(day),
        "status": status,
        "contract_sha256": CONTRACT_SHA256,
        "research_only": True,
        "execution_authorized": False,
    }


def evaluate_bundle(day: date, bundle: PreopenBundle) -> dict:
    open_ns = _ns(day, OPEN)
    preopen_start_ns = _ns(day, time(*PREOPEN_START))
    outcome_end_ns = _ns(day, OUTCOME_END)
    instrument_counts: dict[int, int] = {}
    for row in bundle.seconds:
        if preopen_start_ns <= row.bucket_ns < open_ns:
            instrument_counts[row.instrument_id] = (
                instrument_counts.get(row.instrument_id, 0) + 1
            )
    if not instrument_counts:
        raise ForwardRefusal("pre-open MBP evidence is empty")
    if len(instrument_counts) != 1:
        raise ForwardRefusal("pre-open MBP evidence contains multiple instruments")
    instrument_id = max(instrument_counts, key=instrument_counts.get)
    preopen = [
        row for row in bundle.seconds
        if row.instrument_id == instrument_id
        and preopen_start_ns <= row.bucket_ns < open_ns
    ]
    if not _exact_seconds(preopen, preopen_start_ns, 1800):
        raise ForwardRefusal("pre-open MBP evidence is not a complete 1800-second window")
    quotes = [
        quote for item_id, quote in bundle.quotes
        if item_id == instrument_id
    ]
    entry = _quote_at(quotes, open_ns)
    exit_quote = _quote_at(quotes, outcome_end_ns)
    if entry is None or exit_quote is None:
        raise ForwardRefusal("executable entry or exit quote is missing or late")

    buy = sum(float(row.buy_volume or 0.0) for row in preopen)
    sell = sum(float(row.sell_volume or 0.0) for row in preopen)
    flow_total = buy + sell
    flow_score = (buy - sell) / flow_total if flow_total else 0.0
    price_return = float(preopen[-1].close_mid - preopen[0].open_mid)
    flow_side = _sign(flow_score)
    price_side = _sign(price_return)
    aligned = flow_side != 0 and flow_side == price_side
    eligible = aligned and abs(flow_score) >= FLOW_ABS_THRESHOLD
    long_points = float(exit_quote.bid - entry.ask)
    short_points = float(entry.bid - exit_quote.ask)

    def outcome(side: int) -> dict:
        executable = long_points if side > 0 else short_points
        return {
            "side": side,
            "executable_points": executable,
            "stress_points": executable - EXTRA_COST_STRESS_POINTS,
        }

    return {
        **_base_payload(day, "COMPLETE"),
        "instrument_id": instrument_id,
        "source_provenance": bundle.provenance,
        "preopen": {
            "seconds_observed": len(preopen),
            "buy_volume": buy,
            "sell_volume": sell,
            "flow_score": flow_score,
            "price_return_points": price_return,
            "flow_side": flow_side,
            "price_side": price_side,
            "flow_price_aligned": aligned,
            "high_flow_aligned_eligible": eligible,
        },
        "quotes": {
            "entry_ts_ns": entry.ts_ns,
            "exit_ts_ns": exit_quote.ts_ns,
            "entry_bid": entry.bid,
            "entry_ask": entry.ask,
            "exit_bid": exit_quote.bid,
            "exit_ask": exit_quote.ask,
            "entry_delay_seconds": (entry.ts_ns - open_ns) / 1_000_000_000,
            "exit_delay_seconds": (
                exit_quote.ts_ns - outcome_end_ns
            ) / 1_000_000_000,
        },
        "outcomes": {
            "preopen_flow_direction_all": (
                outcome(flow_side) if flow_side else None
            ),
            "high_flow_aligned_continuation": (
                outcome(flow_side) if eligible else None
            ),
        },
    }


class PreopenSourceProvider:
    """Two-request, cost-preflighted MBP-1/BBO source for this contract."""

    def __init__(self, api_key: str, *, max_cost_usd: float = MAX_COST_USD):
        if not api_key:
            raise ForwardRefusal("DATABENTO_API_KEY is not set")
        self.api_key = api_key
        self.max_cost_usd = float(max_cost_usd)
        self.request_count = 0
        self.estimated_cost_usd = 0.0
        self.completed_sources: list[dict] = []
        self.provider = DatabentoSourceProvider(
            api_key, max_cost_usd=max_cost_usd,
        )

    def attempt_metadata(self) -> dict:
        return {
            "estimated_cost_usd": self.estimated_cost_usd,
            "request_count": self.request_count,
            "completed_sources": list(self.completed_sources),
        }

    def fetch(self, day: date, data_dir: Path) -> PreopenBundle:
        required = MAX_RESPONSE_BYTES + MIN_FREE_AFTER_PROCESSING
        if shutil.disk_usage(data_dir).free < required:
            raise ForwardRefusal("insufficient free disk for bounded processing")
        start = _at(day, PREOPEN_START).astimezone(timezone.utc)
        end = _at(day, (9, 32, 2)).astimezone(timezone.utc)
        params = [
            self.provider._params("mbp-1", start, end),
            self.provider._params("bbo-1s", start, end),
        ]
        estimates = [self.provider._estimate(item) for item in params]
        total = sum(estimates)
        if not math.isfinite(total) or total > self.max_cost_usd:
            raise ForwardRefusal(
                f"Databento estimate ${total:.6f} exceeds ${self.max_cost_usd:.2f} cap"
            )
        self.estimated_cost_usd = total
        self.request_count = len(params)
        self.completed_sources = []
        seconds, mbp = self.provider._fetch_transform(
            params[0], lambda rows: self.provider._seconds(rows, day)
        )
        self.completed_sources.append({**mbp, "estimated_cost_usd": estimates[0]})
        quotes, bbo = self.provider._fetch_transform(
            params[1], lambda rows: self.provider._quotes(rows, day)
        )
        self.completed_sources.append({**bbo, "estimated_cost_usd": estimates[1]})
        return PreopenBundle(
            tuple(seconds), tuple(quotes),
            {"estimated_cost_usd": total, "request_count": 2,
             "sources": list(self.completed_sources)},
        )


class PreopenStore:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path)
        if read_only and self.path.exists():
            self.conn = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro", uri=True, timeout=30,
            )
        else:
            if read_only:
                self.conn = sqlite3.connect(":memory:", timeout=30)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        if not read_only or not self.path.exists():
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS preopen_conditional_sessions (
                    session_date TEXT PRIMARY KEY,
                    contract_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    record_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preopen_conditional_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(session_date, payload_sha256)
                );
                CREATE TRIGGER IF NOT EXISTS preopen_events_no_update
                    BEFORE UPDATE ON preopen_conditional_events
                    BEGIN SELECT RAISE(ABORT, 'preopen event ledger is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS preopen_events_no_delete
                    BEFORE DELETE ON preopen_conditional_events
                    BEGIN SELECT RAISE(ABORT, 'preopen event ledger is append-only'); END;
                """
            )
            self.conn.commit()

    def session(self, day: date | str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM preopen_conditional_sessions WHERE session_date=?",
            (str(day),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def record(self, payload: dict, recorded_at: datetime) -> bool:
        if payload.get("contract_sha256") != CONTRACT_SHA256:
            raise ForwardRefusal("payload contract hash does not match frozen contract")
        text, digest = _canonical(payload)
        stamp = recorded_at.astimezone(timezone.utc).isoformat()
        day = str(payload["session_date"])
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT * FROM preopen_conditional_sessions WHERE session_date=?",
                (day,),
            ).fetchone()
            if existing is not None and existing["status"] in TERMINAL_STATUSES:
                if existing["payload_sha256"] == digest:
                    self.conn.rollback()
                    return False
                raise ForwardRefusal("conflicting result for immutable session")
            inserted = self.conn.execute(
                """INSERT OR IGNORE INTO preopen_conditional_events
                   (session_date,event_type,payload_json,payload_sha256,recorded_at)
                   VALUES (?,?,?,?,?)""",
                (day, payload["status"], text, digest, stamp),
            ).rowcount
            if not inserted:
                self.conn.rollback()
                return False
            if existing is None:
                self.conn.execute(
                    """INSERT INTO preopen_conditional_sessions
                       VALUES (?,?,?,?,?,?,1)""",
                    (day, CONTRACT_SHA256, payload["status"], text, digest, stamp),
                )
            else:
                self.conn.execute(
                    """UPDATE preopen_conditional_sessions
                       SET status=?,payload_json=?,payload_sha256=?,
                           recorded_at=?,record_count=record_count+1
                       WHERE session_date=?""",
                    (payload["status"], text, digest, stamp, day),
                )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT contract_sha256,status,payload_json FROM preopen_conditional_sessions ORDER BY session_date"
        ).fetchall()
        counts: dict[str, int] = {}
        legacy = 0
        candidate_values = {"preopen_flow_direction_all": [], "high_flow_aligned_continuation": []}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            legacy += row["contract_sha256"] != CONTRACT_SHA256
            if row["status"] != "COMPLETE":
                continue
            payload = json.loads(row["payload_json"])
            for name, outcome in payload.get("outcomes", {}).items():
                if outcome is not None:
                    candidate_values[name].append(float(outcome["stress_points"]))
        candidates = {}
        for name, values in candidate_values.items():
            candidates[name] = {
                "n": len(values),
                "wins": sum(value > 0 for value in values),
                "accuracy": (
                    sum(value > 0 for value in values) / len(values)
                    if values else None
                ),
                "mean_stress_points": mean(values) if values else None,
                "cluster_bootstrap_95": _session_cluster_interval(values),
            }
        complete = counts.get("COMPLETE", 0)
        return {
            "contract_sha256": CONTRACT_SHA256,
            "counts": counts,
            "complete_sessions": complete,
            "legacy_contract_sessions": legacy,
            "candidates": candidates,
            "initial_review_minimum_complete_sessions": 20,
            "first_performance_review_minimum_complete_sessions": 60,
            "stronger_review_minimum_complete_sessions": 120,
            "review_status": (
                "STRONGER_REVIEW_SAMPLE" if complete >= 120
                else "FIRST_PERFORMANCE_REVIEW" if complete >= 60
                else "DATA_QUALITY_REVIEW" if complete >= 20
                else "COLLECTING"
            ),
            "execution_authorized": False,
        }


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise RuntimeError("another pre-open observer is already running") from exc
        return self

    def __exit__(self, *_args):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def validate_collection_time(day: date, now: datetime) -> None:
    if now.tzinfo is None:
        raise ForwardRefusal("now must include a timezone")
    local = now.astimezone(ET)
    if day < FIRST_ELIGIBLE_SESSION:
        raise ForwardRefusal("session predates the frozen boundary")
    if day > local.date():
        raise ForwardRefusal("future sessions cannot be observed")
    if day == local.date() and local.time() < EARLIEST_SAME_DAY_RUN:
        raise ForwardRefusal("same-day collection cannot begin before 16:20 ET")


def observe_session(day, store, calendar_provider, source_provider, *, now, data_dir):
    validate_collection_time(day, now)
    existing = store.session(day)
    if existing and existing["status"] in TERMINAL_STATUSES:
        return existing["payload"]
    try:
        if not calendar_provider.is_cash_session(day):
            payload = _base_payload(day, "NO_CASH_SESSION")
            store.record(payload, now)
            return payload
        bundle = source_provider.fetch(day, data_dir)
        payload = evaluate_bundle(day, bundle)
    except Exception as exc:
        status = "REFUSED_SOURCE" if hasattr(source_provider, "attempt_metadata") else "REFUSED_EVALUATION"
        payload = _base_payload(day, status)
        payload["reason"] = f"{type(exc).__name__}: {exc}"
        if hasattr(source_provider, "attempt_metadata"):
            payload["source_attempt"] = source_provider.attempt_metadata()
    store.record(payload, now)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--session-date", type=date.fromisoformat)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    store = PreopenStore(args.db, read_only=args.check)
    if args.check:
        print(json.dumps({
            "status": "READY_NO_NETWORK",
            "contract": CONTRACT,
            "contract_sha256": CONTRACT_SHA256,
            "summary": store.summary(),
            "credentials_present": {
                "databento": bool(os.environ.get("DATABENTO_API_KEY")),
                "alpaca_key": bool(os.environ.get("APCA_API_KEY_ID")),
                "alpaca_secret": bool(os.environ.get("APCA_API_SECRET_KEY")),
            },
            "execution_capability": False,
        }, indent=2, sort_keys=True))
        return 0
    if args.session_date is None:
        parser.error("--session-date is required unless --check is used")
    now = datetime.now(timezone.utc)
    calendar = AlpacaCalendarProvider(
        os.environ.get("APCA_API_KEY_ID", ""),
        os.environ.get("APCA_API_SECRET_KEY", ""),
        base_url=os.environ.get("APCA_BASE_URL", "https://paper-api.alpaca.markets"),
    )
    source = PreopenSourceProvider(os.environ.get("DATABENTO_API_KEY", ""))
    with ProcessLock(args.db.with_suffix(args.db.suffix + ".lock")):
        result = observe_session(
            args.session_date, store, calendar, source, now=now, data_dir=args.db.parent,
        )
    print(json.dumps({
        "session_date": result["session_date"],
        "status": result["status"],
        "estimated_cost_usd": source.estimated_cost_usd,
        "databento_requests": source.request_count,
        "summary": store.summary(),
        "execution_capability": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
