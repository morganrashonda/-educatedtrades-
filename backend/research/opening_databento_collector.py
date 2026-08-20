"""Bounded, restart-safe Databento collector for NQ opening research.

The collector is intentionally research-only.  It streams paid historical
responses into compact one-second SQLite tables and never imports production,
broker, execution, learning, or Tier 3 modules.

Safety properties:

* every request is costed and reserved before a potentially billable fetch;
* the cumulative reservation may never exceed ``--max-cost``;
* incomplete reservations are not retried automatically;
* explicitly authorized retries preserve prior attempt evidence and add their
  new estimate to the cumulative cost ledger;
* only compact, derived rows are persisted (never raw MBP-1/trade payloads);
* collection stops before the configured free-disk reserve is crossed;
* each completed session is committed atomically with a source SHA-256.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

import requests


API_ROOT = "https://hist.databento.com/v0"
DATASET = "GLBX.MDP3"
SYMBOL = "NQ.v.0"
STYPE_IN = "continuous"
ET = ZoneInfo("America/New_York")
PRICE_SCALE = 1_000_000_000
WINDOW_START = (9, 28, 0)
WINDOW_END = (9, 36, 0)
DEFAULT_MAX_COST = 78.0
DEFAULT_FREE_RESERVE = 512 * 1024 * 1024
PHASES = ("ohlcv-1s", "bbo-1s", "trades", "mbp-1")


class CollectionRefusal(RuntimeError):
    """The requested operation would violate a frozen safety bound."""


@dataclass(frozen=True)
class RequestWindow:
    session_date: date
    start: datetime
    end: datetime

    @property
    def key_suffix(self) -> str:
        return self.session_date.isoformat()


def _at(day: date, hms: tuple[int, int, int]) -> datetime:
    return datetime.combine(day, dt_time(*hms), ET)


def request_window(day: date) -> RequestWindow:
    return RequestWindow(
        day,
        _at(day, WINDOW_START).astimezone(timezone.utc),
        _at(day, WINDOW_END).astimezone(timezone.utc),
    )


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def request_params(schema: str, window: RequestWindow) -> dict[str, str]:
    return {
        "dataset": DATASET,
        "symbols": SYMBOL,
        "stype_in": STYPE_IN,
        "schema": schema,
        "start": _iso(window.start),
        "end": _iso(window.end),
        "encoding": "json",
        "map_symbols": "false",
    }


def cost_params(schema: str, window: RequestWindow) -> dict[str, str]:
    params = request_params(schema, window)
    return {
        key: params[key]
        for key in ("dataset", "symbols", "stype_in", "schema", "start", "end")
    }


def _extract_cost(response: requests.Response) -> float:
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, (int, float)):
        value = float(payload)
    elif isinstance(payload, dict):
        value = float(payload.get("cost_usd", payload.get("cost")))
    else:
        raise CollectionRefusal("Databento cost response is not numeric")
    if not math.isfinite(value) or value < 0:
        raise CollectionRefusal("Databento returned an invalid cost estimate")
    return value


def estimate_cost(
    client: requests.Session,
    *,
    schema: str,
    window: RequestWindow,
    api_key: str,
    attempts: int = 3,
) -> float:
    """Fetch a non-billable estimate with bounded transient retries."""

    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        response = None
        try:
            response = client.get(
                f"{API_ROOT}/metadata.get_cost",
                params=cost_params(schema, window),
                auth=(api_key, ""),
                timeout=(20, 60),
            )
            return _extract_cost(response)
        except requests.RequestException:
            if attempt + 1 == attempts:
                raise
            time.sleep(1.0 * (2**attempt))
        finally:
            if response is not None:
                response.close()
    raise AssertionError("unreachable")


def _ts_ns(raw: object) -> int:
    text = str(raw)
    if text.isdigit():
        return int(text)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1_000_000_000)


def _px_i(raw: object) -> int:
    value = float(raw)
    if abs(value) < 10_000_000:
        value *= PRICE_SCALE
    return int(round(value))


def _px(raw: object) -> float:
    return _px_i(raw) / PRICE_SCALE


def session_dates_from_bars(
    path: Path,
    *,
    start: date,
    end: date,
) -> list[date]:
    """Read independent cash-session dates from a trusted NQ bar archive."""

    found: set[date] = set()
    with path.open() as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            ts = datetime.fromtimestamp(
                _ts_ns(row["hd"]["ts_event"]) / 1e9, timezone.utc,
            ).astimezone(ET)
            if (
                start <= ts.date() <= end
                and ts.weekday() < 5
                and (ts.hour, ts.minute) == (9, 30)
            ):
                found.add(ts.date())
    if not found:
        raise CollectionRefusal("bar archive produced no eligible opening sessions")
    return sorted(found)


def cash_session_dates_from_csv(
    path: Path,
    *,
    start: date,
    end: date,
) -> list[date]:
    """Return dates with an actual 09:30 ET QQQ cash-market bar."""

    found: set[date] = set()
    with path.open(newline="") as source:
        for row in csv.DictReader(source):
            try:
                ts = datetime.fromisoformat(
                    str(row["timestamp"]).replace("Z", "+00:00")
                ).astimezone(ET)
            except (KeyError, TypeError, ValueError) as exc:
                raise CollectionRefusal(f"invalid cash-calendar row: {exc}") from exc
            if start <= ts.date() <= end and (ts.hour, ts.minute) == (9, 30):
                found.add(ts.date())
    if not found:
        raise CollectionRefusal("cash-calendar archive produced no eligible sessions")
    return sorted(found)


def year_balanced_order(days: Iterable[date]) -> list[date]:
    """Return a deterministic, year-balanced order for expensive MBP-1 days."""

    grouped: dict[int, list[date]] = defaultdict(list)
    for day in days:
        grouped[day.year].append(day)
    for values in grouped.values():
        # A fixed hash ordering spreads the sample through each year without
        # selecting sessions based on outcomes or volatility.
        values.sort(key=lambda value: hashlib.sha256(value.isoformat().encode()).digest())
    result: list[date] = []
    years = sorted(grouped)
    for index in range(max(len(grouped[year]) for year in years)):
        for year in years:
            if index < len(grouped[year]):
                result.append(grouped[year][index])
    return result


def completed_session_dates(
    conn: sqlite3.Connection, *, start: date, end: date
) -> list[date]:
    """Reuse the already-audited OHLCV cash-session calendar from SQLite."""

    rows = conn.execute(
        """
        SELECT session_date FROM requests
        WHERE schema_name = 'ohlcv-1s' AND status = 'complete'
          AND session_date >= ? AND session_date <= ?
        ORDER BY session_date
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    days = [date.fromisoformat(str(row[0])) for row in rows]
    if not days:
        raise CollectionRefusal("completed OHLCV calendar produced no eligible sessions")
    return days


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS requests (
            request_key TEXT PRIMARY KEY,
            schema_name TEXT NOT NULL,
            session_date TEXT NOT NULL,
            start_utc TEXT NOT NULL,
            end_utc TEXT NOT NULL,
            estimated_cost REAL NOT NULL,
            status TEXT NOT NULL,
            raw_records INTEGER,
            derived_rows INTEGER,
            source_sha256 TEXT,
            error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ohlcv_1s (
            session_date TEXT NOT NULL,
            ts_event INTEGER NOT NULL,
            instrument_id INTEGER NOT NULL,
            open_i INTEGER NOT NULL,
            high_i INTEGER NOT NULL,
            low_i INTEGER NOT NULL,
            close_i INTEGER NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (session_date, ts_event, instrument_id)
        );

        CREATE TABLE IF NOT EXISTS bbo_1s (
            session_date TEXT NOT NULL,
            ts_event INTEGER NOT NULL,
            instrument_id INTEGER NOT NULL,
            bid_px_i INTEGER NOT NULL,
            ask_px_i INTEGER NOT NULL,
            bid_sz REAL NOT NULL,
            ask_sz REAL NOT NULL,
            PRIMARY KEY (session_date, ts_event, instrument_id)
        );

        CREATE TABLE IF NOT EXISTS trades_1s (
            session_date TEXT NOT NULL,
            bucket_ns INTEGER NOT NULL,
            instrument_id INTEGER NOT NULL,
            trade_count INTEGER NOT NULL,
            buy_volume REAL NOT NULL,
            sell_volume REAL NOT NULL,
            unknown_volume REAL NOT NULL,
            open_i INTEGER NOT NULL,
            high_i INTEGER NOT NULL,
            low_i INTEGER NOT NULL,
            close_i INTEGER NOT NULL,
            vwap_i REAL,
            PRIMARY KEY (session_date, bucket_ns, instrument_id)
        );

        CREATE TABLE IF NOT EXISTS mbp1_1s (
            session_date TEXT NOT NULL,
            bucket_ns INTEGER NOT NULL,
            instrument_id INTEGER NOT NULL,
            event_count INTEGER NOT NULL,
            trade_count INTEGER NOT NULL,
            buy_volume REAL NOT NULL,
            sell_volume REAL NOT NULL,
            open_mid REAL NOT NULL,
            high_mid REAL NOT NULL,
            low_mid REAL NOT NULL,
            close_mid REAL NOT NULL,
            ofi REAL NOT NULL,
            bid_queue_add REAL NOT NULL,
            bid_queue_remove REAL NOT NULL,
            ask_queue_add REAL NOT NULL,
            ask_queue_remove REAL NOT NULL,
            add_bid_volume REAL NOT NULL,
            add_ask_volume REAL NOT NULL,
            cancel_bid_volume REAL NOT NULL,
            cancel_ask_volume REAL NOT NULL,
            modify_bid_volume REAL NOT NULL,
            modify_ask_volume REAL NOT NULL,
            mean_depth REAL,
            mean_queue_imbalance REAL,
            mean_spread REAL,
            mean_microprice_displacement REAL,
            PRIMARY KEY (session_date, bucket_ns, instrument_id)
        );
        """
    )
    # Additive migration for databases created before explicit rebilling was
    # supported.  The current row remains the cumulative billing ledger; the
    # JSON history preserves each superseded attempt without changing keys.
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")
    }
    if "attempt_count" not in columns:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1"
        )
    if "attempt_history" not in columns:
        conn.execute("ALTER TABLE requests ADD COLUMN attempt_history TEXT")
    conn.commit()
    return conn


def reserved_cost(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT COALESCE(SUM(estimated_cost), 0) FROM requests").fetchone()
    return float(row[0])


def _request_key(schema: str, day: date) -> str:
    return f"{schema}:{day.isoformat()}:{WINDOW_START}-{WINDOW_END}"


def _completed(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT status FROM requests WHERE request_key = ?", (key,)).fetchone()
    return bool(row and row[0] == "complete")


def _existing_status(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT status FROM requests WHERE request_key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def authorize_retry(conn: sqlite3.Connection, key: str) -> None:
    """Authorize one retry while preserving the prior attempt for audit.

    Authorization does not itself reserve or fetch paid data.  The next
    ``_reserve`` call obtains a fresh estimate and adds it to the existing
    cumulative estimate, so both attempts count against the hard cap.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT status, estimated_cost, error, raw_records, derived_rows,
                   source_sha256, updated_at, attempt_count, attempt_history
            FROM requests WHERE request_key = ?
            """,
            (key,),
        ).fetchone()
        if row is None:
            raise CollectionRefusal(f"cannot retry unknown request: {key}")
        status = str(row[0])
        if status not in {"failed", "reserved"}:
            raise CollectionRefusal(
                f"request is not retryable from status {status!r}: {key}"
            )
        try:
            history = json.loads(row[8]) if row[8] else []
        except (TypeError, ValueError) as exc:
            raise CollectionRefusal(f"invalid attempt history for {key}") from exc
        history.append({
            "attempt_number": int(row[7]),
            "status": status,
            "estimated_cost": float(row[1]),
            "error": row[2],
            "raw_records": row[3],
            "derived_rows": row[4],
            "source_sha256": row[5],
            "updated_at": row[6],
        })
        conn.execute(
            """
            UPDATE requests
            SET status = 'retry_authorized', attempt_history = ?, updated_at = ?
            WHERE request_key = ?
            """,
            (json.dumps(history, sort_keys=True), datetime.now(timezone.utc).isoformat(), key),
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _reserve(
    conn: sqlite3.Connection,
    schema: str,
    window: RequestWindow,
    estimate: float,
    max_cost: float,
) -> str:
    key = _request_key(schema, window.session_date)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT status FROM requests WHERE request_key = ?", (key,)
        ).fetchone()
        current_cost = reserved_cost(conn)
        if current_cost + estimate > max_cost + 1e-9:
            raise CollectionRefusal(
                f"next request would exceed cost cap: "
                f"${current_cost + estimate:.6f} > ${max_cost:.2f}"
            )
        now = datetime.now(timezone.utc).isoformat()
        if existing:
            if existing[0] != "retry_authorized":
                raise CollectionRefusal(
                    f"request already exists with status {existing[0]!r}: {key}"
                )
            conn.execute(
                """
                UPDATE requests
                SET estimated_cost = estimated_cost + ?, status = 'reserved',
                    attempt_count = attempt_count + 1, raw_records = NULL,
                    derived_rows = NULL, source_sha256 = NULL, error = NULL,
                    updated_at = ?
                WHERE request_key = ?
                """,
                (estimate, now, key),
            )
        else:
            conn.execute(
                """
                INSERT INTO requests (
                    request_key, schema_name, session_date, start_utc, end_utc,
                    estimated_cost, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    key,
                    schema,
                    window.session_date.isoformat(),
                    _iso(window.start),
                    _iso(window.end),
                    estimate,
                    now,
                ),
            )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return key


