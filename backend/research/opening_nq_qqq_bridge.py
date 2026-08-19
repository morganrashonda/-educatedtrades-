"""Research-only NQ-signal to QQQ executable-quote bridge audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from backend.research.opening_executable_bbo import (
    AuditRefusal,
    BudgetRefusal,
    DELAY_10_HMS,
    DELAY_5_HMS,
    ENTRY_HMS,
    ET,
    EXIT_HMS,
    SignalSession,
    load_signals,
    metrics,
)


SYMBOL = "QQQ"
FEED = "sip"
PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE = 0.02
SLIPPAGE_SCENARIOS = (0.0, 0.01, 0.02, 0.05, 0.10)
MARKS = {
    "entry": ENTRY_HMS,
    "delayed_5": DELAY_5_HMS,
    "delayed_10": DELAY_10_HMS,
    "exit": EXIT_HMS,
}
MAX_MARK_DELAY_SECONDS = 2.0
HISTORICAL_QUOTES_URL = "https://data.alpaca.markets/v2/stocks/quotes"
REQUESTS_PER_MINUTE = 180


@dataclass(frozen=True)
class EquityQuote:
    ts: datetime
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    bid_exchange: str
    ask_exchange: str
    source_timestamp: str


@dataclass(frozen=True)
class QQQSession:
    signal: SignalSession
    entry: EquityQuote
    delayed_5: EquityQuote
    delayed_10: EquityQuote
    exit: EquityQuote
    gross_per_share: float
    delayed_5_per_share: float
    delayed_10_per_share: float
    source_sha256: str


def _at(day: date, hms: tuple[int, int, int]) -> datetime:
    return datetime(day.year, day.month, day.day, *hms, tzinfo=ET)


def mark_request_params(day: date, hms: tuple[int, int, int]) -> dict[str, str | int]:
    start = _at(day, hms).astimezone(timezone.utc)
    end = start + timedelta(seconds=MAX_MARK_DELAY_SECONDS)
    return {
        "symbols": SYMBOL,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "feed": FEED,
        "sort": "asc",
        "limit": 1,
    }


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AuditRefusal("quote timestamp is timezone-naive")
    return parsed.astimezone(ET)


def parse_quote(row: dict, nominal: datetime) -> EquityQuote:
    try:
        quote = EquityQuote(
            ts=_parse_timestamp(row["t"]),
            bid=float(row["bp"]),
            ask=float(row["ap"]),
            bid_size=float(row["bs"]),
            ask_size=float(row["as"]),
            bid_exchange=str(row.get("bx", "")),
            ask_exchange=str(row.get("ax", "")),
            source_timestamp=str(row["t"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditRefusal(f"malformed QQQ SIP quote: {exc}") from exc
    numeric = (quote.bid, quote.ask, quote.bid_size, quote.ask_size)
    if not all(math.isfinite(value) and value > 0 for value in numeric):
        raise AuditRefusal("QQQ quote has non-positive or non-finite price/size")
    if quote.bid >= quote.ask:
        raise AuditRefusal("QQQ quote is locked or crossed")
    delay = (quote.ts - nominal).total_seconds()
    if delay < 0 or delay > MAX_MARK_DELAY_SECONDS:
        raise AuditRefusal(f"QQQ quote delay {delay:.6f}s violates frozen mark window")
    return quote


def selected_quote(payload: dict, nominal: datetime) -> EquityQuote:
    try:
        rows = payload["quotes"][SYMBOL]
    except (KeyError, TypeError) as exc:
        raise AuditRefusal("Alpaca response has no QQQ quote collection") from exc
    if len(rows) != 1:
        raise AuditRefusal(f"expected one first QQQ quote, found {len(rows)}")
    return parse_quote(rows[0], nominal)


def first_valid_quote(payload: dict, nominal: datetime) -> tuple[EquityQuote | None, str | None]:
    """Return the earliest valid row on a page and the token for later rows."""
    try:
        rows = payload["quotes"][SYMBOL]
    except (KeyError, TypeError) as exc:
        raise AuditRefusal("Alpaca response has no QQQ quote collection") from exc
    if not isinstance(rows, list):
        raise AuditRefusal("Alpaca QQQ quote collection is not a list")
    last_error: AuditRefusal | None = None
    for row in rows:
        try:
            return parse_quote(row, nominal), payload.get("next_page_token")
        except AuditRefusal as exc:
            last_error = exc
            # Timestamps are ascending; once the window is exceeded no later
            # quote can satisfy the frozen mark.
            if "violates frozen mark window" in str(exc):
                break
    if last_error and not payload.get("next_page_token"):
        raise last_error
    return None, payload.get("next_page_token")


def _quote_dict(quote: EquityQuote) -> dict:
    return {
        "ts": quote.source_timestamp,
        "bid": quote.bid,
        "ask": quote.ask,
        "bid_size": quote.bid_size,
        "ask_size": quote.ask_size,
        "bid_exchange": quote.bid_exchange,
        "ask_exchange": quote.ask_exchange,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry_price(direction: int, quote: EquityQuote) -> float:
    return quote.ask if direction > 0 else quote.bid


def _exit_price(direction: int, quote: EquityQuote) -> float:
    return quote.bid if direction > 0 else quote.ask


def _pnl(direction: int, entry: EquityQuote, exit_quote: EquityQuote) -> float:
    return direction * (_exit_price(direction, exit_quote) - _entry_price(direction, entry))


def load_session(signal: SignalSession, path: Path) -> QQQSession:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditRefusal(f"unreadable QQQ mark file: {exc}") from exc
    if payload.get("day") != str(signal.day):
        raise AuditRefusal("QQQ mark file belongs to a different session")
    if payload.get("symbol") != SYMBOL or payload.get("feed") != FEED:
        raise AuditRefusal("QQQ mark file has wrong symbol or feed")
    quotes = {}
    for name, hms in MARKS.items():
        try:
            row = payload["marks"][name]
        except (KeyError, TypeError) as exc:
            raise AuditRefusal(f"QQQ mark file is missing {name}") from exc
        normalized = {
            "t": row["ts"], "bp": row["bid"], "ap": row["ask"],
            "bs": row["bid_size"], "as": row["ask_size"],
            "bx": row.get("bid_exchange", ""), "ax": row.get("ask_exchange", ""),
        }
        quotes[name] = parse_quote(normalized, _at(signal.day, hms))
    entry, exit_quote = quotes["entry"], quotes["exit"]
    return QQQSession(
        signal, entry, quotes["delayed_5"], quotes["delayed_10"], exit_quote,
        _pnl(signal.direction, entry, exit_quote),
        _pnl(signal.direction, quotes["delayed_5"], exit_quote),
        _pnl(signal.direction, quotes["delayed_10"], exit_quote),
        _sha256(path),
    )


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(path)


class RateLimiter:
    def __init__(
        self, requests_per_minute: int = REQUESTS_PER_MINUTE,
        clock: Callable[[], float] = time_module.monotonic,
        sleeper: Callable[[float], None] = time_module.sleep,
    ):
        self.minimum_interval = 60.0 / requests_per_minute
        self.clock = clock
        self.sleeper = sleeper
        self.last_request: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self.last_request is not None:
            remaining = self.minimum_interval - (now - self.last_request)
            if remaining > 0:
                self.sleeper(remaining)
        self.last_request = self.clock()


def _request_first_quote(
    client: requests.Session, headers: dict[str, str], day: date,
    hms: tuple[int, int, int], limiter: RateLimiter,
) -> EquityQuote:
    nominal = _at(day, hms)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            params = mark_request_params(day, hms)
            for _page in range(20):
                limiter.wait()
                response = client.get(
                    HISTORICAL_QUOTES_URL,
                    headers=headers,
                    params=params,
                    timeout=(10, 30),
                )
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = min(float(response.headers.get("Retry-After", "1")), 10.0)
                    response.close()
                    time_module.sleep(retry_after)
                    raise requests.HTTPError(f"transient HTTP {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                response.close()
                quote, token = first_valid_quote(payload, nominal)
                if quote is not None:
                    return quote
                if not token:
                    raise AuditRefusal("no valid QQQ SIP quote in frozen mark window")
                params = dict(params)
                params["page_token"] = token
                params["limit"] = 100
            raise AuditRefusal("QQQ quote pagination exceeded safety bound")
        except (requests.RequestException, ValueError, AuditRefusal) as exc:
            last_error = exc
            if attempt < 2:
                time_module.sleep(1.0 + attempt)
    raise AuditRefusal(f"QQQ SIP request failed after retries: {last_error}")


def collect(
    signals: list[SignalSession], raw_dir: Path, key_id: str, secret_key: str,
    client: requests.Session | None = None, limiter: RateLimiter | None = None,
) -> dict:
    if not key_id or not secret_key:
        raise RuntimeError("Alpaca market-data credentials are not set")
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = client or requests.Session()
    limiter = limiter or RateLimiter()
    headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
    files = []
    refusals = []
    request_count = 0
    for signal in signals:
        path = raw_dir / f"qqq_sip_marks_{signal.day}.json"
        if path.exists():
            loaded = load_session(signal, path)
            files.append({"day": str(signal.day), "status": "reused", "sha256": loaded.source_sha256})
            continue
        marks = {}
        try:
            for name, hms in MARKS.items():
                quote = _request_first_quote(session, headers, signal.day, hms, limiter)
                request_count += 1
                marks[name] = _quote_dict(quote)
            payload = {
                "day": str(signal.day), "symbol": SYMBOL, "feed": FEED,
                "nq_gap_pct": signal.gap_pct, "direction": signal.direction,
                "selection": "first valid quote at/after mark, max delay 2s",
                "marks": marks,
            }
            _atomic_json(path, payload)
            loaded = load_session(signal, path)
            files.append({"day": str(signal.day), "status": "downloaded", "sha256": loaded.source_sha256})
        except AuditRefusal as exc:
            path.with_suffix(path.suffix + ".part").unlink(missing_ok=True)
            refusals.append({"day": str(signal.day), "reason": str(exc)})
    return {
        "research_only": True,
        "qualifying_sessions": len(signals),
        "complete_sessions": len(files),
        "downloaded_sessions": sum(row["status"] == "downloaded" for row in files),
        "reused_sessions": sum(row["status"] == "reused" for row in files),
        "request_count": request_count,
        "files": files,
        "refusals": refusals,
    }


def _thirds(items: list) -> list[list]:
    n = len(items)
    cuts = (0, n // 3, 2 * n // 3, n)
    return [items[cuts[index]:cuts[index + 1]] for index in range(3)]


def _delete_best(values: list[float]) -> dict:
    if len(values) < 2:
        return {"n": 0}
    best = max(range(len(values)), key=values.__getitem__)
    return metrics(value for index, value in enumerate(values) if index != best)


def analyze(signals: list[SignalSession], raw_dir: Path) -> dict:
    sessions = []
    refusals = []
    for signal in signals:
        path = raw_dir / f"qqq_sip_marks_{signal.day}.json"
        if not path.exists():
            refusals.append({"day": str(signal.day), "reason": "mark file missing"})
            continue
        try:
            sessions.append(load_session(signal, path))
        except AuditRefusal as exc:
            refusals.append({"day": str(signal.day), "reason": str(exc)})
    sessions.sort(key=lambda row: row.signal.day)
    primary_values = [row.gross_per_share - PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE for row in sessions]
    primary = metrics(primary_values)
    thirds = _thirds(sessions)
    third_metrics = [
        metrics(row.gross_per_share - PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE for row in group)
        for group in thirds
    ]
    delayed_5 = metrics(row.delayed_5_per_share - PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE for row in sessions)
    delayed_10 = metrics(row.delayed_10_per_share - PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE for row in sessions)
    delete_best = _delete_best(primary_values)
    ci = primary.get("bootstrap_mean_95")
    pf = primary.get("profit_factor")
    gates = {
        "at_least_50": len(sessions) >= 50,
        "positive_mean": primary.get("mean_points", 0) > 0,
        "positive_median": primary.get("median_points", 0) > 0,
        "bootstrap_lower_positive": bool(ci and ci[0] > 0),
        "profit_factor_above_1_10": bool(pf is not None and pf > 1.10),
        "positive_after_best_deleted": delete_best.get("mean_points", 0) > 0,
        "positive_every_chronological_third": all(row.get("mean_points", 0) > 0 for row in third_metrics),
        "positive_delayed_5": delayed_5.get("mean_points", 0) > 0,
        "positive_delayed_10": delayed_10.get("mean_points", 0) > 0,
    }
    scenarios = {
        f"additional_slippage_{int(cost * 100)}_cents_per_share": metrics(
            row.gross_per_share - cost for row in sessions
        )
        for cost in SLIPPAGE_SCENARIOS
    }
    return {
        "audit": "Frozen NQ signal executed through QQQ historical SIP quotes",
        "research_only": True,
        "execution_authorized": False,
        "data": {"qualifying_sessions": len(signals), "valid_sessions": len(sessions), "refusals": refusals},
        "primary_cost": {"embedded_spread": True, "additional_slippage_dollars_per_share": 0.02},
        "primary": primary,
        "delete_best_session": delete_best,
        "chronological_thirds": third_metrics,
        "delayed_entry_5_seconds": delayed_5,
        "delayed_entry_10_seconds": delayed_10,
        "gates": gates,
        "status": "QQQ_EXECUTION_BRIDGE_PASS" if all(gates.values()) else "QQQ_EXECUTION_BRIDGE_FAIL",
        "cost_scenarios": scenarios,
        "example_mean_dollars": {
            str(shares): primary.get("mean_points", 0) * shares for shares in (1, 10, 100)
        },
        "sessions": [
            {
                "day": str(row.signal.day), "nq_gap_pct": row.signal.gap_pct,
                "direction": row.signal.direction, "gross_per_share": row.gross_per_share,
                "primary_net_per_share": row.gross_per_share - PRIMARY_SLIPPAGE_DOLLARS_PER_SHARE,
                "delayed_5_per_share": row.delayed_5_per_share,
                "delayed_10_per_share": row.delayed_10_per_share,
                "entry_ts": row.entry.ts.isoformat(), "exit_ts": row.exit.ts.isoformat(),
                "source_sha256": row.source_sha256,
            }
            for row in sessions
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args()
    signals = load_signals(args.signals)
    if args.collect:
        if args.manifest.exists():
            raise BudgetRefusal(f"manifest already exists at {args.manifest}; refusing provenance overwrite")
        manifest = collect(
            signals, args.raw_dir,
            os.environ.get("APCA_API_KEY_ID", ""), os.environ.get("APCA_API_SECRET_KEY", ""),
        )
        _write_json(args.manifest, manifest)
    report = analyze(signals, args.raw_dir)
    _write_json(args.report, report)
    print(json.dumps({
        "status": report["status"], "valid_sessions": report["data"]["valid_sessions"],
        "refusals": len(report["data"]["refusals"]),
        "execution_authorized": report["execution_authorized"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
