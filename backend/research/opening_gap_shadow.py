"""Broker-free shadow-forward logger for the frozen NQ opening gap fade.

This module is intentionally isolated from the production coordinator.  It
records decisions and executable quotes, but has no order, account, position,
or learning capability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as clock_time
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
SCHEMA_VERSION = 1
FROZEN_THRESHOLD_PCT = 1.278097837
POINT_COSTS = (0.0, 0.5, 1.0, 2.0, 3.0)
FORWARD_CAPTURE_MODES = {"live", "historical_replay"}


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed


def _parse_day(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _canonical(payload: dict) -> tuple[str, str]:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return text, hashlib.sha256(text.encode()).hexdigest()


def _finite_positive(value: float) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("prices must be finite and positive")
    return result


def _at(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.combine(day, clock_time(hour, minute, second), tzinfo=ET)


def _require_local_bar(ts: datetime, day: date, hour: int, minute: int, label: str) -> None:
    local = ts.astimezone(ET)
    if local.date() != day or (local.hour, local.minute, local.second, local.microsecond) != (
        hour,
        minute,
        0,
        0,
    ):
        raise ValueError(f"{label} must be the {hour:02d}:{minute:02d} ET bar timestamp")


def _quote_error(bid: float, ask: float) -> str | None:
    if not all(math.isfinite(value) and value > 0 for value in (bid, ask)):
        return "quote prices must be finite and positive"
    if bid >= ask:
        return "quote bid must be below ask"
    return None


def _quote_timing_error(ts: datetime, boundary: datetime) -> str | None:
    latency = (ts - boundary).total_seconds()
    if latency < 0:
        return "quote precedes the frozen boundary"
    if latency > 5:
        return "quote is more than five seconds after the frozen boundary"
    return None


@dataclass(frozen=True)
class DecisionObservation:
    session_date: str
    capture_mode: str
    captured_at: str
    prior_bar_ts: str
    prior_close: float
    prior_instrument_id: int
    decision_bar_ts: str
    decision_price: float
    current_instrument_id: int
    source: str
    source_hash: str = ""


@dataclass(frozen=True)
class QuoteObservation:
    session_date: str
    quote_ts: str
    bid: float
    ask: float
    source: str
    source_hash: str = ""


class OpeningGapShadowStore:
    """Durable stage-ordered evidence store for one frozen candidate."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS opening_gap_sessions (
                session_date TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                capture_mode TEXT NOT NULL CHECK(capture_mode IN ('live','historical_replay')),
                operationally_eligible INTEGER NOT NULL,
                status TEXT NOT NULL,
                refusal_reason TEXT,
                threshold_pct REAL NOT NULL,
                decision_captured_at REAL,
                prior_bar_ts REAL,
                prior_close REAL,
                prior_instrument_id INTEGER,
                decision_bar_ts REAL,
                decision_price REAL,
                current_instrument_id INTEGER,
                gap_pct REAL,
                side TEXT CHECK(side IN ('buy','sell') OR side IS NULL),
                entry_quote_ts REAL,
                entry_bid REAL,
                entry_ask REAL,
                entry_price REAL,
                entry_latency_ms REAL,
                exit_quote_ts REAL,
                exit_bid REAL,
                exit_ask REAL,
                exit_price REAL,
                exit_latency_ms REAL,
                gross_points REAL,
                source TEXT,
                source_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS opening_gap_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at REAL NOT NULL,
                UNIQUE(session_date, event_type)
            );
            CREATE TABLE IF NOT EXISTS opening_gap_diagnostics (
                session_date TEXT NOT NULL,
                delay_seconds INTEGER NOT NULL CHECK(delay_seconds IN (5,10)),
                quote_ts REAL NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                entry_price REAL NOT NULL,
                gross_points REAL,
                source TEXT NOT NULL,
                source_hash TEXT,
                PRIMARY KEY(session_date, delay_seconds)
            );
            CREATE TABLE IF NOT EXISTS opening_gap_reference_closes (
                session_date TEXT PRIMARY KEY,
                bar_ts REAL NOT NULL,
                close_price REAL NOT NULL,
                instrument_id INTEGER NOT NULL,
                captured_at REAL NOT NULL,
                capture_mode TEXT NOT NULL CHECK(capture_mode IN ('live','historical_replay')),
                source TEXT NOT NULL,
                source_hash TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_opening_gap_status
                ON opening_gap_sessions(status, operationally_eligible);
            CREATE TRIGGER IF NOT EXISTS opening_gap_events_no_update
                BEFORE UPDATE ON opening_gap_events
                BEGIN SELECT RAISE(ABORT, 'opening_gap_events is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS opening_gap_events_no_delete
                BEFORE DELETE ON opening_gap_events
                BEGIN SELECT RAISE(ABORT, 'opening_gap_events is append-only'); END;
            """
        )
        conn.commit()

    def record_reference_close(
        self,
        session_date: str,
        bar_ts: str,
        close_price: float,
        instrument_id: int,
        captured_at: str,
        capture_mode: str,
        source: str,
        source_hash: str = "",
    ) -> bool:
        day = _parse_day(session_date)
        if day.weekday() >= 5:
            raise ValueError("session_date cannot be a weekend")
        bar = _parse_datetime(bar_ts)
        captured = _parse_datetime(captured_at)
        mode = capture_mode.strip().lower()
        _require_local_bar(bar, day, 15, 59, "bar_ts")
        if mode not in FORWARD_CAPTURE_MODES:
            raise ValueError("capture_mode must be live or historical_replay")
        if mode == "live":
            local = captured.astimezone(ET)
            if local.date() != day or not (_at(day, 16, 0) <= local < _at(day, 16, 1)):
                raise ValueError("live reference close must be captured during 16:00 ET")
        price = _finite_positive(close_price)
        payload = {
            "session_date": str(day), "bar_ts": bar.isoformat(),
            "close_price": price, "instrument_id": int(instrument_id),
            "captured_at": captured.isoformat(), "capture_mode": mode,
            "source": source, "source_hash": source_hash,
        }
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                inserted = self._append_event(conn, str(day), "REFERENCE_CLOSE", payload)
                if not inserted:
                    conn.rollback()
                    return False
                conn.execute(
                    """INSERT INTO opening_gap_reference_closes
                       (session_date,bar_ts,close_price,instrument_id,captured_at,
                        capture_mode,source,source_hash)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        str(day), bar.timestamp(), price, int(instrument_id),
                        captured.timestamp(), mode, source, source_hash,
                    ),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def latest_reference_close(self, before_session_date: str) -> dict | None:
        day = str(_parse_day(before_session_date))
        row = self._connect().execute(
            """SELECT * FROM opening_gap_reference_closes
               WHERE session_date < ? ORDER BY session_date DESC LIMIT 1""",
            (day,),
        ).fetchone()
        return dict(row) if row else None

    def _append_event(self, conn: sqlite3.Connection, day: str, event_type: str, payload: dict) -> bool:
        text, digest = _canonical(payload)
        existing = conn.execute(
            "SELECT event_hash FROM opening_gap_events WHERE session_date=? AND event_type=?",
            (day, event_type),
        ).fetchone()
        if existing:
            if existing["event_hash"] == digest:
                return False
            raise ValueError(f"conflicting {event_type} retry for {day}")
        conn.execute(
            """INSERT INTO opening_gap_events
               (session_date,event_type,event_hash,payload_json,recorded_at)
               VALUES (?,?,?,?,?)""",
            (day, event_type, digest, text, time.time()),
        )
        return True

    @staticmethod
    def _validate_capture(day: date, mode: str, captured_at: datetime) -> None:
        if mode not in FORWARD_CAPTURE_MODES:
            raise ValueError("capture_mode must be live or historical_replay")
        if mode == "live":
            local = captured_at.astimezone(ET)
            if local.date() != day or not (_at(day, 9, 29) <= local < _at(day, 9, 30)):
                raise ValueError("live decision must be captured during 09:29 ET")

    def record_decision(self, observation: DecisionObservation) -> bool:
        payload = asdict(observation)
        day = _parse_day(observation.session_date)
        if day.weekday() >= 5:
            raise ValueError("session_date cannot be a weekend")
        mode = observation.capture_mode.strip().lower()
        captured_at = _parse_datetime(observation.captured_at)
        prior_ts = _parse_datetime(observation.prior_bar_ts)
        decision_ts = _parse_datetime(observation.decision_bar_ts)
        prior_close = _finite_positive(observation.prior_close)
        decision_price = _finite_positive(observation.decision_price)
        prior_day = prior_ts.astimezone(ET).date()
        if prior_day >= day:
            raise ValueError("prior close must come from an earlier session date")
        _require_local_bar(prior_ts, prior_day, 15, 59, "prior_bar_ts")
        _require_local_bar(decision_ts, day, 9, 28, "decision_bar_ts")
        self._validate_capture(day, mode, captured_at)
        same_instrument = int(observation.prior_instrument_id) == int(observation.current_instrument_id)
        gap_pct = (decision_price / prior_close - 1.0) * 100.0
        side = "sell" if gap_pct > FROZEN_THRESHOLD_PCT else "buy" if gap_pct < -FROZEN_THRESHOLD_PCT else None
        if not same_instrument:
            status, refusal = "REFUSED_DECISION", "continuous-contract instrument changed"
            side = None
        elif side is None:
            status, refusal = "NO_SIGNAL", None
        else:
            status, refusal = "SIGNAL_AWAITING_ENTRY", None
        eligible = mode == "live" and same_instrument

        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                inserted = self._append_event(conn, str(day), "DECISION", payload)
                if not inserted:
                    conn.rollback()
                    return False
                if conn.execute(
                    "SELECT 1 FROM opening_gap_sessions WHERE session_date=?", (str(day),)
                ).fetchone():
                    raise ValueError(f"session {day} already has an initial record")
                now = time.time()
                conn.execute(
                    """INSERT INTO opening_gap_sessions
                       (session_date,schema_version,created_at,updated_at,capture_mode,
                        operationally_eligible,status,refusal_reason,threshold_pct,
                        decision_captured_at,prior_bar_ts,prior_close,prior_instrument_id,
                        decision_bar_ts,decision_price,current_instrument_id,gap_pct,side,
                        source,source_hash)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(day), SCHEMA_VERSION, now, now, mode, int(eligible), status,
                        refusal, FROZEN_THRESHOLD_PCT, captured_at.timestamp(),
                        prior_ts.timestamp(), prior_close, int(observation.prior_instrument_id),
                        decision_ts.timestamp(), decision_price,
                        int(observation.current_instrument_id), gap_pct, side,
                        observation.source, observation.source_hash,
                    ),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def record_decision_refusal(
        self,
        session_date: str,
        capture_mode: str,
        captured_at: str,
        reason: str,
        source: str,
    ) -> bool:
        day = _parse_day(session_date)
        if day.weekday() >= 5:
            raise ValueError("session_date cannot be a weekend")
        captured = _parse_datetime(captured_at)
        mode = capture_mode.strip().lower()
        self._validate_capture(day, mode, captured)
        if not reason.strip():
            raise ValueError("refusal reason is required")
        payload = {
            "session_date": str(day), "capture_mode": mode,
            "captured_at": captured.isoformat(), "reason": reason.strip(), "source": source,
        }
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                inserted = self._append_event(conn, str(day), "DECISION_REFUSAL", payload)
                if not inserted:
                    conn.rollback()
                    return False
                if conn.execute(
                    "SELECT 1 FROM opening_gap_sessions WHERE session_date=?", (str(day),)
                ).fetchone():
                    raise ValueError(f"session {day} already has an initial record")
                now = time.time()
                conn.execute(
                    """INSERT INTO opening_gap_sessions
                       (session_date,schema_version,created_at,updated_at,capture_mode,
                        operationally_eligible,status,refusal_reason,threshold_pct,source)
                       VALUES (?,?,?,?,?,0,'REFUSED_DECISION',?,?,?)""",
                    (str(day), SCHEMA_VERSION, now, now, mode, reason.strip(), FROZEN_THRESHOLD_PCT, source),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def _session(self, conn: sqlite3.Connection, day: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM opening_gap_sessions WHERE session_date=?", (day,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no decision exists for {day}")
        return row

    def _refuse_quote(
        self,
        conn: sqlite3.Connection,
        day: str,
        event_type: str,
        status: str,
        payload: dict,
        reason: str,
    ) -> bool:
        payload = dict(payload, accepted=False, refusal_reason=reason)
        inserted = self._append_event(conn, day, event_type, payload)
        if inserted:
            conn.execute(
                """UPDATE opening_gap_sessions SET status=?, refusal_reason=?,
                   operationally_eligible=0, updated_at=? WHERE session_date=?""",
                (status, reason, time.time(), day),
            )
        return False

    def record_entry(self, observation: QuoteObservation) -> bool:
        return self._record_boundary_quote(observation, "ENTRY")

    def record_exit(self, observation: QuoteObservation) -> bool:
        return self._record_boundary_quote(observation, "EXIT")

    def _record_boundary_quote(self, observation: QuoteObservation, phase: str) -> bool:
        day = str(_parse_day(observation.session_date))
        ts = _parse_datetime(observation.quote_ts)
        bid, ask = float(observation.bid), float(observation.ask)
        boundary = (
            _at(_parse_day(day), 9, 30)
            if phase == "ENTRY"
            else _at(_parse_day(day), 9, 32)
        )
        payload = asdict(observation)
        expected_status = "SIGNAL_AWAITING_ENTRY" if phase == "ENTRY" else "SIGNAL_OPEN"
        refused_status = "REFUSED_ENTRY" if phase == "ENTRY" else "REFUSED_EXIT"
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._session(conn, day)
                existing = conn.execute(
                    "SELECT event_hash FROM opening_gap_events WHERE session_date=? AND event_type=?",
                    (day, phase),
                ).fetchone()
                if existing:
                    _, digest = _canonical(dict(payload, accepted=True))
                    if existing["event_hash"] == digest:
                        conn.rollback()
                        return False
                    raise ValueError(f"conflicting {phase} retry for {day}")
                if row["status"] != expected_status:
                    raise ValueError(f"{phase.lower()} not allowed from status {row['status']}")
                error = _quote_error(bid, ask) or _quote_timing_error(ts, boundary)
                if error:
                    result = self._refuse_quote(
                        conn, day, phase, refused_status, payload, error
                    )
                    conn.commit()
                    return result
                side_sign = 1 if row["side"] == "buy" else -1
                selected = ask if side_sign > 0 else bid
                accepted_payload = dict(payload, accepted=True)
                self._append_event(conn, day, phase, accepted_payload)
                latency_ms = (ts - boundary).total_seconds() * 1000.0
                if phase == "ENTRY":
                    conn.execute(
                        """UPDATE opening_gap_sessions SET status='SIGNAL_OPEN',
                           entry_quote_ts=?,entry_bid=?,entry_ask=?,entry_price=?,
                           entry_latency_ms=?,updated_at=? WHERE session_date=?""",
                        (ts.timestamp(), bid, ask, selected, latency_ms, time.time(), day),
                    )
                else:
                    exit_price = bid if side_sign > 0 else ask
                    gross = side_sign * (exit_price - float(row["entry_price"]))
                    conn.execute(
                        """UPDATE opening_gap_sessions SET status='COMPLETE',
                           exit_quote_ts=?,exit_bid=?,exit_ask=?,exit_price=?,
                           exit_latency_ms=?,gross_points=?,updated_at=? WHERE session_date=?""",
                        (
                            ts.timestamp(), bid, ask, exit_price, latency_ms, gross,
                            time.time(), day,
                        ),
                    )
                    diagnostics = conn.execute(
                        "SELECT delay_seconds,entry_price FROM opening_gap_diagnostics WHERE session_date=?",
                        (day,),
                    ).fetchall()
                    for diagnostic in diagnostics:
                        delayed_gross = side_sign * (exit_price - float(diagnostic["entry_price"]))
                        conn.execute(
                            """UPDATE opening_gap_diagnostics SET gross_points=?
                               WHERE session_date=? AND delay_seconds=?""",
                            (delayed_gross, day, diagnostic["delay_seconds"]),
                        )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def record_delayed_entry(self, delay_seconds: int, observation: QuoteObservation) -> bool:
        if int(delay_seconds) not in (5, 10):
            raise ValueError("delay_seconds must be 5 or 10")
        delay_seconds = int(delay_seconds)
        day = str(_parse_day(observation.session_date))
        ts = _parse_datetime(observation.quote_ts)
        bid, ask = float(observation.bid), float(observation.ask)
        boundary = _at(_parse_day(day), 9, 30, delay_seconds)
        payload = dict(asdict(observation), delay_seconds=delay_seconds)
        event_type = f"DELAYED_ENTRY_{delay_seconds}S"
        error = _quote_error(bid, ask) or _quote_timing_error(ts, boundary)
        if error:
            raise ValueError(error)
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._session(conn, day)
                if row["status"] not in ("SIGNAL_OPEN", "COMPLETE"):
                    raise ValueError(f"delayed entry not allowed from status {row['status']}")
                inserted = self._append_event(conn, day, event_type, payload)
                if not inserted:
                    conn.rollback()
                    return False
                side_sign = 1 if row["side"] == "buy" else -1
                entry = ask if side_sign > 0 else bid
                gross = None
                if row["exit_price"] is not None:
                    gross = side_sign * (float(row["exit_price"]) - entry)
                conn.execute(
                    """INSERT INTO opening_gap_diagnostics
                       (session_date,delay_seconds,quote_ts,bid,ask,entry_price,
                        gross_points,source,source_hash)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        day, delay_seconds, ts.timestamp(), bid, ask, entry, gross,
                        observation.source, observation.source_hash,
                    ),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def mark_stage_failure(self, session_date: str, phase: str, reason: str, source: str) -> bool:
        day = str(_parse_day(session_date))
        phase = phase.strip().upper()
        if phase not in ("ENTRY", "EXIT"):
            raise ValueError("phase must be ENTRY or EXIT")
        if not reason.strip():
            raise ValueError("failure reason is required")
        expected = "SIGNAL_AWAITING_ENTRY" if phase == "ENTRY" else "SIGNAL_OPEN"
        status = f"REFUSED_{phase}"
        payload = {"session_date": day, "phase": phase, "reason": reason.strip(), "source": source}
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._session(conn, day)
                if row["status"] != expected:
                    raise ValueError(f"{phase.lower()} failure not allowed from status {row['status']}")
                inserted = self._append_event(conn, day, phase, dict(payload, accepted=False))
                if not inserted:
                    conn.rollback()
                    return False
                conn.execute(
                    """UPDATE opening_gap_sessions SET status=?,refusal_reason=?,
                       operationally_eligible=0,updated_at=? WHERE session_date=?""",
                    (status, reason.strip(), time.time(), day),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _bootstrap_ci(values: list[float], samples: int = 5000) -> list[float] | None:
        if len(values) < 2:
            return None
        rng = random.Random("opening-gap-shadow-v1")
        estimates = [mean(rng.choice(values) for _ in values) for _ in range(samples)]
        estimates.sort()
        return [estimates[int(samples * 0.025)], estimates[int(samples * 0.975)]]

    def summary(
        self,
        commission_round_trip_usd: float = 0.0,
        point_value_usd: float = 20.0,
    ) -> dict:
        commission_round_trip_usd = float(commission_round_trip_usd)
        point_value_usd = float(point_value_usd)
        if commission_round_trip_usd < 0:
            raise ValueError("commission_round_trip_usd cannot be negative")
        if not math.isfinite(point_value_usd) or point_value_usd <= 0:
            raise ValueError("point_value_usd must be finite and positive")
        commission_points = commission_round_trip_usd / point_value_usd
        conn = self._connect()
        states = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status,COUNT(*) AS n FROM opening_gap_sessions GROUP BY status"
            )
        }
        rows = conn.execute(
            """SELECT session_date,gross_points FROM opening_gap_sessions
               WHERE status='COMPLETE' AND operationally_eligible=1
               ORDER BY session_date"""
        ).fetchall()
        gross = [float(row["gross_points"]) for row in rows]
        gross_by_month: dict[str, list[float]] = {}
        for row in rows:
            gross_by_month.setdefault(str(row["session_date"])[:7], []).append(
                float(row["gross_points"])
            )
        scenarios = {}
        for cost in POINT_COSTS:
            values = [value - cost - commission_points for value in gross]
            positives = sum(value for value in values if value > 0)
            negatives = -sum(value for value in values if value < 0)
            scenarios[str(cost)] = {
                "n": len(values),
                "wins": sum(value > 0 for value in values),
                "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
                "mean_points": mean(values) if values else None,
                "median_points": median(values) if values else None,
                "profit_factor": positives / negatives if negatives else None,
                "profit_factor_infinite": bool(positives and not negatives),
                "bootstrap_95_ci": self._bootstrap_ci(values),
                "mean_without_best": (
                    mean(sorted(values)[:-1]) if len(values) > 1 else None
                ),
            }
        diagnostics = {}
        for delay in (5, 10):
            values = [
                float(row[0])
                for row in conn.execute(
                    """SELECT d.gross_points FROM opening_gap_diagnostics d
                       JOIN opening_gap_sessions s USING(session_date)
                       WHERE d.delay_seconds=? AND d.gross_points IS NOT NULL
                         AND s.operationally_eligible=1""",
                    (delay,),
                )
            ]
            diagnostics[str(delay)] = {
                "n": len(values), "mean_points": mean(values) if values else None,
                "median_points": median(values) if values else None,
                "mean_after_one_point_and_commission": (
                    mean(value - 1.0 - commission_points for value in values)
                    if values else None
                ),
            }
        primary = scenarios["1.0"]
        leave_one_month_out = {}
        for month in sorted(gross_by_month):
            remaining = [
                value - 1.0 - commission_points
                for other, month_values in gross_by_month.items()
                if other != month
                for value in month_values
            ]
            leave_one_month_out[month] = mean(remaining) if remaining else None
        month_minimum = (
            min(value for value in leave_one_month_out.values() if value is not None)
            if any(value is not None for value in leave_one_month_out.values()) else None
        )
        blockers = []
        if commission_round_trip_usd <= 0:
            blockers.append("actual round-trip commission must be supplied")
        if len(gross) < 60:
            blockers.append(f"need 60 eligible completions; have {len(gross)}")
        if primary["mean_points"] is None or primary["mean_points"] <= 0:
            blockers.append("mean after one-point cost is not positive")
        if primary["median_points"] is None or primary["median_points"] <= 0:
            blockers.append("median after one-point cost is not positive")
        lower = primary["bootstrap_95_ci"][0] if primary["bootstrap_95_ci"] else None
        if lower is None or lower <= 0:
            blockers.append("bootstrap lower bound after one-point cost is not above zero")
        pf = primary["profit_factor"]
        if not primary["profit_factor_infinite"] and (pf is None or pf <= 1.10):
            blockers.append("profit factor after one-point cost is not above 1.10")
        if primary["mean_without_best"] is None or primary["mean_without_best"] <= 0:
            blockers.append("result depends on or cannot survive best-session removal")
        if len(gross_by_month) < 3:
            blockers.append(f"need at least 3 independent calendar months; have {len(gross_by_month)}")
        elif month_minimum is None or month_minimum <= 0:
            blockers.append("result does not survive removal of every individual month")
        if diagnostics["5"]["n"] < len(gross) or diagnostics["10"]["n"] < len(gross):
            blockers.append("5-second and 10-second delayed-entry diagnostics are incomplete")
        for delay in ("5", "10"):
            delayed_mean = diagnostics[delay]["mean_after_one_point_and_commission"]
            if delayed_mean is None or delayed_mean <= 0:
                blockers.append(
                    f"{delay}-second delayed entry is not positive after cost and commission"
                )
        return {
            "schema_version": SCHEMA_VERSION,
            "threshold_pct": FROZEN_THRESHOLD_PCT,
            "commission_round_trip_usd": commission_round_trip_usd,
            "point_value_usd": point_value_usd,
            "commission_points": commission_points,
            "states": states,
            "eligible_completions": len(gross),
            "initial_review_ready": len(gross) >= 30,
            "strong_review_ready": len(gross) >= 60,
            "cost_scenarios": scenarios,
            "delayed_entry_diagnostics": diagnostics,
            "calendar_months": len(gross_by_month),
            "leave_one_month_out_mean_points": leave_one_month_out,
            "leave_one_month_out_minimum_mean_points": month_minimum,
            "research_gate_passed": not blockers,
            "blockers": blockers,
            "execution_authorized": False,
        }

    def session(self, session_date: str) -> dict | None:
        row = self._connect().execute(
            "SELECT * FROM opening_gap_sessions WHERE session_date=?",
            (str(_parse_day(session_date)),),
        ).fetchone()
        return dict(row) if row else None