def _iter_json(response: requests.Response) -> Iterator[tuple[dict, bytes]]:
    response.raise_for_status()
    for raw in response.iter_lines():
        if not raw.strip():
            continue
        yield json.loads(raw), raw


def _ohlcv_rows(rows: Iterable[dict], day: date) -> list[tuple]:
    result = []
    for row in rows:
        if "hd" not in row or "close" not in row:
            continue
        hd = row["hd"]
        result.append((
            day.isoformat(), _ts_ns(hd["ts_event"]), int(hd["instrument_id"]),
            _px_i(row["open"]), _px_i(row["high"]), _px_i(row["low"]),
            _px_i(row["close"]), float(row.get("volume") or 0),
        ))
    return result


def _bbo_rows(rows: Iterable[dict], day: date) -> list[tuple]:
    result = []
    for row in rows:
        if "hd" not in row or not row.get("levels"):
            continue
        hd = row["hd"]
        level = (row.get("levels") or [{}])[0]
        result.append((
            day.isoformat(), _ts_ns(row.get("ts_recv", hd["ts_event"])),
            int(hd["instrument_id"]),
            _px_i(level["bid_px"]), _px_i(level["ask_px"]),
            float(level.get("bid_sz") or 0), float(level.get("ask_sz") or 0),
        ))
    return result


