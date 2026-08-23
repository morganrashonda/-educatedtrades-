"""Post-close, research-only observer for accepted NQ opening breaks.

This module retrieves market observations and writes an isolated evidence
ledger.  It has no production, broker, order, account, position, learning, or
Tier 3 capability.
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
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import requests

from backend.research.opening_databento_collector import (
    _bbo_rows,
    _mbp_rows,
    _px,
    _ts_ns,
)
from backend.research.opening_level_reaction import (
    Bar,
    Quote,
    SecondState,
    _cluster_levels,
    _ns,
    _opening_range_level,
    build_session_maps_from_bars,
    observe_level,
)


ET = ZoneInfo("America/New_York")
FREEZE_DATE = date(2026, 8, 19)
FIRST_ELIGIBLE_SESSION = date(2026, 8, 20)
EARLIEST_SAME_DAY_RUN = time(16, 20)
DATASET = "GLBX.MDP3"
SYMBOL = "NQ.v.0"
STYPE_IN = "continuous"
API_ROOT = "https://hist.databento.com/v0"
WINDOW_START = (9, 28, 0)
WINDOW_END = (9, 36, 0)
MAP_LOOKBACK_DAYS = 35
DECISION_SECONDS = 30
OUTCOME_SECONDS = 30
MAX_REQUESTS = 3
MAX_COST_USD = 1.00
MAX_RESPONSE_BYTES = 256 * 1024 * 1024
MIN_FREE_AFTER_PROCESSING = 1024 * 1024 * 1024
TERMINAL_STATUSES = {"COMPLETE", "NO_CASH_SESSION"}

CONTRACT = {
    "frozen": str(FREEZE_DATE),
    "first_eligible_session": str(FIRST_ELIGIBLE_SESSION),
    "dataset": DATASET,
    "symbol": SYMBOL,
    "stype_in": STYPE_IN,
    "algorithm": "opening_level_reaction_v1",
    "measurement_schema": "top_of_book_measurements_v3",
    "opening_measurement": "first_60s_ofi_v1",
    "opening_linking_features": "first_60s_linking_vector_v1",
    "opening_ofi_abs_threshold": 0.005,
    "opening_outcome_seconds": 120,
    "specification": "docs/OPENING_ACCEPTED_BREAK_SHADOW_FORWARD_SPEC_20260819.md",
    "evidence_window_et": ["09:28:00", "09:36:00"],
    "map_lookback_calendar_days": MAP_LOOKBACK_DAYS,
    "levels": [
        "prior_rth_high_low",
        "overnight_high_low",
        "premarket_0900_092959_high_low",
        "prior_completed_iso_week_rth_high_low",
        "opening_range_first_60_seconds_high_low",
    ],
    "level_cluster_ticks": 4,
    "attempt_tick_points": 0.25,
    "decision_seconds_after_observed_break": DECISION_SECONDS,
    "candidate": "accepted_break_continuation_only",
    "outcome_seconds_after_decision": OUTCOME_SECONDS,
    "max_quote_delay_seconds": 2,
    "extra_round_trip_stress_points": 0.50,
    "headline_context": "DATA_GATED",
    "max_requests": MAX_REQUESTS,
    "max_cost_usd": MAX_COST_USD,
    "max_response_bytes": MAX_RESPONSE_BYTES,
    "minimum_free_after_processing_bytes": MIN_FREE_AFTER_PROCESSING,
    "execution_authorized": False,
}
CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(CONTRACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
OPENING_OFI_ABS_THRESHOLD = float(CONTRACT["opening_ofi_abs_threshold"])


class ForwardRefusal(RuntimeError):
    """The requested observation violates a frozen integrity boundary."""


@dataclass(frozen=True)
class SourceBundle:
    bars: tuple[Bar, ...]
    seconds: tuple[SecondState, ...]
    quotes: tuple[tuple[int, Quote], ...]
    provenance: dict


def _canonical(payload: dict) -> tuple[str, str]:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return text, hashlib.sha256(text.encode()).hexdigest()


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return (
        ordered[lower] * (upper - index)
        + ordered[upper] * (index - lower)
    )


def _session_cluster_interval(values: list[float]) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(f"accepted-break-forward:{CONTRACT_SHA256}")
    draws = [mean(rng.choices(values, k=len(values))) for _ in range(2000)]
    return [_quantile(draws, 0.025), _quantile(draws, 0.975)]


def _at(day: date, hms: tuple[int, int, int]) -> datetime:
    return datetime.combine(day, time(*hms), ET)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_collection_time(day: date, now: datetime) -> None:
    if now.tzinfo is None:
        raise ForwardRefusal("now must include a timezone")
    local = now.astimezone(ET)
    if day < FIRST_ELIGIBLE_SESSION:
        raise ForwardRefusal(
            f"session {day} predates the immutable boundary {FIRST_ELIGIBLE_SESSION}"
        )
    if day > local.date():
        raise ForwardRefusal("future sessions cannot be observed")
    if day == local.date() and local.time() < EARLIEST_SAME_DAY_RUN:
        raise ForwardRefusal("same-day collection cannot begin before 16:20 ET")


class ForwardStore:
    """Append-only attempts plus a restart-safe materialized session result."""

    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only and self.path.exists():
            # Check mode must not create journals, acquire write locks, or
            # mutate the evidence database merely to report its status.
            self.conn = sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro", uri=True, timeout=30
            )
        else:
            if read_only:
                # A first-run check is still useful when no evidence ledger
                # exists yet; keep that empty result in memory only.
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
            CREATE TABLE IF NOT EXISTS accepted_break_forward_sessions (
                session_date TEXT PRIMARY KEY,
                contract_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                attempted_breaks INTEGER NOT NULL,
                accepted_candidates INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                first_recorded_at TEXT NOT NULL,
                last_recorded_at TEXT NOT NULL,
                record_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accepted_break_forward_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(session_date, payload_sha256)
            );
            CREATE TRIGGER IF NOT EXISTS accepted_break_forward_events_no_update
                BEFORE UPDATE ON accepted_break_forward_events
                BEGIN SELECT RAISE(ABORT, 'forward event ledger is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS accepted_break_forward_events_no_delete
                BEFORE DELETE ON accepted_break_forward_events
                BEGIN SELECT RAISE(ABORT, 'forward event ledger is append-only'); END;
            """
            )
            self.conn.commit()

    def session(self, day: date | str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM accepted_break_forward_sessions WHERE session_date=?",
            (str(day),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def events(self, day: date | str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM accepted_break_forward_events
               WHERE session_date=? ORDER BY id""",
            (str(day),),
        ).fetchall()
        return [dict(row) for row in rows]

    def record(self, payload: dict, recorded_at: datetime) -> bool:
        day = date.fromisoformat(str(payload["session_date"]))
        if day < FIRST_ELIGIBLE_SESSION:
            raise ForwardRefusal("store refuses pre-boundary evidence")
        if payload.get("contract_sha256") != CONTRACT_SHA256:
            raise ForwardRefusal("payload contract hash does not match frozen contract")
        status = str(payload["status"])
        text, digest = _canonical(payload)
        stamp = recorded_at.astimezone(timezone.utc).isoformat()
        attempted = len(payload.get("attempts", []))
        accepted = sum(
            item.get("candidate_status") == "ACCEPTED_CANDIDATE"
            for item in payload.get("attempts", [])
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                """SELECT * FROM accepted_break_forward_sessions
                   WHERE session_date=?""",
                (str(day),),
            ).fetchone()
            if existing is not None and existing["status"] in TERMINAL_STATUSES:
                if existing["payload_sha256"] == digest:
                    self.conn.rollback()
                    return False
                raise ForwardRefusal("conflicting result for immutable complete session")
            inserted = self.conn.execute(
                """INSERT OR IGNORE INTO accepted_break_forward_events
                   (session_date,event_type,payload_json,payload_sha256,recorded_at)
                   VALUES (?,?,?,?,?)""",
                (str(day), status, text, digest, stamp),
            ).rowcount
            if not inserted:
                self.conn.rollback()
                return False
            if existing is None:
                self.conn.execute(
                    """INSERT INTO accepted_break_forward_sessions
                       (session_date,contract_sha256,status,attempted_breaks,
                        accepted_candidates,payload_json,payload_sha256,
                        first_recorded_at,last_recorded_at,record_count)
                       VALUES (?,?,?,?,?,?,?,?,?,1)""",
                    (
                        str(day), CONTRACT_SHA256, status, attempted, accepted,
                        text, digest, stamp, stamp,
                    ),
                )
            else:
                self.conn.execute(
                    """UPDATE accepted_break_forward_sessions
                       SET status=?,attempted_breaks=?,accepted_candidates=?,
                           payload_json=?,payload_sha256=?,last_recorded_at=?,
                           record_count=record_count+1
                       WHERE session_date=?""",
                    (status, attempted, accepted, text, digest, stamp, str(day)),
                )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def summary(self) -> dict:
        rows = self.conn.execute(
            """SELECT status, contract_sha256, payload_json
               FROM accepted_break_forward_sessions
               ORDER BY session_date"""
        ).fetchall()
        counts: dict[str, int] = {}
        accepted_values = []
        session_means = []
        complete_sessions = 0
        no_attempt_sessions = 0
        attempts_without_candidate = 0
        legacy_contract_sessions = 0
        opening_complete_sessions = 0
        opening_missing_sessions = 0
        opening_candidates: dict[str, dict] = {}
        calendar: dict[str, dict] = {}
        by_level_family: dict[str, list[float]] = {}
        by_side: dict[str, list[float]] = {}
        for row in rows:
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
            if str(row["contract_sha256"]) != CONTRACT_SHA256:
                legacy_contract_sessions += 1
            if status != "COMPLETE":
                continue
            complete_sessions += 1
            payload = json.loads(row["payload_json"])
            opening = payload.get("opening_60s")
            if opening and opening.get("status") == "COMPLETE":
                opening_complete_sessions += 1
                move = opening.get("forward_mid_move_points")
                for name, candidate in opening.get("candidates", {}).items():
                    item = opening_candidates.setdefault(
                        name, {"signals": 0, "wins": 0, "signed_points": []}
                    )
                    side = candidate.get("side")
                    if not candidate.get("eligible") or side not in (-1, 1):
                        continue
                    if move is None:
                        continue
                    signed = float(side) * float(move)
                    item["signals"] += 1
                    item["wins"] += int(signed > 0)
                    item["signed_points"].append(signed)
            elif opening:
                opening_missing_sessions += 1
            attempts = payload.get("attempts", [])
            if not attempts:
                no_attempt_sessions += 1
            values = [
                float(item["primary_net_points"])
                for item in attempts
                if item.get("candidate_status") == "ACCEPTED_CANDIDATE"
                and item.get("primary_net_points") is not None
            ]
            if attempts and not values:
                attempts_without_candidate += 1
            accepted_values.extend(values)
            if values:
                session_means.append(mean(values))
            month = str(payload["session_date"])[:7]
            month_item = calendar.setdefault(month, {
                "complete_sessions": 0,
                "attempted_breaks": 0,
                "accepted_event_outcomes": 0,
                "primary_net_points": [],
            })
            month_item["complete_sessions"] += 1
            month_item["attempted_breaks"] += len(attempts)
            month_item["accepted_event_outcomes"] += len(values)
            month_item["primary_net_points"].extend(values)
            for item in attempts:
                if item.get("candidate_status") != "ACCEPTED_CANDIDATE":
                    continue
                value = float(item["primary_net_points"])
                family = "+".join(item.get("level_names", []))
                by_level_family.setdefault(family, []).append(value)
                by_side.setdefault(str(item.get("break_side")), []).append(value)

        def grouped(items: dict[str, list[float]]) -> dict:
            return {
                key: {
                    "n": len(values),
                    "mean_primary_net_points": mean(values),
                }
                for key, values in sorted(items.items())
            }

        calendar_summary = {}
        for month, item in sorted(calendar.items()):
            values = item.pop("primary_net_points")
            calendar_summary[month] = {
                **item,
                "mean_primary_net_points": mean(values) if values else None,
            }
        opening_summary = {
            "complete_sessions": opening_complete_sessions,
            "missing_sessions": opening_missing_sessions,
            "candidates": {},
        }
        for name, item in sorted(opening_candidates.items()):
            values = item["signed_points"]
            signals = item["signals"]
            opening_summary["candidates"][name] = {
                "signals": signals,
                "wins": item["wins"],
                "accuracy": item["wins"] / signals if signals else None,
                "mean_signed_points": mean(values) if values else None,
                "mean_signed_points_after_0_5_cost": (
                    mean(value - 0.5 for value in values) if values else None
                ),
                "mean_signed_points_after_1_0_cost": (
                    mean(value - 1.0 for value in values) if values else None
                ),
            }
        return {
            "contract_sha256": CONTRACT_SHA256,
            "first_eligible_session": str(FIRST_ELIGIBLE_SESSION),
            "counts": counts,
            "legacy_contract_sessions": legacy_contract_sessions,
            "complete_sessions": complete_sessions,
            "no_attempt_sessions": no_attempt_sessions,
            "attempt_sessions_without_accepted_candidate": attempts_without_candidate,
            "accepted_event_outcomes": len(accepted_values),
            "observation_weighted_mean_primary_net_points": (
                mean(accepted_values) if accepted_values else None
            ),
            "equal_signal_session_mean_primary_net_points": (
                mean(session_means) if session_means else None
            ),
            "signal_session_cluster_bootstrap_95": (
                _session_cluster_interval(session_means)
            ),
            "calendar_months": calendar_summary,
            "accepted_by_level_family": grouped(by_level_family),
            "accepted_by_break_side": grouped(by_side),
            "opening_60s": opening_summary,
            "initial_review_minimum_complete_sessions": 60,
            "stronger_review_minimum_complete_sessions": 120,
            "review_status": (
                "STRONGER_REVIEW_SAMPLE"
                if complete_sessions >= 120
                else "INITIAL_REVIEW_SAMPLE"
                if complete_sessions >= 60
                else "COLLECTING"
            ),
            "overlap_rule_defined": False,
            "execution_authorized": False,
        }


class AlpacaCalendarProvider:
    """Calendar-only client; it never requests an account, order, or position."""

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        base_url: str = "https://paper-api.alpaca.markets",
        client: requests.Session | None = None,
    ):
        if not key_id or not secret_key:
            raise ForwardRefusal("Alpaca calendar credentials are not set")
        self.headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        }
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v2"):
            self.base_url = self.base_url[:-3]
        self.client = client or requests.Session()

    def is_cash_session(self, day: date) -> bool:
        response = self.client.get(
            f"{self.base_url}/v2/calendar",
            headers=self.headers,
            params={"start": str(day), "end": str(day)},
            timeout=(10, 30),
        )
        try:
            response.raise_for_status()
            payload = response.json()
        finally:
            response.close()
        if not isinstance(payload, list):
            raise ForwardRefusal("Alpaca calendar response is not a list")
        return any(str(item.get("date")) == str(day) for item in payload)


class DatabentoSourceProvider:
    """Cost-preflighted retrieval of the three frozen source windows."""

    def __init__(
        self,
        api_key: str,
        *,
        client: requests.Session | None = None,
        max_cost_usd: float = MAX_COST_USD,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        if not api_key:
            raise ForwardRefusal("DATABENTO_API_KEY is not set")
        self.api_key = api_key
        self.client = client or requests.Session()
        self.max_cost_usd = float(max_cost_usd)
        self.max_response_bytes = int(max_response_bytes)
        self.estimated_cost_usd = 0.0
        self.request_count = 0
        self.completed_sources: list[dict] = []

    def attempt_metadata(self) -> dict:
        return {
            "estimated_cost_usd": self.estimated_cost_usd,
            "request_count": self.request_count,
            "completed_sources": list(self.completed_sources),
        }

    @staticmethod
    def _params(schema: str, start: datetime, end: datetime) -> dict[str, str]:
        return {
            "dataset": DATASET,
            "symbols": SYMBOL,
            "stype_in": STYPE_IN,
            "schema": schema,
            "start": _iso(start),
            "end": _iso(end),
            "encoding": "json",
            "pretty_px": "true",
            "pretty_ts": "true",
            "map_symbols": "false",
        }

    @staticmethod
    def _cost_value(payload) -> float:
        if isinstance(payload, (int, float)):
            value = float(payload)
        elif isinstance(payload, dict):
            value = float(payload.get("cost_usd", payload.get("cost")))
        else:
            raise ForwardRefusal("Databento cost response is not numeric")
        if not math.isfinite(value) or value < 0:
            raise ForwardRefusal("Databento returned an invalid cost estimate")
        return value

    def _estimate(self, params: dict[str, str]) -> float:
        cost_keys = ("dataset", "symbols", "stype_in", "schema", "start", "end")
        response = self.client.get(
            f"{API_ROOT}/metadata.get_cost",
            params={key: params[key] for key in cost_keys},
            auth=(self.api_key, ""),
            timeout=(10, 30),
        )
        try:
            response.raise_for_status()
            return self._cost_value(response.json())
        finally:
            response.close()

    def _fetch_transform(self, params: dict[str, str], transform) -> tuple[object, dict]:
        response = self.client.get(
            f"{API_ROOT}/timeseries.get_range",
            params=params,
            auth=(self.api_key, ""),
            timeout=(10, 180),
            stream=True,
        )
        digest = hashlib.sha256()
        raw_records = 0
        raw_bytes = 0

        def rows():
            nonlocal raw_records, raw_bytes
            for raw in response.iter_lines():
                if not raw.strip():
                    continue
                raw_bytes += len(raw) + 1
                if raw_bytes > self.max_response_bytes:
                    raise ForwardRefusal("Databento response exceeded the 256 MiB cap")
                digest.update(raw)
                digest.update(b"\n")
                raw_records += 1
                yield json.loads(raw)

        try:
            response.raise_for_status()
            derived = transform(rows())
        finally:
            response.close()
        if not derived:
            raise ForwardRefusal(f"{params['schema']} produced no usable rows")
        return derived, {
            "schema": params["schema"],
            "start_utc": params["start"],
            "end_utc": params["end"],
            "raw_records": raw_records,
            "derived_rows": len(derived),
            "source_sha256": digest.hexdigest(),
            "streamed_bytes": raw_bytes,
        }

    @staticmethod
    def _bars(rows) -> list[Bar]:
        result = []
        for row in rows:
            if "hd" not in row or "close" not in row:
                continue
            hd = row["hd"]
            ts = datetime.fromtimestamp(
                _ts_ns(hd["ts_event"]) / 1e9, timezone.utc
            )
            result.append(Bar(
                ts=ts,
                open=_px(row["open"]),
                high=_px(row["high"]),
                low=_px(row["low"]),
                close=_px(row["close"]),
                instrument_id=int(hd["instrument_id"]),
            ))
        return result

    @staticmethod
    def _seconds(rows, day: date) -> list[SecondState]:
        compact = _mbp_rows(rows, day)
        return [
            SecondState(
                bucket_ns=row[1], instrument_id=row[2], event_count=row[3],
                trade_count=row[4], buy_volume=row[5], sell_volume=row[6],
                open_mid=row[7], high_mid=row[8], low_mid=row[9],
                close_mid=row[10], ofi=row[11], bid_queue_add=row[12],
                bid_queue_remove=row[13], ask_queue_add=row[14],
                ask_queue_remove=row[15], mean_depth=row[22],
                mean_queue_imbalance=row[23], mean_spread=row[24],
                mean_microprice_displacement=row[25],
            )
            for row in compact
        ]

    @staticmethod
    def _quotes(rows, day: date) -> list[tuple[int, Quote]]:
        compact = _bbo_rows(rows, day)
        result = []
        for row in compact:
            bid, ask = row[3] / 1e9, row[4] / 1e9
            if not (
                math.isfinite(bid)
                and math.isfinite(ask)
                and bid > 0
                and ask > bid
            ):
                continue
            result.append((row[2], Quote(ts_ns=row[1], bid=bid, ask=ask)))
        return result

    def fetch(self, day: date, data_dir: Path) -> SourceBundle:
        required = self.max_response_bytes + MIN_FREE_AFTER_PROCESSING
        if shutil.disk_usage(data_dir).free < required:
            raise ForwardRefusal(
                "insufficient free disk for bounded processing plus 1 GiB reserve"
            )
        start = _at(day, WINDOW_START).astimezone(timezone.utc)
        end = _at(day, WINDOW_END).astimezone(timezone.utc)
        map_start = _at(day - timedelta(days=MAP_LOOKBACK_DAYS), (0, 0, 0)).astimezone(
            timezone.utc
        )
        specs = [
            ("ohlcv-1m", map_start, end),
            ("mbp-1", start, end),
            ("bbo-1s", start, end),
        ]
        if len(specs) > MAX_REQUESTS:
            raise ForwardRefusal("frozen request-count ceiling exceeded")
        params = [self._params(schema, first, last) for schema, first, last in specs]
        estimates = [self._estimate(item) for item in params]
        total = sum(estimates)
        if not math.isfinite(total) or total > self.max_cost_usd:
            raise ForwardRefusal(
                f"Databento estimate ${total:.6f} exceeds ${self.max_cost_usd:.2f} cap"
            )
        self.estimated_cost_usd = total
        self.request_count = len(params)
        self.completed_sources = []
        bars, bar_provenance = self._fetch_transform(params[0], self._bars)
        self.completed_sources.append(
            {**bar_provenance, "estimated_cost_usd": estimates[0]}
        )
        seconds, mbp_provenance = self._fetch_transform(
            params[1], lambda rows: self._seconds(rows, day)
        )
        self.completed_sources.append(
            {**mbp_provenance, "estimated_cost_usd": estimates[1]}
        )
        quotes, bbo_provenance = self._fetch_transform(
            params[2], lambda rows: self._quotes(rows, day)
        )
        self.completed_sources.append(
            {**bbo_provenance, "estimated_cost_usd": estimates[2]}
        )
        provenance = {
            "estimated_cost_usd": total,
            "request_count": self.request_count,
            "sources": list(self.completed_sources),
        }
        return SourceBundle(tuple(bars), tuple(seconds), tuple(quotes), provenance)


def _base_payload(day: date, status: str) -> dict:
    return {
        "session_date": str(day),
        "status": status,
        "contract_sha256": CONTRACT_SHA256,
        "research_only": True,
        "execution_authorized": False,
    }


def _opening_60s_measurement(day: date, seconds: list[SecondState]) -> dict:
    """Measure frozen first-60-second OFI candidates without execution."""
    open_ns = _ns(day, time(9, 30))
    decision_end_ns = open_ns + 60 * 1_000_000_000
    outcome_end_ns = open_ns + 120 * 1_000_000_000
    opening = [
        item for item in seconds
        if open_ns <= item.bucket_ns < decision_end_ns
    ]
    outcome_window = [
        item for item in seconds
        if open_ns <= item.bucket_ns < outcome_end_ns
    ]
    expected_opening = {
        open_ns + offset * 1_000_000_000 for offset in range(60)
    }
    expected_outcome = {
        open_ns + offset * 1_000_000_000 for offset in range(120)
    }
    base = {
        "window_et": ["09:30:00", "09:31:00"],
        "outcome_window_et": ["09:30:00", "09:32:00"],
        "seconds_observed": len(opening),
        "outcome_seconds_observed": len(outcome_window),
        "ofi_score": None,
        "forward_mid_move_points": None,
        "forward_direction": None,
        "candidates": {
            "ofi_direction_all": {"eligible": False, "side": None},
            "ofi_direction_abs_ge_0.005": {
                "eligible": False,
                "side": None,
                "threshold": OPENING_OFI_ABS_THRESHOLD,
            },
        },
    }
    if (
        len(opening) != 60
        or {item.bucket_ns for item in opening} != expected_opening
        or len(outcome_window) != 120
        or {item.bucket_ns for item in outcome_window} != expected_outcome
    ):
        return {"status": "MISSING_OPENING_60S_EVIDENCE", **base}

    raw_ofi = sum(float(item.ofi or 0.0) for item in opening)
    activity = sum(
        float(value or 0.0)
        for item in opening
        for value in (
            item.bid_queue_add,
            item.bid_queue_remove,
            item.ask_queue_add,
            item.ask_queue_remove,
        )
    )
    ofi_score = raw_ofi / activity if activity > 0 else 0.0
    trade_volume = sum(
        float(item.buy_volume or 0.0) + float(item.sell_volume or 0.0)
        for item in opening
    )
    flow_score = (
        sum(float(item.buy_volume or 0.0) - float(item.sell_volume or 0.0)
            for item in opening) / trade_volume
        if trade_volume > 0 else 0.0
    )
    ofi_side = 1 if ofi_score > 0 else -1 if ofi_score < 0 else None
    ofi_persistence = (
        sum(
            1
            for item in opening
            if ofi_side is not None and float(item.ofi or 0.0) * ofi_side > 0
        )
        / len(opening)
        if ofi_side is not None else 0.0
    )
    queue_values = [
        float(item.mean_queue_imbalance)
        for item in opening
        if item.mean_queue_imbalance is not None
    ]
    micro_values = [
        float(item.mean_microprice_displacement)
        for item in opening
        if item.mean_microprice_displacement is not None
    ]
    spread_values = [
        float(item.mean_spread) for item in opening if item.mean_spread is not None
    ]
    depth_values = [
        float(item.mean_depth) for item in opening if item.mean_depth is not None
    ]
    queue_mean = sum(queue_values) / len(queue_values) if queue_values else None
    micro_mean = sum(micro_values) / len(micro_values) if micro_values else None
    flow_side = 1 if flow_score > 0 else -1 if flow_score < 0 else None
    queue_side = 1 if queue_mean is not None and queue_mean > 0 else (
        -1 if queue_mean is not None and queue_mean < 0 else None
    )
    micro_side = 1 if micro_mean is not None and micro_mean > 0 else (
        -1 if micro_mean is not None and micro_mean < 0 else None
    )
    agreement = {
        "ofi_flow": ofi_side is not None and ofi_side == flow_side,
        "ofi_queue": ofi_side is not None and ofi_side == queue_side,
        "ofi_microprice": ofi_side is not None and ofi_side == micro_side,
    }
    move = float(outcome_window[-1].close_mid - outcome_window[0].open_mid)
    direction = 1 if move > 0 else -1 if move < 0 else 0
    threshold_side = (
        ofi_side if abs(ofi_score) >= OPENING_OFI_ABS_THRESHOLD else None
    )
    return {
        "status": "COMPLETE",
        **base,
        "raw_ofi": raw_ofi,
        "activity_denominator": activity,
        "ofi_score": ofi_score,
        "linking_features": {
            "flow_score": flow_score,
            "ofi_persistence": ofi_persistence,
            "queue_imbalance_mean": queue_mean,
            "microprice_displacement_mean": micro_mean,
            "mean_spread": (
                sum(spread_values) / len(spread_values) if spread_values else None
            ),
            "mean_depth": (
                sum(depth_values) / len(depth_values) if depth_values else None
            ),
            "opening_return_points": (
                float(opening[-1].close_mid - opening[0].open_mid)
            ),
            "opening_range_points": float(
                max(item.high_mid for item in opening)
                - min(item.low_mid for item in opening)
            ),
            "agreement": agreement,
            "three_way_flow_queue_micro_agree": all(agreement.values()),
        },
        "forward_mid_move_points": move,
        "forward_direction": direction,
        "candidates": {
            "ofi_direction_all": {
                "eligible": ofi_side is not None,
                "side": ofi_side,
            },
            "ofi_direction_abs_ge_0.005": {
                "eligible": threshold_side is not None,
                "side": threshold_side,
                "threshold": OPENING_OFI_ABS_THRESHOLD,
            },
        },
    }


def evaluate_bundle(day: date, bundle: SourceBundle) -> dict:
    maps, map_quality = build_session_maps_from_bars(bundle.bars)
    session_map = maps.get(day)
    if session_map is None:
        raise ForwardRefusal("point-in-time level map is unavailable")
    seconds = [
        item for item in bundle.seconds
        if item.instrument_id == session_map.instrument_id
    ]
    quotes = [
        quote for instrument_id, quote in bundle.quotes
        if instrument_id == session_map.instrument_id
        and _ns(day, time(9, 28)) <= quote.ts_ns < _ns(day, time(9, 36))
    ]
    if not seconds:
        raise ForwardRefusal("MBP evidence does not match the cash-open instrument")
    if not quotes:
        raise ForwardRefusal("BBO evidence is unavailable")
    opening_60s = _opening_60s_measurement(day, seconds)
    open_ns = _ns(day, time(9, 30))
    levels = _cluster_levels([
        *session_map.levels,
        *_opening_range_level(seconds, open_ns),
    ])
    attempts = []
    level_inventory = []
    for level in levels:
        level_events = observe_level(
            day=day,
            level=level,
            seconds=seconds,
            quotes=quotes,
            headlines=[],
            headline_status="DATA_GATED",
            context=session_map.context,
            decision_seconds=(DECISION_SECONDS,),
            horizon_seconds=(OUTCOME_SECONDS,),
        )
        level_inventory.append({
            "price": level.price,
            "names": list(level.names),
            "eligible_seconds": level.eligible_seconds,
            "allowed_sides": list(level.allowed_sides),
            "attempts": len(level_events),
        })
        for event in level_events:
            decision = event["decisions"][str(DECISION_SECONDS)]
            classification = decision["features"].get("classification", "unresolved")
            outcome = decision["outcomes"]["horizons"].get(str(OUTCOME_SECONDS))
            if classification != "accepted_break":
                candidate_status = "ABSTAINED_NOT_ACCEPTED"
                primary = None
            elif outcome is None:
                candidate_status = "ABSTAINED_MISSING_OUTCOME"
                primary = None
            else:
                candidate_status = "ACCEPTED_CANDIDATE"
                primary = outcome["continuation"][
                    "one_tick_each_side_stress_points"
                ]
            attempts.append({
                **event,
                "candidate_status": candidate_status,
                "primary_net_points": primary,
            })
    payload = _base_payload(day, "COMPLETE")
    payload.update({
        "source_provenance": bundle.provenance,
        "map_quality": map_quality,
        "map_context": session_map.context,
        "instrument_id": session_map.instrument_id,
        "level_inventory": level_inventory,
        "attempts": attempts,
        "attempted_breaks": len(attempts),
        "accepted_candidates": sum(
            item["candidate_status"] == "ACCEPTED_CANDIDATE"
            for item in attempts
        ),
        "no_attempt_session": not attempts,
        "overlap_rule_defined": False,
        "opening_60s": opening_60s,
    })
    return payload


def observe_session(
    day: date,
    store: ForwardStore,
    calendar_provider,
    source_provider,
    *,
    now: datetime,
    data_dir: Path,
) -> dict:
    validate_collection_time(day, now)
    existing = store.session(day)
    if existing and existing["status"] in TERMINAL_STATUSES:
        return existing["payload"]
    try:
        is_cash_session = calendar_provider.is_cash_session(day)
    except Exception as exc:
        payload = _base_payload(day, "REFUSED_CALENDAR_SOURCE")
        payload["reason"] = f"{type(exc).__name__}: {exc}"
        store.record(payload, now)
        return payload
    if not is_cash_session:
        payload = _base_payload(day, "NO_CASH_SESSION")
        store.record(payload, now)
        return payload
    try:
        bundle = source_provider.fetch(day, data_dir)
    except Exception as exc:
        payload = _base_payload(day, "REFUSED_DATABENTO_SOURCE")
        payload["reason"] = f"{type(exc).__name__}: {exc}"
        if hasattr(source_provider, "attempt_metadata"):
            payload["source_attempt"] = source_provider.attempt_metadata()
        store.record(payload, now)
        return payload
    try:
        payload = evaluate_bundle(day, bundle)
    except Exception as exc:
        payload = _base_payload(day, "REFUSED_EVALUATION")
        payload["reason"] = f"{type(exc).__name__}: {exc}"
        payload["source_provenance"] = bundle.provenance
    store.record(payload, now)
    return payload


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
            raise RuntimeError(
                "another accepted-break forward observer is already running"
            ) from exc
        return self

    def __exit__(self, *_args):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--session-date", type=date.fromisoformat)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    store = ForwardStore(args.db, read_only=args.check)
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
        }, indent=2, sort_keys=True, allow_nan=False))
        return 0
    if args.session_date is None:
        parser.error("--session-date is required unless --check is used")
    now = datetime.now(timezone.utc)
    calendar = AlpacaCalendarProvider(
        os.environ.get("APCA_API_KEY_ID", ""),
        os.environ.get("APCA_API_SECRET_KEY", ""),
        base_url=os.environ.get("APCA_BASE_URL", "https://paper-api.alpaca.markets"),
    )
    source = DatabentoSourceProvider(os.environ.get("DATABENTO_API_KEY", ""))
    with ProcessLock(args.db.with_suffix(args.db.suffix + ".lock")):
        result = observe_session(
            args.session_date,
            store,
            calendar,
            source,
            now=now,
            data_dir=args.db.parent,
        )
    print(json.dumps({
        "session_date": result["session_date"],
        "status": result["status"],
        "attempted_breaks": result.get("attempted_breaks"),
        "accepted_candidates": result.get("accepted_candidates"),
        "estimated_cost_usd": source.estimated_cost_usd,
        "databento_requests": source.request_count,
        "summary": store.summary(),
        "execution_capability": False,
    }, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
