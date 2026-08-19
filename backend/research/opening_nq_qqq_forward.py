"""Future-only, post-close observer for the frozen NQ-to-QQQ opening signal.

The module retrieves observations and writes research evidence. It cannot
submit orders, inspect accounts or positions, write learning state, or enable
any production strategy.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import math
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from backend.research.opening_executable_bbo import AuditRefusal, BudgetRefusal
from backend.research.opening_nq_qqq_bridge import (
    EquityQuote,
    MARKS,
    PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE,
    RateLimiter,
    _request_first_quote,
)
from backend.research.opening_ofi import extract_event_buckets


ET = ZoneInfo("America/New_York")
FREEZE_DATE = date(2026, 8, 18)
FIRST_ELIGIBLE_SESSION = date(2026, 8, 19)
EARLIEST_SAME_DAY_RUN = time(16, 20)
THRESHOLD_PCT = 1.278097837
DATASET = "GLBX.MDP3"
NQ_SYMBOL = "NQ.v.0"
QQQ_SYMBOL = "QQQ"
API_ROOT = "https://hist.databento.com/v0"
MAX_DATABENTO_COST_USD = 0.01
MAX_DATABENTO_REQUESTS = 10
MAX_ORDERFLOW_COST_USD = 0.50
MAX_ORDERFLOW_BYTES = 64 * 1024 * 1024
MIN_FREE_DISK_HEADROOM_BYTES = 256 * 1024 * 1024
PRICE_SCALE = 1_000_000_000
TERMINAL_STATUSES = {"NO_SIGNAL", "COMPLETE"}

CONTRACT = {
    "frozen": str(FREEZE_DATE),
    "first_eligible_session": str(FIRST_ELIGIBLE_SESSION),
    "nq_symbol": NQ_SYMBOL,
    "calendar_source": "Alpaca /v2/calendar",
    "gap_reference": "immediately_preceding_full_cash_session_15:59_ET_close",
    "gap_decision": "current_09:28_ET_close",
    "same_instrument_required": True,
    "threshold_pct_strictly_greater_than": THRESHOLD_PCT,
    "direction": "fade",
    "qqq_symbol": QQQ_SYMBOL,
    "qqq_feed": "sip",
    "marks_et": {name: "%02d:%02d:%02d" % hms for name, hms in MARKS.items()},
    "max_quote_delay_seconds": 2.0,
    "primary_extra_slippage_per_share": PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE,
    "all_session_qqq_baseline": True,
    "orderflow": {
        "dataset": DATASET,
        "schema": "mbp-1",
        "window_et": ["09:29:55", "09:32:06"],
        "diagnostic_windows_et": [
            ["09:30:01", "09:30:11"],
            ["09:30:01", "09:30:31"],
            ["09:30:01", "09:32:01"],
        ],
        "may_affect_signal_or_primary_outcome": False,
    },
}
CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(CONTRACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


class ForwardRefusal(ValueError):
    """The requested run violates the frozen forward-observation contract."""


@dataclass(frozen=True)
class NQBar:
    ts: datetime
    close: float
    instrument_id: int
    source_hash: str


@dataclass(frozen=True)
class CashSessionContext:
    session_date: date
    prior_session_date: date
    prior_close_time: time


def _canonical(payload: dict) -> tuple[str, str]:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return text, hashlib.sha256(text.encode()).hexdigest()


def _at(day: date, hms: tuple[int, int, int]) -> datetime:
    return datetime.combine(day, time(*hms), tzinfo=ET)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_session_time(day: date, now: datetime) -> None:
    if now.tzinfo is None:
        raise ForwardRefusal("now must include a timezone")
    local = now.astimezone(ET)
    if day < FIRST_ELIGIBLE_SESSION:
        raise ForwardRefusal(
            f"session {day} predates the immutable forward boundary {FIRST_ELIGIBLE_SESSION}"
        )
    if day.weekday() >= 5:
        raise ForwardRefusal("session date cannot be a weekend")
    if day > local.date():
        raise ForwardRefusal("future sessions cannot be collected")
    if day == local.date() and local.time() < EARLIEST_SAME_DAY_RUN:
        raise ForwardRefusal("same-day collection cannot begin before 16:20 ET")


def _validate_bar(bar: NQBar, day: date, hms: tuple[int, int, int], label: str) -> None:
    local = bar.ts.astimezone(ET)
    if (local.date(), local.hour, local.minute, local.second, local.microsecond) != (
        day, hms[0], hms[1], hms[2], 0,
    ):
        raise AuditRefusal(f"{label} is not the exact {hms[0]:02d}:{hms[1]:02d} ET bar")
    if not math.isfinite(bar.close) or bar.close <= 0:
        raise AuditRefusal(f"{label} has invalid close")
    if bar.instrument_id <= 0:
        raise AuditRefusal(f"{label} has invalid instrument ID")


def _validate_quote(quote: EquityQuote, day: date, hms: tuple[int, int, int], label: str) -> None:
    nominal = _at(day, hms)
    delay = (quote.ts.astimezone(ET) - nominal).total_seconds()
    values = (quote.bid, quote.ask, quote.bid_size, quote.ask_size)
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise AuditRefusal(f"{label} has non-positive or non-finite price/size")
    if quote.bid >= quote.ask:
        raise AuditRefusal(f"{label} is locked or crossed")
    if delay < 0 or delay > 2.0:
        raise AuditRefusal(f"{label} delay {delay:.6f}s violates frozen mark window")


def _bar_payload(bar: NQBar) -> dict:
    return {
        "ts": bar.ts.isoformat(),
        "close": bar.close,
        "instrument_id": bar.instrument_id,
        "source_hash": bar.source_hash,
    }


def _quote_payload(quote: EquityQuote) -> dict:
    return {
        "ts": quote.source_timestamp,
        "bid": quote.bid,
        "ask": quote.ask,
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
        "bid_exchange": quote.bid_exchange,
        "ask_exchange": quote.ask_exchange,
    }


def qqq_null_baseline(marks: dict[str, EquityQuote]) -> dict:
    """Report both executable directions without selecting either one."""

    entry = marks["entry"]
    exit_quote = marks["exit"]

    def values(start: EquityQuote) -> dict:
        long_gross = exit_quote.bid - start.ask
        short_gross = start.bid - exit_quote.ask
        return {
            "long_gross_per_share": long_gross,
            "short_gross_per_share": short_gross,
            "long_primary_net_per_share": (
                long_gross - PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE
            ),
            "short_primary_net_per_share": (
                short_gross - PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE
            ),
        }

    return {
        "entry": values(entry),
        "delayed_5": values(marks["delayed_5"]),
        "delayed_10": values(marks["delayed_10"]),
        "entry_spread": entry.ask - entry.bid,
        "exit_spread": exit_quote.ask - exit_quote.bid,
        "midpoint_change": (
            (exit_quote.bid + exit_quote.ask) / 2.0
            - (entry.bid + entry.ask) / 2.0
        ),
        "direction_selected": False,
    }


ORDERFLOW_WINDOWS = {
    "first_10_seconds": ((9, 30, 1), (9, 30, 11)),
    "first_30_seconds": ((9, 30, 1), (9, 30, 31)),
    "primary_120_seconds": ((9, 30, 1), (9, 32, 1)),
}


def summarize_orderflow(rows: list[dict], day: date, fade_direction: int) -> dict:
    """Aggregate frozen diagnostic windows; never return a trading decision."""

    if fade_direction not in (-1, 1):
        raise AuditRefusal("fade direction must be -1 or 1")
    gap_direction = -fade_direction
    summaries = {}
    for name, (start_hms, end_hms) in ORDERFLOW_WINDOWS.items():
        start = _at(day, start_hms)
        end = _at(day, end_hms)
        selected = []
        for row in rows:
            ts = datetime.fromisoformat(str(row["timestamp_utc"])).astimezone(ET)
            if start <= ts < end:
                selected.append(row)
        if not selected:
            raise AuditRefusal(f"order-flow window {name} has no valid one-second buckets")
        buy = sum(float(row.get("buy_volume") or 0) for row in selected)
        sell = sum(float(row.get("sell_volume") or 0) for row in selected)
        total = buy + sell
        ofi = sum(float(row.get("ofi") or 0) for row in selected)
        depths = [float(row["mean_depth"]) for row in selected if row.get("mean_depth")]
        spreads = [float(row["mean_spread"]) for row in selected if row.get("mean_spread") is not None]
        queue = [
            float(row["mean_queue_imbalance"])
            for row in selected if row.get("mean_queue_imbalance") is not None
        ]
        micro = [
            float(row["mean_microprice_displacement"])
            for row in selected if row.get("mean_microprice_displacement") is not None
        ]
        open_mid = float(selected[0]["open_mid"])
        close_mid = float(selected[-1]["close_mid"])
        mids = [float(row["close_mid"]) for row in selected]
        gap_progress = gap_direction * (close_mid - open_mid)
        gap_effort = buy if gap_direction > 0 else sell
        gap_extensions = [gap_direction * (mid - open_mid) for mid in mids]
        maximum_extension = max(gap_extensions)
        summaries[name] = {
            "covered_seconds": len(selected),
            "events": sum(int(row.get("events") or 0) for row in selected),
            "trades": sum(int(row.get("trades") or 0) for row in selected),
            "buy_volume": buy,
            "sell_volume": sell,
            "signed_trade_imbalance": (buy - sell) / total if total else None,
            "fade_aligned_trade_imbalance": (
                fade_direction * (buy - sell) / total if total else None
            ),
            "ofi": ofi,
            "mean_depth": sum(depths) / len(depths) if depths else None,
            "depth_normalized_ofi": ofi / (sum(depths) / len(depths)) if depths else None,
            "fade_aligned_depth_normalized_ofi": (
                fade_direction * ofi / (sum(depths) / len(depths)) if depths else None
            ),
            "mean_queue_imbalance": sum(queue) / len(queue) if queue else None,
            "mean_spread": sum(spreads) / len(spreads) if spreads else None,
            "mean_microprice_displacement": sum(micro) / len(micro) if micro else None,
            "open_mid": open_mid,
            "close_mid": close_mid,
            "gap_direction_aggressive_volume": gap_effort,
            "gap_direction_progress_points": gap_progress,
            "gap_direction_progress_per_contract": (
                max(gap_progress, 0.0) / gap_effort if gap_effort else None
            ),
            "gap_direction_max_extension_points": maximum_extension,
            "reversal_from_gap_extreme_points": maximum_extension - gap_progress,
        }
    return {
        "status": "COMPLETE",
        "diagnostic_only": True,
        "may_affect_signal_or_primary_outcome": False,
        "windows": summaries,
    }


class ForwardStore:
    """Append-only attempt ledger with a restart-safe materialized result."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nq_qqq_forward_sessions (
                session_date TEXT PRIMARY KEY,
                contract_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                gap_pct REAL,
                direction INTEGER CHECK(direction IN (-1,1) OR direction IS NULL),
                gross_per_share REAL,
                primary_net_per_share REAL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                first_recorded_at TEXT NOT NULL,
                last_recorded_at TEXT NOT NULL,
                attempt_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nq_qqq_forward_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(session_date, payload_sha256)
            );
            CREATE TRIGGER IF NOT EXISTS nq_qqq_forward_events_no_update
                BEFORE UPDATE ON nq_qqq_forward_events
                BEGIN SELECT RAISE(ABORT, 'forward event ledger is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS nq_qqq_forward_events_no_delete
                BEFORE DELETE ON nq_qqq_forward_events
                BEGIN SELECT RAISE(ABORT, 'forward event ledger is append-only'); END;
            """
        )
        self.conn.commit()

    def session(self, day: date | str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM nq_qqq_forward_sessions WHERE session_date=?", (str(day),)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def events(self, day: date | str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM nq_qqq_forward_events WHERE session_date=? ORDER BY id", (str(day),)
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
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT * FROM nq_qqq_forward_sessions WHERE session_date=?", (str(day),)
            ).fetchone()
            if existing is not None and existing["status"] in TERMINAL_STATUSES:
                if existing["payload_sha256"] == digest:
                    self.conn.rollback()
                    return False
                raise ForwardRefusal("conflicting result for immutable terminal session")
            inserted = self.conn.execute(
                """INSERT OR IGNORE INTO nq_qqq_forward_events
                   (session_date,event_type,payload_json,payload_sha256,recorded_at)
                   VALUES (?,?,?,?,?)""",
                (str(day), status, text, digest, stamp),
            ).rowcount
            if not inserted:
                self.conn.rollback()
                return False
            if existing is None:
                self.conn.execute(
                    """INSERT INTO nq_qqq_forward_sessions
                       (session_date,contract_sha256,status,gap_pct,direction,gross_per_share,
                        primary_net_per_share,payload_json,payload_sha256,first_recorded_at,
                        last_recorded_at,attempt_count)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (
                        str(day), CONTRACT_SHA256, status, payload.get("gap_pct"),
                        payload.get("direction"), payload.get("gross_per_share"),
                        payload.get("primary_net_per_share"), text, digest, stamp, stamp,
                    ),
                )
            else:
                self.conn.execute(
                    """UPDATE nq_qqq_forward_sessions SET
                       status=?,gap_pct=?,direction=?,gross_per_share=?,primary_net_per_share=?,
                       payload_json=?,payload_sha256=?,last_recorded_at=?,attempt_count=attempt_count+1
                       WHERE session_date=?""",
                    (
                        status, payload.get("gap_pct"), payload.get("direction"),
                        payload.get("gross_per_share"), payload.get("primary_net_per_share"),
                        text, digest, stamp, str(day),
                    ),
                )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT status,COUNT(*) AS n FROM nq_qqq_forward_sessions GROUP BY status"
        ).fetchall()
        completed = self.conn.execute(
            "SELECT primary_net_per_share FROM nq_qqq_forward_sessions WHERE status='COMPLETE'"
        ).fetchall()
        values = [float(row[0]) for row in completed]
        payload_rows = self.conn.execute(
            "SELECT status,payload_json FROM nq_qqq_forward_sessions"
        ).fetchall()
        all_long = []
        all_short = []
        all_long_net = []
        all_short_net = []
        no_signal_long = []
        no_signal_short = []
        no_signal_long_net = []
        no_signal_short_net = []
        orderflow_counts = {}
        for row in payload_rows:
            payload = json.loads(row["payload_json"])
            baseline = payload.get("qqq_null_baseline", {}).get("entry", {})
            if baseline.get("long_gross_per_share") is not None:
                all_long.append(float(baseline["long_gross_per_share"]))
                all_short.append(float(baseline["short_gross_per_share"]))
                all_long_net.append(float(baseline["long_primary_net_per_share"]))
                all_short_net.append(float(baseline["short_primary_net_per_share"]))
                if row["status"] == "NO_SIGNAL":
                    no_signal_long.append(float(baseline["long_gross_per_share"]))
                    no_signal_short.append(float(baseline["short_gross_per_share"]))
                    no_signal_long_net.append(float(baseline["long_primary_net_per_share"]))
                    no_signal_short_net.append(float(baseline["short_primary_net_per_share"]))
            orderflow_status = payload.get("orderflow_diagnostics", {}).get("status")
            if orderflow_status:
                orderflow_counts[orderflow_status] = orderflow_counts.get(orderflow_status, 0) + 1

        def average(items: list[float]) -> float | None:
            return sum(items) / len(items) if items else None

        return {
            "contract_sha256": CONTRACT_SHA256,
            "first_eligible_session": str(FIRST_ELIGIBLE_SESSION),
            "counts": {row["status"]: row["n"] for row in rows},
            "completed_signals": len(values),
            "mean_primary_net_per_share": sum(values) / len(values) if values else None,
            "qqq_null_baseline": {
                "all_valid_sessions": len(all_long),
                "mean_long_gross_per_share": average(all_long),
                "mean_short_gross_per_share": average(all_short),
                "mean_long_primary_net_per_share": average(all_long_net),
                "mean_short_primary_net_per_share": average(all_short_net),
                "no_signal_sessions": len(no_signal_long),
                "no_signal_mean_long_gross_per_share": average(no_signal_long),
                "no_signal_mean_short_gross_per_share": average(no_signal_short),
                "no_signal_mean_long_primary_net_per_share": average(no_signal_long_net),
                "no_signal_mean_short_primary_net_per_share": average(no_signal_short_net),
                "direction_selected": False,
            },
            "orderflow_diagnostic_counts": orderflow_counts,
            "execution_authorized": False,
        }