def _trade_rows(rows: Iterable[dict], day: date) -> list[tuple]:
    buckets: dict[tuple[int, int], dict] = {}
    for row in rows:
        if "hd" not in row or "price" not in row:
            continue
        hd = row["hd"]
        ts_ns = _ts_ns(hd["ts_event"])
        instrument = int(hd["instrument_id"])
        key = (ts_ns - ts_ns % 1_000_000_000, instrument)
        px = _px_i(row["price"])
        size = float(row.get("size") or 0)
        item = buckets.setdefault(key, {
            "count": 0, "buy": 0.0, "sell": 0.0, "unknown": 0.0,
            "open": px, "high": px, "low": px, "close": px,
            "notional": 0.0, "volume": 0.0,
        })
        item["count"] += 1
        item["high"] = max(item["high"], px)
        item["low"] = min(item["low"], px)
        item["close"] = px
        item["notional"] += px * size
        item["volume"] += size
        if row.get("side") == "B":
            item["buy"] += size
        elif row.get("side") == "A":
            item["sell"] += size
        else:
            item["unknown"] += size
    return [(
        day.isoformat(), bucket, instrument, item["count"], item["buy"],
        item["sell"], item["unknown"], item["open"], item["high"],
        item["low"], item["close"],
        item["notional"] / item["volume"] if item["volume"] else None,
    ) for (bucket, instrument), item in sorted(buckets.items())]