def _print(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    decision = sub.add_parser("decision")
    for name in ("session_date", "capture_mode", "captured_at", "prior_bar_ts",
                 "prior_close", "prior_instrument_id", "decision_bar_ts",
                 "decision_price", "current_instrument_id", "source"):
        decision.add_argument("--" + name.replace("_", "-"), required=True)
    decision.add_argument("--source-hash", default="")

    refusal = sub.add_parser("decision-refusal")
    for name in ("session_date", "capture_mode", "captured_at", "reason", "source"):
        refusal.add_argument("--" + name.replace("_", "-"), required=True)

    reference = sub.add_parser("reference-close")
    for name in ("session_date", "bar_ts", "close_price", "instrument_id",
                 "captured_at", "capture_mode", "source"):
        reference.add_argument("--" + name.replace("_", "-"), required=True)
    reference.add_argument("--source-hash", default="")

    for command in ("entry", "exit"):
        quote = sub.add_parser(command)
        for name in ("session_date", "quote_ts", "bid", "ask", "source"):
            quote.add_argument("--" + name.replace("_", "-"), required=True)
        quote.add_argument("--source-hash", default="")

    delayed = sub.add_parser("delayed-entry")
    delayed.add_argument("--delay-seconds", required=True, type=int, choices=(5, 10))
    for name in ("session_date", "quote_ts", "bid", "ask", "source"):
        delayed.add_argument("--" + name.replace("_", "-"), required=True)
    delayed.add_argument("--source-hash", default="")

    failure = sub.add_parser("stage-failure")
    for name in ("session_date", "phase", "reason", "source"):
        failure.add_argument("--" + name.replace("_", "-"), required=True)

    status = sub.add_parser("status")
    status.add_argument("--session-date")
    status.add_argument("--commission-round-trip-usd", type=float, default=0.0)
    status.add_argument("--point-value-usd", type=float, default=20.0)

    args = parser.parse_args()
    store = OpeningGapShadowStore(args.db)
    if args.command == "decision":
        value = DecisionObservation(
            args.session_date, args.capture_mode, args.captured_at,
            args.prior_bar_ts, float(args.prior_close), int(args.prior_instrument_id),
            args.decision_bar_ts, float(args.decision_price),
            int(args.current_instrument_id), args.source, args.source_hash,
        )
        _print({"recorded": store.record_decision(value), "session": store.session(args.session_date)})
    elif args.command == "decision-refusal":
        recorded = store.record_decision_refusal(
            args.session_date, args.capture_mode, args.captured_at, args.reason, args.source
        )
        _print({"recorded": recorded, "session": store.session(args.session_date)})
    elif args.command == "reference-close":
        recorded = store.record_reference_close(
            args.session_date, args.bar_ts, float(args.close_price),
            int(args.instrument_id), args.captured_at, args.capture_mode,
            args.source, args.source_hash,
        )
        _print({"recorded": recorded, "reference": store.latest_reference_close("9999-12-31")})
    elif args.command in ("entry", "exit", "delayed-entry"):
        value = QuoteObservation(
            args.session_date, args.quote_ts, float(args.bid), float(args.ask),
            args.source, args.source_hash,
        )
        if args.command == "entry":
            recorded = store.record_entry(value)
        elif args.command == "exit":
            recorded = store.record_exit(value)
        else:
            recorded = store.record_delayed_entry(args.delay_seconds, value)
        _print({"recorded": recorded, "session": store.session(args.session_date)})
    elif args.command == "stage-failure":
        recorded = store.mark_stage_failure(
            args.session_date, args.phase, args.reason, args.source
        )
        _print({"recorded": recorded, "session": store.session(args.session_date)})
    else:
        _print(
            store.session(args.session_date)
            if args.session_date
            else store.summary(args.commission_round_trip_usd, args.point_value_usd)
        )


if __name__ == "__main__":
    main()