class DatabentoMinuteProvider:
    def __init__(
        self,
        key: str,
        client: requests.Session | None = None,
        max_cost_usd: float = MAX_DATABENTO_COST_USD,
        max_requests: int = MAX_DATABENTO_REQUESTS,
    ):
        if not key:
            raise RuntimeError("DATABENTO_API_KEY is not set")
        self.key = key
        self.client = client or requests.Session()
        self.max_cost_usd = max_cost_usd
        self.max_requests = max_requests
        self.estimated_cost_usd = 0.0
        self.request_count = 0
        self.orderflow_estimated_cost_usd = 0.0
        self.orderflow_downloaded_bytes = 0

    @staticmethod
    def _params(day: date, hms: tuple[int, int, int]) -> dict[str, str]:
        start = _at(day, hms).astimezone(timezone.utc)
        end = start + timedelta(minutes=1)
        return {
            "dataset": DATASET,
            "symbols": NQ_SYMBOL,
            "stype_in": "continuous",
            "schema": "ohlcv-1m",
            "start": _iso_utc(start),
            "end": _iso_utc(end),
            "encoding": "json",
            "pretty_px": "true",
            "pretty_ts": "true",
            "map_symbols": "true",
        }

    @staticmethod
    def _cost(payload) -> float:
        if isinstance(payload, (int, float)):
            return float(payload)
        if isinstance(payload, dict):
            for key in ("cost_usd", "cost"):
                if key in payload:
                    return float(payload[key])
        raise BudgetRefusal("Databento cost response is not numeric")

    @staticmethod
    def _parse_bar(row: dict) -> NQBar | None:
        if "close" not in row:
            return None
        try:
            header = row["hd"]
            raw_ts = header["ts_event"]
            if str(raw_ts).isdigit():
                ts = datetime.fromtimestamp(int(raw_ts) / 1e9, timezone.utc)
            else:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            raw_close = float(row["close"])
            close = raw_close / PRICE_SCALE if abs(raw_close) > 10_000_000 else raw_close
            canonical, digest = _canonical(row)
            del canonical
            return NQBar(ts, close, int(header["instrument_id"]), digest)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise AuditRefusal(f"malformed Databento OHLCV row: {exc}") from exc

    def fetch_bar(self, day: date, hms: tuple[int, int, int]) -> NQBar | None:
        if self.request_count >= self.max_requests:
            raise BudgetRefusal("Databento request-count ceiling reached")
        params = self._params(day, hms)
        cost_params = {
            key: params[key]
            for key in ("dataset", "symbols", "stype_in", "schema", "start", "end")
        }
        cost_response = self.client.get(
            f"{API_ROOT}/metadata.get_cost", params=cost_params,
            auth=(self.key, ""), timeout=(10, 30),
        )
        cost_response.raise_for_status()
        estimate = self._cost(cost_response.json())
        cost_response.close()
        if not math.isfinite(estimate) or estimate < 0:
            raise BudgetRefusal("Databento returned an invalid cost estimate")
        if self.estimated_cost_usd + estimate > self.max_cost_usd:
            raise BudgetRefusal("next Databento request would exceed the $0.01 run cap")
        self.estimated_cost_usd += estimate
        self.request_count += 1
        response = self.client.get(
            f"{API_ROOT}/timeseries.get_range", params=params,
            auth=(self.key, ""), timeout=(10, 30),
        )
        try:
            response.raise_for_status()
            bars = []
            for line in response.text.splitlines():
                if not line.strip():
                    continue
                bar = self._parse_bar(json.loads(line))
                if bar is not None:
                    bars.append(bar)
        finally:
            response.close()
        if not bars:
            return None
        exact = [bar for bar in bars if bar.ts.astimezone(ET) == _at(day, hms)]
        if len(exact) != 1:
            raise AuditRefusal(f"expected one exact NQ bar, found {len(exact)}")
        return exact[0]

    def prior_close(
        self, day: date, candidate: date, prior_close_time: time,
    ) -> tuple[date, NQBar] | None:
        if candidate >= day:
            raise AuditRefusal("calendar returned an invalid preceding session")
        if prior_close_time != time(16, 0):
            raise AuditRefusal(
                f"preceding cash session {candidate} closed early and has no frozen 15:59 reference"
            )
        bar = self.fetch_bar(candidate, (15, 59, 0))
        return (candidate, bar) if bar is not None else None

    @staticmethod
    def _orderflow_params(day: date) -> dict[str, str]:
        start = _at(day, (9, 29, 55)).astimezone(timezone.utc)
        end = _at(day, (9, 32, 6)).astimezone(timezone.utc)
        return {
            "dataset": DATASET,
            "symbols": NQ_SYMBOL,
            "stype_in": "continuous",
            "schema": "mbp-1",
            "start": _iso_utc(start),
            "end": _iso_utc(end),
            "encoding": "json",
            "map_symbols": "true",
        }

    @staticmethod
    def _validate_orderflow_file(path: Path, expected_instrument_id: int) -> tuple[int, str]:
        records = 0
        digest = hashlib.sha256()
        with path.open("rb") as raw:
            for line_number, line in enumerate(raw, 1):
                digest.update(line)
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AuditRefusal(
                        f"invalid MBP-1 JSON at line {line_number}"
                    ) from exc
                if not row.get("levels"):
                    continue
                try:
                    instrument_id = int(row["hd"]["instrument_id"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise AuditRefusal("MBP-1 data row has no instrument ID") from exc
                if instrument_id != expected_instrument_id:
                    raise AuditRefusal(
                        f"MBP-1 instrument {instrument_id} does not match signal instrument "
                        f"{expected_instrument_id}"
                    )
                records += 1
        if not records:
            raise AuditRefusal("MBP-1 response has no usable book records")
        return records, digest.hexdigest()

    def collect_orderflow(
        self,
        day: date,
        expected_instrument_id: int,
        fade_direction: int,
        raw_dir: Path,
    ) -> dict:
        raw_dir.mkdir(parents=True, exist_ok=True)
        target = raw_dir / f"nq_mbp1_{day}_{expected_instrument_id}.jsonl.gz"
        raw_part = raw_dir / f".{target.name}.raw.part"
        compressed_part = raw_dir / f".{target.name}.part"
        reused = target.exists()
        try:
            required_free = MAX_ORDERFLOW_BYTES + MIN_FREE_DISK_HEADROOM_BYTES
            if shutil.disk_usage(raw_dir).free < required_free:
                raise BudgetRefusal(
                    "insufficient disk for bounded MBP-1 processing while preserving "
                    "256 MiB free headroom"
                )
            if reused:
                size = 0
                with gzip.open(target, "rb") as source, raw_part.open("wb") as destination:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_ORDERFLOW_BYTES:
                            raise BudgetRefusal("existing MBP-1 evidence exceeds the 64 MiB cap")
                        destination.write(chunk)
            else:
                params = self._orderflow_params(day)
                cost_params = {
                    key: params[key]
                    for key in ("dataset", "symbols", "stype_in", "schema", "start", "end")
                }
                cost_response = self.client.get(
                    f"{API_ROOT}/metadata.get_cost", params=cost_params,
                    auth=(self.key, ""), timeout=(10, 30),
                )
                try:
                    cost_response.raise_for_status()
                    estimate = self._cost(cost_response.json())
                finally:
                    cost_response.close()
                if not math.isfinite(estimate) or estimate < 0:
                    raise BudgetRefusal("Databento returned an invalid MBP-1 cost estimate")
                if estimate > MAX_ORDERFLOW_COST_USD:
                    raise BudgetRefusal(
                        f"MBP-1 estimate ${estimate:.6f} exceeds the $0.50 diagnostic cap"
                    )
                self.orderflow_estimated_cost_usd += estimate
                response = self.client.get(
                    f"{API_ROOT}/timeseries.get_range", params=params,
                    auth=(self.key, ""), timeout=(10, 60), stream=True,
                )
                try:
                    response.raise_for_status()
                    declared = response.headers.get("Content-Length")
                    if declared and int(declared) > MAX_ORDERFLOW_BYTES:
                        raise BudgetRefusal("declared MBP-1 response exceeds the 64 MiB cap")
                    size = 0
                    with raw_part.open("wb") as destination:
                        for chunk in response.iter_content(64 * 1024):
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > MAX_ORDERFLOW_BYTES:
                                raise BudgetRefusal("streamed MBP-1 response exceeds the 64 MiB cap")
                            destination.write(chunk)
                    self.orderflow_downloaded_bytes += size
                finally:
                    response.close()
            records, source_sha256 = self._validate_orderflow_file(
                raw_part, expected_instrument_id
            )
            rows = extract_event_buckets(raw_part, interval_seconds=1)
            diagnostics = summarize_orderflow(rows, day, fade_direction)
            if not reused:
                with raw_part.open("rb") as source, gzip.open(
                    compressed_part, "wb", compresslevel=6
                ) as destination:
                    shutil.copyfileobj(source, destination)
                compressed_part.replace(target)
            diagnostics["provenance"] = {
                "dataset": DATASET,
                "symbol": NQ_SYMBOL,
                "schema": "mbp-1",
                "window_et": ["09:29:55", "09:32:06"],
                "instrument_id": expected_instrument_id,
                "records": records,
                "uncompressed_sha256": source_sha256,
                "compressed_path": str(target),
                "compressed_bytes": target.stat().st_size,
                "reused": reused,
                "estimated_new_cost_usd": 0.0 if reused else estimate,
            }
            return diagnostics
        finally:
            raw_part.unlink(missing_ok=True)
            compressed_part.unlink(missing_ok=True)


class AlpacaQQQProvider:
    def __init__(
        self, key_id: str, secret_key: str,
        client: requests.Session | None = None, limiter: RateLimiter | None = None,
        trading_api_base_url: str = "https://paper-api.alpaca.markets",
    ):
        if not key_id or not secret_key:
            raise RuntimeError("Alpaca market-data credentials are not set")
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self.client = client or requests.Session()
        self.limiter = limiter or RateLimiter()
        self.trading_api_base_url = trading_api_base_url.rstrip("/")
        if self.trading_api_base_url.endswith("/v2"):
            self.trading_api_base_url = self.trading_api_base_url[:-3]

    def session_context(self, day: date) -> CashSessionContext:
        response = self.client.get(
            f"{self.trading_api_base_url}/v2/calendar",
            headers=self.headers,
            params={"start": str(day - timedelta(days=14)), "end": str(day)},
            timeout=(10, 30),
        )
        try:
            response.raise_for_status()
            payload = response.json()
        finally:
            response.close()
        if not isinstance(payload, list):
            raise AuditRefusal("Alpaca calendar response is not a list")
        sessions = {}
        try:
            for row in payload:
                session_day = date.fromisoformat(str(row["date"]))
                sessions[session_day] = time.fromisoformat(str(row["close"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditRefusal(f"malformed Alpaca calendar response: {exc}") from exc
        if day not in sessions:
            raise AuditRefusal(f"{day} is not an Alpaca cash-market session")
        prior_days = [session_day for session_day in sessions if session_day < day]
        if not prior_days:
            raise AuditRefusal("Alpaca calendar did not include a preceding session")
        prior = max(prior_days)
        return CashSessionContext(day, prior, sessions[prior])

    def marks(self, day: date) -> dict[str, EquityQuote]:
        return {
            name: _request_first_quote(self.client, self.headers, day, hms, self.limiter)
            for name, hms in MARKS.items()
        }


def _base_payload(day: date, status: str) -> dict:
    return {
        "session_date": str(day),
        "status": status,
        "contract_sha256": CONTRACT_SHA256,
        "research_only": True,
        "execution_authorized": False,
    }


def observe_session(
    day: date,
    store: ForwardStore,
    nq_provider,
    qqq_provider,
    now: datetime,
    orderflow_raw_dir: Path | None = None,
) -> dict:
    _validate_session_time(day, now)
    existing = store.session(day)
    if existing and existing["status"] in TERMINAL_STATUSES:
        return existing["payload"]
    try:
        calendar = qqq_provider.session_context(day)
    except Exception as exc:
        payload = _base_payload(day, "REFUSED_CALENDAR_SOURCE")
        payload["reason"] = f"{type(exc).__name__}: {exc}"
        store.record(payload, now)
        return payload
    try:
        if calendar.session_date != day:
            raise AuditRefusal("calendar context belongs to a different candidate session")
        if calendar.prior_close_time != time(16, 0):
            raise AuditRefusal(
                f"preceding cash session {calendar.prior_session_date} closed early "
                "and has no frozen 15:59 reference"
            )
        current = nq_provider.fetch_bar(day, (9, 28, 0))
        prior_result = nq_provider.prior_close(
            day, calendar.prior_session_date, calendar.prior_close_time
        )
        if current is None:
            raise AuditRefusal("current 09:28 NQ bar is unavailable")
        if prior_result is None:
            raise AuditRefusal("no prior NQ 15:59 close found inside ten calendar days")
        prior_day, prior = prior_result
        _validate_bar(current, day, (9, 28, 0), "decision bar")
        _validate_bar(prior, prior_day, (15, 59, 0), "reference bar")
    except Exception as exc:
        payload = _base_payload(day, "REFUSED_NQ_SOURCE")
        payload["reason"] = f"{type(exc).__name__}: {exc}"
        store.record(payload, now)
        return payload
    payload = _base_payload(day, "PENDING")
    payload.update({
        "prior_session_date": str(prior_day),
        "prior_bar": _bar_payload(prior),
        "decision_bar": _bar_payload(current),
    })
    if prior.instrument_id != current.instrument_id:
        payload["status"] = "REFUSED_ROLL_TRANSITION"
        payload["reason"] = "NQ instrument ID changed across the signal boundary"
        store.record(payload, now)
        return payload
    gap_pct = (current.close / prior.close - 1.0) * 100.0
    payload["gap_pct"] = gap_pct
    try:
        marks = qqq_provider.marks(day)
        if set(marks) != set(MARKS):
            raise AuditRefusal("QQQ provider did not return all four frozen marks")
        for name, hms in MARKS.items():
            _validate_quote(marks[name], day, hms, name)
    except Exception as exc:
        payload["status"] = "REFUSED_QQQ_SOURCE"
        payload["reason"] = f"{type(exc).__name__}: {exc}"
        store.record(payload, now)
        return payload
    payload["qqq_marks"] = {name: _quote_payload(marks[name]) for name in MARKS}
    payload["qqq_null_baseline"] = qqq_null_baseline(marks)
    if abs(gap_pct) <= THRESHOLD_PCT:
        payload["status"] = "NO_SIGNAL"
        store.record(payload, now)
        return payload
    direction = -1 if gap_pct > 0 else 1
    payload["direction"] = direction
    entry = marks["entry"]
    exit_quote = marks["exit"]
    entry_price = entry.ask if direction > 0 else entry.bid
    exit_price = exit_quote.bid if direction > 0 else exit_quote.ask
    gross = direction * (exit_price - entry_price)
    delayed = {}
    for name in ("delayed_5", "delayed_10"):
        quote = marks[name]
        delayed_entry = quote.ask if direction > 0 else quote.bid
        delayed[name] = direction * (exit_price - delayed_entry)
    payload.update({
        "status": "COMPLETE",
        "gross_per_share": gross,
        "primary_extra_slippage_per_share": PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE,
        "primary_net_per_share": gross - PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE,
        "delayed_5_gross_per_share": delayed["delayed_5"],
        "delayed_10_gross_per_share": delayed["delayed_10"],
    })
    if orderflow_raw_dir is None:
        payload["orderflow_diagnostics"] = {
            "status": "NOT_CONFIGURED",
            "diagnostic_only": True,
            "may_affect_signal_or_primary_outcome": False,
        }
    else:
        try:
            payload["orderflow_diagnostics"] = nq_provider.collect_orderflow(
                day, current.instrument_id, direction, orderflow_raw_dir
            )
        except Exception as exc:
            payload["orderflow_diagnostics"] = {
                "status": "REFUSED_ORDERFLOW_SOURCE",
                "reason": f"{type(exc).__name__}: {exc}",
                "diagnostic_only": True,
                "may_affect_signal_or_primary_outcome": False,
            }
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
            raise RuntimeError("another NQ/QQQ forward observer is already running") from exc
        return self

    def __exit__(self, *_args):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--session-date", type=date.fromisoformat)
    parser.add_argument(
        "--raw-dir", type=Path,
        help="ignored local directory for compressed qualifying-session MBP-1 evidence",
    )
    parser.add_argument("--check", action="store_true", help="validate config without network access")
    args = parser.parse_args()
    store = ForwardStore(args.db)
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
    nq = DatabentoMinuteProvider(os.environ.get("DATABENTO_API_KEY", ""))
    qqq = AlpacaQQQProvider(
        os.environ.get("APCA_API_KEY_ID", ""), os.environ.get("APCA_API_SECRET_KEY", ""),
        trading_api_base_url=os.environ.get(
            "APCA_BASE_URL", "https://paper-api.alpaca.markets"
        ),
    )
    lock_path = args.db.with_suffix(args.db.suffix + ".lock")
    raw_dir = args.raw_dir or args.db.with_name(args.db.stem + "_raw")
    with ProcessLock(lock_path):
        result = observe_session(
            args.session_date, store, nq, qqq, now, orderflow_raw_dir=raw_dir
        )
    print(json.dumps({
        "session_date": result["session_date"],
        "status": result["status"],
        "gap_pct": result.get("gap_pct"),
        "primary_net_per_share": result.get("primary_net_per_share"),
        "databento_estimated_cost_usd": nq.estimated_cost_usd,
        "databento_requests": nq.request_count,
        "orderflow_estimated_cost_usd": nq.orderflow_estimated_cost_usd,
        "orderflow_downloaded_bytes": nq.orderflow_downloaded_bytes,
        "execution_authorized": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