def _book_state(row: dict) -> tuple[int, int, float, float, float, float] | None:
    level = (row.get("levels") or [{}])[0]
    try:
        ts_ns = _ts_ns(row["hd"]["ts_event"])
        instrument = int(row["hd"]["instrument_id"])
        bid_px, ask_px = _px(level["bid_px"]), _px(level["ask_px"])
        bid_sz = float(level.get("bid_sz") or 0)
        ask_sz = float(level.get("ask_sz") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if bid_px <= 0 or ask_px <= bid_px:
        return None
    return ts_ns, instrument, bid_px, ask_px, bid_sz, ask_sz


def _queue_components(previous: tuple, current: tuple) -> tuple[float, float, float, float]:
    _, _, prev_bid, prev_ask, prev_bid_sz, prev_ask_sz = previous
    _, _, bid, ask, bid_sz, ask_sz = current
    if bid > prev_bid:
        bid_add, bid_remove = bid_sz, 0.0
    elif bid < prev_bid:
        bid_add, bid_remove = 0.0, prev_bid_sz
    else:
        delta = bid_sz - prev_bid_sz
        bid_add, bid_remove = max(delta, 0.0), max(-delta, 0.0)
    if ask < prev_ask:
        ask_add, ask_remove = ask_sz, 0.0
    elif ask > prev_ask:
        ask_add, ask_remove = 0.0, prev_ask_sz
    else:
        delta = ask_sz - prev_ask_sz
        ask_add, ask_remove = max(delta, 0.0), max(-delta, 0.0)
    return bid_add, bid_remove, ask_add, ask_remove


def _new_mbp_bucket(mid: float) -> dict:
    return {
        "events": 0, "trades": 0, "buy": 0.0, "sell": 0.0,
        "open": mid, "high": mid, "low": mid, "close": mid,
        "ofi": 0.0, "bqa": 0.0, "bqr": 0.0, "aqa": 0.0, "aqr": 0.0,
        "add_bid": 0.0, "add_ask": 0.0, "cancel_bid": 0.0,
        "cancel_ask": 0.0, "modify_bid": 0.0, "modify_ask": 0.0,
        "depth": 0.0, "queue": 0.0, "spread": 0.0, "micro": 0.0,
        "states": 0, "queue_states": 0,
    }


def _mbp_rows(rows: Iterable[dict], day: date) -> list[tuple]:
    buckets: dict[tuple[int, int], dict] = {}
    previous: tuple | None = None
    for row in rows:
        if row.get("action") == "R":
            previous = None
            continue
        current = _book_state(row)
        if current is None:
            continue
        ts_ns, instrument, bid, ask, bid_sz, ask_sz = current
        if previous is not None and (instrument != previous[1] or ts_ns < previous[0]):
            previous = None
        bucket = ts_ns - ts_ns % 1_000_000_000
        mid = (bid + ask) / 2
        item = buckets.setdefault((bucket, instrument), _new_mbp_bucket(mid))
        item["events"] += 1
        item["high"] = max(item["high"], mid)
        item["low"] = min(item["low"], mid)
        item["close"] = mid
        item["depth"] += (bid_sz + ask_sz) / 2
        item["spread"] += ask - bid
        total_depth = bid_sz + ask_sz
        if total_depth:
            item["queue"] += (bid_sz - ask_sz) / total_depth
            item["micro"] += ((ask * bid_sz + bid * ask_sz) / total_depth) - mid
            item["queue_states"] += 1
        item["states"] += 1
        if previous is not None:
            bqa, bqr, aqa, aqr = _queue_components(previous, current)
            item["bqa"] += bqa
            item["bqr"] += bqr
            item["aqa"] += aqa
            item["aqr"] += aqr
            item["ofi"] += bqa - bqr - aqa + aqr
        action, side = row.get("action"), row.get("side")
        size = float(row.get("size") or 0)
        if action == "T":
            item["trades"] += 1
            if side == "B":
                item["buy"] += size
            elif side == "A":
                item["sell"] += size
        elif action == "A" and side in {"A", "B"}:
            item["add_ask" if side == "A" else "add_bid"] += size
        elif action == "C" and side in {"A", "B"}:
            item["cancel_ask" if side == "A" else "cancel_bid"] += size
        elif action == "M" and side in {"A", "B"}:
            item["modify_ask" if side == "A" else "modify_bid"] += size
        previous = current
    result = []
    for (bucket, instrument), item in sorted(buckets.items()):
        states = item["states"]
        queue_states = item["queue_states"]
        result.append((
            day.isoformat(), bucket, instrument, item["events"], item["trades"],
            item["buy"], item["sell"], item["open"], item["high"], item["low"],
            item["close"], item["ofi"], item["bqa"], item["bqr"], item["aqa"],
            item["aqr"], item["add_bid"], item["add_ask"], item["cancel_bid"],
            item["cancel_ask"], item["modify_bid"], item["modify_ask"],
            item["depth"] / states if states else None,
            item["queue"] / queue_states if queue_states else None,
            item["spread"] / states if states else None,
            item["micro"] / queue_states if queue_states else None,
        ))
    return result


INSERTS = {
    "ohlcv-1s": ("ohlcv_1s", _ohlcv_rows),
    "bbo-1s": ("bbo_1s", _bbo_rows),
    "trades": ("trades_1s", _trade_rows),
    "mbp-1": ("mbp1_1s", _mbp_rows),
}


def _insert_sql(table: str, row_length: int) -> str:
    return f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(row_length))})"


