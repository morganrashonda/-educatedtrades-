"""Research-only executable BBO audit for the frozen NQ opening-gap fade.

This module deliberately has no production, broker, order, learning, or Tier 3
imports.  It downloads only pre-qualified 131-second BBO windows and refuses to
continue if the preflight cost or byte limits would be exceeded.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Iterable
from zoneinfo import ZoneInfo

import requests


ET = ZoneInfo("America/New_York")
DATASET = "GLBX.MDP3"
SYMBOL = "NQ.v.0"
SCHEMA = "bbo-1s"
THRESHOLD_PCT = 1.278097837
MAX_ESTIMATED_COST_USD = 0.25
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
PER_SESSION_COST_CEILING_USD = 0.0025
POINT_VALUE_NQ = 20.0
POINT_VALUE_MNQ = 2.0
PRIMARY_EXTRA_SLIPPAGE_POINTS = 1.0
PRIMARY_COMMISSION_USD = 5.0
PRIMARY_COST_POINTS = PRIMARY_EXTRA_SLIPPAGE_POINTS + PRIMARY_COMMISSION_USD / POINT_VALUE_NQ
STOP_TARGET_GRID = (4.0, 8.0, 12.0, 16.0, 24.0, 32.0, 48.0)
ENTRY_HMS = (9, 30, 1)
DELAY_5_HMS = (9, 30, 6)
DELAY_10_HMS = (9, 30, 11)
EXIT_HMS = (9, 32, 1)
DOWNLOAD_START_HMS = (9, 29, 55)
DOWNLOAD_END_HMS = (9, 32, 6)
API_ROOT = "https://hist.databento.com/v0"


class AuditRefusal(ValueError):
    """A session cannot satisfy the frozen executable-price contract."""


class BudgetRefusal(RuntimeError):
    """A request would violate a frozen spend or storage cap."""


@dataclass(frozen=True)
class SignalSession:
    day: date
    gap_pct: float
    direction: int
    mechanism: str = "unknown"
    expected_instrument_id: int | None = None


@dataclass(frozen=True)
class Quote:
    ts_recv: datetime
    instrument_id: int
    bid: float
    ask: float


@dataclass
class ExecutableSession:
    signal: SignalSession
    instrument_id: int
    entry: Quote
    delayed_5: Quote
    delayed_10: Quote
    exit: Quote
    path: list[Quote]
    gross_points: float
    delayed_5_points: float
    delayed_10_points: float
    mae_points: float
    mfe_points: float
    source_sha256: str


def _at(day: date, hms: tuple[int, int, int]) -> datetime:
    return datetime.combine(day, time(*hms), ET)


def request_window(day: date) -> tuple[datetime, datetime]:
    return (
        _at(day, DOWNLOAD_START_HMS).astimezone(timezone.utc),
        _at(day, DOWNLOAD_END_HMS).astimezone(timezone.utc),
    )


def request_params(day: date) -> dict[str, str]:
    start, end = request_window(day)
    return {
        "dataset": DATASET,
        "symbols": SYMBOL,
        "stype_in": "continuous",
        "schema": SCHEMA,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "encoding": "json",
        "pretty_px": "true",
        "pretty_ts": "true",
        "map_symbols": "true",
    }


def cost_request_params(day: date) -> dict[str, str]:
    """Metadata endpoints accept the dataset query, not output-format flags."""
    params = request_params(day)
    return {
        key: params[key]
        for key in ("dataset", "symbols", "stype_in", "schema", "start", "end")
    }


def _signal_instrument_ids(nq_bars: Path, days: set[date]) -> dict[date, int]:
    """Verify the frozen prior-close/current-09:28 contract identity."""
    found: dict[date, int] = {}
    last_reference_id: int | None = None
    with nq_bars.open() as fh:
        for line in fh:
            row = json.loads(line)
            ts = datetime.fromtimestamp(int(row["hd"]["ts_event"]) / 1e9, timezone.utc).astimezone(ET)
            hm = (ts.hour, ts.minute)
            instrument_id = int(row["hd"]["instrument_id"])
            if hm == (15, 59):
                last_reference_id = instrument_id
            elif hm == (9, 28) and ts.date() in days:
                if ts.date() in found:
                    raise AuditRefusal(f"duplicate 09:28 signal bar for {ts.date()}")
                if last_reference_id is None:
                    raise AuditRefusal(f"no prior 15:59 reference instrument for {ts.date()}")
                if instrument_id != last_reference_id:
                    raise AuditRefusal(f"roll transition contaminates signal date {ts.date()}")
                found[ts.date()] = instrument_id
    missing = days - set(found)
    if missing:
        raise AuditRefusal(f"missing 09:28 signal bars for {len(missing)} qualifying dates")
    return found


def load_signals(
    session_csv: Path, fair_value_csv: Path | None = None, nq_bars: Path | None = None,
) -> list[SignalSession]:
    mechanism: dict[str, str] = {}
    if fair_value_csv and fair_value_csv.exists():
        with fair_value_csv.open(newline="") as fh:
            for row in csv.DictReader(fh):
                total = abs(float(row["absolute_gap_pct"]))
                move = abs(float(row["segment_0830"]))
                mechanism[row["day"]] = (
                    "information_created" if total > 0 and move >= 0.10 * total else "pre_existing"
                )
    found: dict[date, SignalSession] = {}
    with session_csv.open(newline="") as fh:
        for row in csv.DictReader(fh):
            gap = float(row["nq_overnight_ret"])
            if not math.isfinite(gap) or abs(gap) <= THRESHOLD_PCT:
                continue
            day = date.fromisoformat(row["day"])
            if day in found:
                raise ValueError(f"duplicate signal date: {day}")
            found[day] = SignalSession(day, gap, -1 if gap > 0 else 1, mechanism.get(str(day), "unknown"))
    signals = [found[day] for day in sorted(found)]
    if nq_bars:
        instrument_ids = _signal_instrument_ids(nq_bars, set(found))
        signals = [replace(signal, expected_instrument_id=instrument_ids[signal.day]) for signal in signals]
    return signals


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AuditRefusal("ts_recv is timezone-naive")
    return parsed.astimezone(ET)


def _quote(row: dict) -> Quote:
    try:
        level = row["levels"][0]
        quote = Quote(
            _parse_ts(row["ts_recv"]),
            int(row["hd"]["instrument_id"]),
            float(level["bid_px"]),
            float(level["ask_px"]),
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AuditRefusal(f"malformed BBO record: {exc}") from exc
    if not all(math.isfinite(x) and x > 0 for x in (quote.bid, quote.ask)):
        raise AuditRefusal("non-finite or non-positive BBO")
    if quote.bid >= quote.ask:
        raise AuditRefusal("locked or crossed BBO")
    return quote


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_quotes(path: Path, expected_day: date) -> tuple[list[Quote], str]:
    quotes: list[Quote] = []
    seen: set[datetime] = set()
    with path.open() as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditRefusal(f"invalid JSON on line {line_no}") from exc
            # Symbol-mapping/control records have no levels and are irrelevant.
            if not row.get("levels"):
                continue
            quote = _quote(row)
            if quote.ts_recv.date() != expected_day:
                raise AuditRefusal("BBO record belongs to a different ET session")
            if quote.ts_recv in seen:
                raise AuditRefusal(f"duplicate ts_recv: {quote.ts_recv.isoformat()}")
            seen.add(quote.ts_recv)
            quotes.append(quote)
    quotes.sort(key=lambda item: item.ts_recv)
    if not quotes:
        raise AuditRefusal("no BBO records")
    return quotes, _sha256(path)


def _mark(quotes: list[Quote], expected: datetime) -> Quote:
    matches = [quote for quote in quotes if quote.ts_recv == expected]
    if len(matches) != 1:
        raise AuditRefusal(f"expected exactly one BBO at {expected.isoformat()}, found {len(matches)}")
    return matches[0]


def _entry_price(direction: int, quote: Quote) -> float:
    return quote.ask if direction > 0 else quote.bid


def _exit_price(direction: int, quote: Quote) -> float:
    return quote.bid if direction > 0 else quote.ask


def _points(direction: int, entry: Quote, exit_quote: Quote) -> float:
    return direction * (_exit_price(direction, exit_quote) - _entry_price(direction, entry))


def evaluate_session(signal: SignalSession, path: Path) -> ExecutableSession:
    quotes, digest = load_quotes(path, signal.day)
    entry = _mark(quotes, _at(signal.day, ENTRY_HMS))
    delayed_5 = _mark(quotes, _at(signal.day, DELAY_5_HMS))
    delayed_10 = _mark(quotes, _at(signal.day, DELAY_10_HMS))
    exit_quote = _mark(quotes, _at(signal.day, EXIT_HMS))
    marks = (entry, delayed_5, delayed_10, exit_quote)
    instrument_ids = {quote.instrument_id for quote in marks}
    if len(instrument_ids) != 1:
        raise AuditRefusal("required BBO marks use different instrument IDs")
    instrument_id = entry.instrument_id
    if signal.expected_instrument_id is not None and instrument_id != signal.expected_instrument_id:
        raise AuditRefusal("BBO instrument ID does not match the frozen 09:28 signal contract")
    window_path = [quote for quote in quotes if entry.ts_recv <= quote.ts_recv <= exit_quote.ts_recv]
    if any(quote.instrument_id != instrument_id for quote in window_path):
        raise AuditRefusal("executable BBO path changes instrument ID")
    executable_path = window_path
    if not executable_path or executable_path[0].ts_recv != entry.ts_recv or executable_path[-1].ts_recv != exit_quote.ts_recv:
        raise AuditRefusal("incomplete executable BBO path")
    path_points = [_points(signal.direction, entry, quote) for quote in executable_path]
    return ExecutableSession(
        signal=signal,
        instrument_id=instrument_id,
        entry=entry,
        delayed_5=delayed_5,
        delayed_10=delayed_10,
        exit=exit_quote,
        path=executable_path,
        gross_points=_points(signal.direction, entry, exit_quote),
        delayed_5_points=_points(signal.direction, delayed_5, exit_quote),
        delayed_10_points=_points(signal.direction, delayed_10, exit_quote),
        mae_points=min(path_points),
        mfe_points=max(path_points),
        source_sha256=digest,
    )


def bootstrap_mean_ci(values: Iterable[float], trials: int = 10_000, seed: int = 260818) -> list[float] | None:
    values = list(values)
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    samples = sorted(mean(values[rng.randrange(n)] for _ in range(n)) for _ in range(trials))
    return [samples[int(0.025 * trials)], samples[min(trials - 1, int(0.975 * trials))]]


def _max_drawdown(values: list[float]) -> float:
    running = peak = 0.0
    worst = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


def metrics(values: Iterable[float]) -> dict:
    values = list(values)
    if not values:
        return {"n": 0}
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    loss_total = abs(sum(losses))
    return {
        "n": len(values),
        "wins": len(wins),
        "win_rate": len(wins) / len(values),
        "mean_points": mean(values),
        "median_points": median(values),
        "total_points": sum(values),
        "bootstrap_mean_95": bootstrap_mean_ci(values),
        "profit_factor": sum(wins) / loss_total if loss_total else None,
        "max_drawdown_points": _max_drawdown(values),
        "best_points": max(values),
        "worst_points": min(values),
    }


def _thirds(items: list) -> list[list]:
    n = len(items)
    cuts = (0, n // 3, 2 * n // 3, n)
    return [items[cuts[i]:cuts[i + 1]] for i in range(3)]


def bracket_points(session: ExecutableSession, stop: float, target: float) -> float:
    for quote in session.path[1:]:
        current = _points(session.signal.direction, session.entry, quote)
        # Conservative ordering: a stop is resolved before a target.
        if current <= -stop:
            return current
        if current >= target:
            return current
    return session.gross_points


def select_bracket(development: list[ExecutableSession]) -> dict:
    eligible: list[dict] = []
    all_trials: list[dict] = []
    for stop in STOP_TARGET_GRID:
        for target in STOP_TARGET_GRID:
            net = [bracket_points(session, stop, target) - PRIMARY_COST_POINTS for session in development]
            summary = metrics(net)
            ci = summary.get("bootstrap_mean_95")
            row = {"stop_points": stop, "target_points": target, "metrics": summary}
            all_trials.append(row)
            if (
                summary.get("mean_points", 0) > 0
                and summary.get("median_points", 0) > 0
                and ci and ci[0] > 0
            ):
                eligible.append(row)
    if not eligible:
        return {"selection": "NO_BRACKET_SELECTED", "trials_disclosed": 49, "trials": all_trials}
    selected = max(
        eligible,
        key=lambda row: (
            row["metrics"]["bootstrap_mean_95"][0],
            row["metrics"]["mean_points"],
            -row["metrics"]["max_drawdown_points"],
            row["stop_points"],
            row["target_points"],
        ),
    )
    return {"selection": selected, "trials_disclosed": 49, "trials": all_trials}


def _cost_scenarios(sessions: list[ExecutableSession]) -> dict:
    scenarios = {}
    for slippage in (0.0, 0.5, 1.0, 2.0, 3.0):
        for commission in (0.0, 2.5, 5.0, 7.5):
            nq_cost = slippage + commission / POINT_VALUE_NQ
            mnq_cost = slippage + commission / POINT_VALUE_MNQ
            key = f"slippage_{slippage:g}_commission_{commission:g}"
            scenarios[key] = {
                "nq": metrics(session.gross_points - nq_cost for session in sessions),
                "mnq": metrics(session.gross_points - mnq_cost for session in sessions),
            }
    return scenarios


def analyze(signals: list[SignalSession], raw_dir: Path) -> dict:
    valid: list[ExecutableSession] = []
    refusals: list[dict] = []
    for signal in signals:
        path = raw_dir / f"nq_bbo_1s_{signal.day}.jsonl"
        if not path.exists():
            refusals.append({"day": str(signal.day), "reason": "source file missing"})
            continue
        try:
            valid.append(evaluate_session(signal, path))
        except AuditRefusal as exc:
            refusals.append({"day": str(signal.day), "reason": str(exc)})
    valid.sort(key=lambda item: item.signal.day)
    primary_net = [session.gross_points - PRIMARY_COST_POINTS for session in valid]
    primary = metrics(primary_net)
    thirds = _thirds(valid)
    third_metrics = [metrics(session.gross_points - PRIMARY_COST_POINTS for session in group) for group in thirds]
    delayed_5 = metrics(session.delayed_5_points - PRIMARY_COST_POINTS for session in valid)
    delayed_10 = metrics(session.delayed_10_points - PRIMARY_COST_POINTS for session in valid)
    if len(primary_net) > 1:
        best_index = max(range(len(primary_net)), key=primary_net.__getitem__)
        delete_best = metrics(value for index, value in enumerate(primary_net) if index != best_index)
    else:
        delete_best = {"n": 0}
    ci = primary.get("bootstrap_mean_95")
    pf = primary.get("profit_factor")
    gates = {
        "at_least_50": len(valid) >= 50,
        "positive_mean": primary.get("mean_points", 0) > 0,
        "positive_median": primary.get("median_points", 0) > 0,
        "bootstrap_lower_positive": bool(ci and ci[0] > 0),
        "profit_factor_above_1_10": bool(pf is not None and pf > 1.10),
        "positive_after_best_deleted": delete_best.get("mean_points", 0) > 0,
        "positive_every_chronological_third": all(row.get("mean_points", 0) > 0 for row in third_metrics),
        "positive_delayed_5": delayed_5.get("mean_points", 0) > 0,
        "positive_delayed_10": delayed_10.get("mean_points", 0) > 0,
    }
    bracket = select_bracket(thirds[0]) if thirds and thirds[0] else {"selection": "NO_BRACKET_SELECTED"}
    selected = bracket.get("selection")
    if isinstance(selected, dict):
        stop, target = selected["stop_points"], selected["target_points"]
        bracket["later_two_thirds"] = metrics(
            bracket_points(session, stop, target) - PRIMARY_COST_POINTS
            for group in thirds[1:] for session in group
        )
    mechanism = {}
    for label in ("pre_existing", "information_created", "unknown"):
        group = [session.gross_points - PRIMARY_COST_POINTS for session in valid if session.signal.mechanism == label]
        if group:
            mechanism[label] = {
                "status": "MEASURED" if len(group) >= 15 else "INSUFFICIENT_MECHANISM_SAMPLE",
                "metrics": metrics(group),
            }
    return {
        "audit": "NQ opening-gap executable BBO audit",
        "research_only": True,
        "execution_authorized": False,
        "signal": {"threshold_pct_strictly_greater_than": THRESHOLD_PCT, "direction": "fade"},
        "data": {"qualifying_sessions": len(signals), "valid_sessions": len(valid), "refusals": refusals},
        "primary_cost_contract": {
            "spread": "embedded by side-correct BBO crossing",
            "extra_round_trip_slippage_points": PRIMARY_EXTRA_SLIPPAGE_POINTS,
            "commission_usd_nq": PRIMARY_COMMISSION_USD,
            "total_additional_points_nq": PRIMARY_COST_POINTS,
        },
        "primary": primary,
        "delete_best_session": delete_best,
        "chronological_thirds": third_metrics,
        "delayed_entry_5_seconds": delayed_5,
        "delayed_entry_10_seconds": delayed_10,
        "gates": gates,
        "status": "EXECUTION_AUDIT_PASS" if all(gates.values()) else "EXECUTION_AUDIT_FAIL",
        "cost_scenarios": _cost_scenarios(valid),
        "mae_points": metrics(session.mae_points for session in valid),
        "mfe_points": metrics(session.mfe_points for session in valid),
        "mechanism_attribution": mechanism,
        "stop_target_development": bracket,
        "sessions": [
            {
                "day": str(session.signal.day),
                "gap_pct": session.signal.gap_pct,
                "direction": session.signal.direction,
                "instrument_id": session.instrument_id,
                "expected_signal_instrument_id": session.signal.expected_instrument_id,
                "gross_points": session.gross_points,
                "primary_net_points": session.gross_points - PRIMARY_COST_POINTS,
                "delayed_5_points": session.delayed_5_points,
                "delayed_10_points": session.delayed_10_points,
                "mae_points": session.mae_points,
                "mfe_points": session.mfe_points,
                "mechanism": session.signal.mechanism,
                "source_sha256": session.source_sha256,
            }
            for session in valid
        ],
    }


def _extract_cost(response: requests.Response) -> float:
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, dict):
        for key in ("cost_usd", "cost"):
            if key in payload:
                return float(payload[key])
    raise BudgetRefusal("metadata cost response did not contain a numeric cost")


def download(
    signals: list[SignalSession], raw_dir: Path, key: str,
    max_cost: float = MAX_ESTIMATED_COST_USD,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    session: requests.Session | None = None,
) -> dict:
    if not key:
        raise RuntimeError("DATABENTO_API_KEY is not set")
    raw_dir.mkdir(parents=True, exist_ok=True)
    client = session or requests.Session()
    estimated_cost = 0.0
    downloaded_bytes = 0
    reused_bytes = 0
    files: list[dict] = []
    failures: list[dict] = []
    for signal in signals:
        target = raw_dir / f"nq_bbo_1s_{signal.day}.jsonl"
        if target.exists():
            size = target.stat().st_size
            if size > max_bytes:
                raise BudgetRefusal("an existing session file exceeds the byte cap")
            # Validate before treating an existing file as resumable provenance.
            load_quotes(target, signal.day)
            reused_bytes += size
            files.append({"day": str(signal.day), "status": "reused", "bytes": size, "sha256": _sha256(target)})
            continue
        params = request_params(signal.day)
        try:
            estimate_response = client.get(
                f"{API_ROOT}/metadata.get_cost",
                params=cost_request_params(signal.day),
                auth=(key, ""), timeout=(10, 30),
            )
            session_cost = _extract_cost(estimate_response)
        except (requests.RequestException, ValueError, BudgetRefusal) as exc:
            failures.append({"day": str(signal.day), "stage": "cost_preflight", "reason": str(exc)})
            continue
        if not math.isfinite(session_cost) or session_cost < 0 or session_cost > PER_SESSION_COST_CEILING_USD:
            raise BudgetRefusal(f"{signal.day} estimate ${session_cost:.6f} exceeds per-session ceiling")
        if estimated_cost + session_cost > max_cost:
            raise BudgetRefusal(
                f"next request would exceed cost cap: ${estimated_cost + session_cost:.6f} > ${max_cost:.2f}"
            )
        # A data request may be billable even when transport or validation
        # fails, so reserve its full estimate before issuing it.
        estimated_cost += session_cost
        try:
            response = client.get(
                f"{API_ROOT}/timeseries.get_range",
                params=params,
                auth=(key, ""),
                timeout=(10, 60),
                stream=True,
            )
        except requests.RequestException as exc:
            failures.append({"day": str(signal.day), "stage": "download", "reason": str(exc)})
            continue
        try:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length and downloaded_bytes + int(content_length) > max_bytes:
                raise BudgetRefusal("declared response size would exceed byte cap")
            part = target.with_suffix(target.suffix + ".part")
            size = 0
            digest = hashlib.sha256()
            with part.open("wb") as fh:
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    if downloaded_bytes + size + len(chunk) > max_bytes:
                        raise BudgetRefusal("streamed response would exceed byte cap")
                    fh.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if size == 0:
                part.unlink(missing_ok=True)
                raise AuditRefusal("Databento returned an empty BBO response")
            # Enforce all exact marks and instrument continuity before making
            # the billed response durable or eligible for reuse.
            evaluate_session(signal, part)
            part.replace(target)
            downloaded_bytes += size
            files.append({
                "day": str(signal.day), "status": "downloaded", "bytes": size,
                "estimated_cost_usd": session_cost, "sha256": digest.hexdigest(),
            })
        except BudgetRefusal:
            target.with_suffix(target.suffix + ".part").unlink(missing_ok=True)
            raise
        except (requests.RequestException, AuditRefusal, ValueError) as exc:
            target.with_suffix(target.suffix + ".part").unlink(missing_ok=True)
            failures.append({"day": str(signal.day), "stage": "download_validation", "reason": str(exc)})
        finally:
            response.close()
    return {
        "research_only": True,
        "qualifying_sessions": len(signals),
        "downloaded_sessions": sum(row["status"] == "downloaded" for row in files),
        "reused_sessions": sum(row["status"] == "reused" for row in files),
        "estimated_new_cost_usd": estimated_cost,
        "downloaded_bytes": downloaded_bytes,
        "reused_bytes": reused_bytes,
        "cost_cap_usd": max_cost,
        "byte_cap": max_bytes,
        "files": files,
        "failures": failures,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--fair-value", type=Path)
    parser.add_argument("--nq-bars", type=Path, help="original NQ 1-minute source for contract-ID verification")
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--download", action="store_true", help="perform capped Databento downloads")
    args = parser.parse_args()
    signals = load_signals(args.sessions, args.fair_value, args.nq_bars)
    if args.download:
        if args.manifest.exists():
            raise BudgetRefusal(
                f"manifest already exists at {args.manifest}; refusing to overwrite cost provenance"
            )
        manifest = download(signals, args.raw_dir, os.environ.get("DATABENTO_API_KEY", ""))
        _write_json(args.manifest, manifest)
    report = analyze(signals, args.raw_dir)
    _write_json(args.report, report)
    print(json.dumps({
        "status": report["status"],
        "qualifying_sessions": report["data"]["qualifying_sessions"],
        "valid_sessions": report["data"]["valid_sessions"],
        "refusals": len(report["data"]["refusals"]),
        "execution_authorized": report["execution_authorized"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