def collect_one(
    conn: sqlite3.Connection,
    client: requests.Session,
    *,
    key: str,
    schema: str,
    window: RequestWindow,
    api_key: str,
    timeout: tuple[int, int],
) -> tuple[int, int, str]:
    response = client.get(
        f"{API_ROOT}/timeseries.get_range",
        params=request_params(schema, window),
        auth=(api_key, ""),
        timeout=timeout,
        stream=True,
    )
    digest = hashlib.sha256()
    raw_count = 0

    def streamed_rows() -> Iterator[dict]:
        nonlocal raw_count
        for row, raw in _iter_json(response):
            digest.update(raw)
            digest.update(b"\n")
            raw_count += 1
            yield row

    try:
        table, transform = INSERTS[schema]
        derived = transform(streamed_rows(), window.session_date)
    finally:
        response.close()
    if not derived:
        raise CollectionRefusal(f"{schema} {window.session_date} produced no derived rows")
    with conn:
        conn.executemany(_insert_sql(table, len(derived[0])), derived)
        conn.execute(
            """
            UPDATE requests
            SET status = 'complete', raw_records = ?, derived_rows = ?,
                source_sha256 = ?, error = NULL, updated_at = ?
            WHERE request_key = ?
            """,
            (
                raw_count, len(derived), digest.hexdigest(),
                datetime.now(timezone.utc).isoformat(), key,
            ),
        )
    return raw_count, len(derived), digest.hexdigest()


def collect(
    *,
    db_path: Path,
    bar_path: Path,
    cash_calendar_path: Path,
    api_key: str,
    start: date,
    end: date,
    max_cost: float,
    free_reserve: int,
    phases: tuple[str, ...],
    max_sessions_per_phase: int | None = None,
    sleep_seconds: float = 0.0,
    reuse_completed_calendar: bool = False,
) -> dict:
    if not api_key:
        raise CollectionRefusal("DATABENTO_API_KEY is not set")
    if max_cost <= 0 or max_cost > DEFAULT_MAX_COST:
        raise CollectionRefusal(f"max_cost must be within (0, ${DEFAULT_MAX_COST:.2f}]")
    unknown = set(phases) - set(PHASES)
    if unknown:
        raise CollectionRefusal(f"unknown phases: {sorted(unknown)}")
    if reuse_completed_calendar:
        calendar_conn = connect(db_path)
        try:
            days = completed_session_dates(calendar_conn, start=start, end=end)
        finally:
            calendar_conn.close()
    else:
        # The futures archive proves NQ coverage, while QQQ 09:30 bars define
        # actual U.S. cash sessions. Their intersection excludes exchange
        # holidays on which NQ traded but the opening auction did not occur.
        futures_days = set(session_dates_from_bars(bar_path, start=start, end=end))
        cash_days = set(cash_session_dates_from_csv(cash_calendar_path, start=start, end=end))
        days = sorted(futures_days & cash_days)
    if not days:
        raise CollectionRefusal("cash/futures calendar intersection is empty")
    conn = connect(db_path)
    client = requests.Session()
    completed = 0
    stopped_reason = None
    consecutive_failures = 0
    try:
        for schema in phases:
            ordered = year_balanced_order(days) if schema == "mbp-1" else days
            phase_count = 0
            for day in ordered:
                if max_sessions_per_phase is not None and phase_count >= max_sessions_per_phase:
                    break
                key = _request_key(schema, day)
                status = _existing_status(conn, key)
                if status == "complete":
                    continue
                if status is not None and status != "retry_authorized":
                    # A prior reserved/failed request may already have been
                    # billed.  Never retry it silently.
                    continue
                if shutil.disk_usage(db_path.parent).free < free_reserve:
                    stopped_reason = "free-disk reserve reached"
                    raise CollectionRefusal(stopped_reason)
                window = request_window(day)
                estimate = estimate_cost(
                    client, schema=schema, window=window, api_key=api_key
                )
                try:
                    key = _reserve(conn, schema, window, estimate, max_cost)
                except CollectionRefusal as exc:
                    stopped_reason = str(exc)
                    return {
                        "sessions_available": len(days),
                        "completed_this_run": completed,
                        "reserved_cost": reserved_cost(conn),
                        "stopped_reason": stopped_reason,
                    }
                try:
                    raw_count, derived_count, _ = collect_one(
                        conn, client, key=key, schema=schema, window=window,
                        api_key=api_key, timeout=(10, 180),
                    )
                except Exception as exc:
                    conn.execute(
                        """
                        UPDATE requests SET status = 'failed', error = ?, updated_at = ?
                        WHERE request_key = ?
                        """,
                        (str(exc)[:1000], datetime.now(timezone.utc).isoformat(), key),
                    )
                    conn.commit()
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        stopped_reason = "three consecutive paid-data failures"
                        return {
                            "sessions_available": len(days),
                            "completed_this_run": completed,
                            "reserved_cost": reserved_cost(conn),
                            "stopped_reason": stopped_reason,
                        }
                    continue
                consecutive_failures = 0
                completed += 1
                phase_count += 1
                print(
                    f"COMPLETE schema={schema} day={day} raw={raw_count} "
                    f"derived={derived_count} reserved=${reserved_cost(conn):.6f}",
                    flush=True,
                )
                if sleep_seconds:
                    time.sleep(sleep_seconds)
    finally:
        client.close()
        conn.close()
    final_conn = connect(db_path)
    try:
        final_reserved_cost = reserved_cost(final_conn)
    finally:
        final_conn.close()
    return {
        "sessions_available": len(days),
        "completed_this_run": completed,
        "reserved_cost": final_reserved_cost,
        "stopped_reason": stopped_reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--cash-calendar", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 8, 17))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 14))
    parser.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST)
    parser.add_argument("--free-reserve-mib", type=int, default=512)
    parser.add_argument("--phases", default=",".join(PHASES))
    parser.add_argument("--max-sessions-per-phase", type=int)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument(
        "--reuse-completed-calendar",
        action="store_true",
        help="reuse the audited completed OHLCV session dates already in --db",
    )
    args = parser.parse_args()
    result = collect(
        db_path=args.db,
        bar_path=args.bars,
        cash_calendar_path=args.cash_calendar,
        api_key=os.environ.get("DATABENTO_API_KEY", ""),
        start=args.start,
        end=args.end,
        max_cost=args.max_cost,
        free_reserve=args.free_reserve_mib * 1024 * 1024,
        phases=tuple(value for value in args.phases.split(",") if value),
        max_sessions_per_phase=args.max_sessions_per_phase,
        sleep_seconds=args.sleep_seconds,
        reuse_completed_calendar=args.reuse_completed_calendar,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
