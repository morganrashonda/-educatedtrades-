"""Read-only opening-behaviour study for QQQ and NQ.

This module deliberately has no imports from the production trading path.  It
normalizes vendor files, builds one record per cash session, and writes a
research report only.  It does not place orders, update the learning database,
or change strategy configuration.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median, pstdev
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
HORIZONS = (5, 15, 30, 60, 120)
WINDOWS = (5, 15, 30)


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _bar_ok(b: Bar) -> bool:
    return (
        min(b.open, b.close) >= b.low
        and max(b.open, b.close) <= b.high
        and min(b.open, b.high, b.low, b.close) > 0
    )


def load_qqq(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            bars.append(Bar(ts, *(float(row[k]) for k in ("open", "high", "low", "close")), float(row["volume"])))
    return bars


def load_nq(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            # Databento fixed-point prices are nanounits for this schema.
            scale = 1_000_000_000
            ts = datetime.fromtimestamp(int(row["hd"]["ts_event"]) / 1e9, timezone.utc)
            bars.append(Bar(ts, *(int(row[k]) / scale for k in ("open", "high", "low", "close")), float(row["volume"])))
    return bars


def _rth_days(bars: list[Bar]) -> tuple[dict, dict]:
    days: dict = defaultdict(list)
    invalid = 0
    timestamps = [bar.ts for bar in bars]
    non_monotonic = sum(b <= a for a, b in zip(timestamps, timestamps[1:]))
    duplicate_timestamps = len(timestamps) - len(set(timestamps))
    for bar in sorted(bars, key=lambda x: x.ts):
        if not _bar_ok(bar):
            invalid += 1
            continue
        local = bar.ts.astimezone(ET)
        if (local.hour, local.minute) >= (9, 30) and (local.hour, local.minute) < (16, 0):
            days[local.date()].append(bar)
    valid = {day: sorted(rows, key=lambda x: x.ts) for day, rows in days.items() if len(rows) in (210, 390)}
    quality = {
        "rth_days_seen": len(days),
        "valid_days": len(valid),
        "excluded_days": len(days) - len(valid),
        "bar_counts": {str(day): len(rows) for day, rows in sorted(days.items())},
        "invalid_ohlc_rows": invalid,
        "duplicate_timestamps": duplicate_timestamps,
        "non_monotonic_adjacent_timestamps": non_monotonic,
    }
    return valid, quality


def _overnight_by_cash_day(bars: list[Bar]) -> dict[date, dict]:
    """Assign Globex/evening and premarket bars to the next cash session."""
    grouped: dict[date, list[Bar]] = defaultdict(list)
    for bar in bars:
        local = bar.ts.astimezone(ET)
        if local.hour >= 18:
            grouped[local.date() + timedelta(days=1)].append(bar)
        elif (local.hour, local.minute) < (9, 30):
            grouped[local.date()].append(bar)
    result = {}
    for day, rows in grouped.items():
        if rows:
            result[day] = {"high": max(x.high for x in rows), "low": min(x.low for x in rows), "bars": len(rows)}
    return result


def _percentile_rank(value: float, values: list[float]) -> float:
    if not values:
        return 0.5
    return sum(x <= value for x in values) / len(values)


def _stats(values: list[float], costs: float) -> dict:
    net = [v - costs for v in values]
    if not net:
        return {"n": 0}
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    equity = peak = 0.0
    max_dd = 0.0
    for v in net:
        equity += v
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    rng = random.Random(20260817 + len(net))
    bootstrap = []
    if len(net) >= 2:
        for _ in range(1000):
            bootstrap.append(mean(rng.choice(net) for _ in net))
    bootstrap.sort()
    return {
        "n": len(net),
        "mean_net_return": mean(net),
        "median_net_return": median(net),
        "win_rate": len(wins) / len(net),
        "avg_winner": mean(wins) if wins else None,
        "avg_loser": mean(losses) if losses else None,
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "stdev": pstdev(net) if len(net) > 1 else 0.0,
        "max_drawdown_return_units": max_dd,
        "worst": min(net),
        "best": max(net),
        "bootstrap_mean_ci95": [bootstrap[25], bootstrap[974]] if bootstrap else None,
        "cost_stress": {
            str(bps): mean(v - bps / 100.0 for v in values)
            for bps in (0, 2, 5, 10)
        },
    }


def _stability(values: list[float]) -> dict:
    if len(values) < 2:
        return {"n": len(values), "leave_one_out_mean_min": None, "leave_one_out_mean_max": None}
    loo = [mean(values[:i] + values[i + 1:]) for i in range(len(values))]
    block = [mean(values[i:i + 20]) for i in range(0, len(values), 20)]
    return {
        "n": len(values),
        "leave_one_out_mean_min": min(loo),
        "leave_one_out_mean_max": max(loo),
        "rolling_20_day_block_means": block,
    }


def _mfe_mae(rows: list[Bar], index: int, horizon: int, direction: str) -> tuple[float, float] | None:
    end = min(len(rows), index + horizon + 1)
    if index >= end:
        return None
    entry = rows[index].close
    highs = [bar.high for bar in rows[index:end]]
    lows = [bar.low for bar in rows[index:end]]
    if direction == "long":
        return (max(highs) / entry - 1.0) * 100.0, (min(lows) / entry - 1.0) * 100.0
    return (entry / min(lows) - 1.0) * 100.0, (entry / max(highs) - 1.0) * 100.0


def _future_close(rows: list[Bar], index: int, horizon: int) -> float | None:
    target = index + horizon
    return rows[target].close if target < len(rows) else None


def analyze(instrument: str, bars: list[Bar], cost_bps: float = 2.0) -> dict:
    days, quality = _rth_days(bars)
    overnight = _overnight_by_cash_day(bars)
    ordered = sorted(days.items())
    prior_closes: list[float] = []
    records: list[dict] = []
    for day, rows in ordered:
        if len(rows) < 210:
            continue
        prev_close = prior_closes[-1] if prior_closes else None
        prior_closes.append(rows[-1].close)
        if prev_close is None:
            continue
        rec = {"day": str(day), "n_bars": len(rows), "open": rows[0].open, "prior_close": prev_close}
        rec["gap_pct"] = (rows[0].open / prev_close - 1.0) * 100.0
        for horizon in HORIZONS:
            if horizon <= len(rows):
                rec[f"open_ret_{horizon}m"] = (rows[horizon - 1].close / rows[0].open - 1.0) * 100.0
        if day in overnight:
            rec["overnight"] = overnight[day]
        for window in WINDOWS:
            if len(rows) < window:
                continue
            opening = rows[:window]
            close_index = window - 1
            hi = max(x.high for x in opening)
            lo = min(x.low for x in opening)
            oc = opening[-1].close
            rec[f"w{window}"] = {"high": hi, "low": lo, "close": oc, "range": hi - lo, "close_location": (oc - lo) / (hi - lo) if hi > lo else 0.5}
            if day in overnight:
                ov = overnight[day]
                rec[f"w{window}"]["overnight_high_test"] = max(x.high for x in opening) >= ov["high"]
                rec[f"w{window}"]["overnight_low_test"] = min(x.low for x in opening) <= ov["low"]
                rec[f"w{window}"]["overnight_reversal_long"] = rec[f"w{window}"]["overnight_low_test"] and oc > rows[0].open
                rec[f"w{window}"]["overnight_reversal_short"] = rec[f"w{window}"]["overnight_high_test"] and oc < rows[0].open
            # Signal state is frozen at the opening-window close.
            rec[f"w{window}"]["gap_continuation"] = abs(rec["gap_pct"]) >= 0.25 and ((oc - rows[0].open) * rec["gap_pct"] > 0)
            rec[f"w{window}"]["gap_fill_state"] = abs(rec["gap_pct"]) >= 0.25 and ((oc - prev_close) * rec["gap_pct"] < 0)
            rec[f"w{window}"]["long_bias"] = rec[f"w{window}"]["close_location"] >= 0.70
            rec[f"w{window}"]["short_bias"] = rec[f"w{window}"]["close_location"] <= 0.30
            for horizon in HORIZONS:
                future = _future_close(rows, close_index, horizon)
                if future is not None:
                    rec[f"w{window}"][f"ret_{horizon}m"] = (future / oc - 1.0) * 100.0
            # First break after the window; only closes outside the range count.
            direction = None
            break_index = None
            for idx in range(window, len(rows)):
                if rows[idx].close > hi:
                    direction, break_index = "long", idx
                    break
                if rows[idx].close < lo:
                    direction, break_index = "short", idx
                    break
            rec[f"w{window}"]["first_break"] = direction
            if break_index is not None:
                rec[f"w{window}"]["break_minute"] = break_index
                for horizon in HORIZONS:
                    future = _future_close(rows, break_index, horizon)
                    if future is not None:
                        signed = (future / rows[break_index].close - 1.0) * 100.0
                        rec[f"w{window}"][f"break_ret_{horizon}m"] = signed if direction == "long" else -signed
                # A failed breakout is defined only by a later close back inside
                # the frozen opening range; no intrabar assumption is made.
                reentry = next((idx for idx in range(break_index + 1, min(len(rows), break_index + 31)) if lo <= rows[idx].close <= hi), None)
                rec[f"w{window}"]["failed_breakout"] = reentry is not None
                rec[f"w{window}"]["failed_breakout_direction"] = direction if reentry is not None else None
                if reentry is not None:
                    rec[f"w{window}"]["reentry_minute"] = reentry
                    opposite = "short" if direction == "long" else "long"
                    for horizon in HORIZONS:
                        future = _future_close(rows, reentry, horizon)
                        if future is not None:
                            signed = (future / rows[reentry].close - 1.0) * 100.0
                            rec[f"w{window}"][f"failed_ret_{horizon}m"] = signed if opposite == "long" else -signed
                            excursion = _mfe_mae(rows, reentry, horizon, opposite)
                            if excursion:
                                rec[f"w{window}"][f"failed_mfe_{horizon}m"], rec[f"w{window}"][f"failed_mae_{horizon}m"] = excursion
        records.append(rec)

    # Fixed chronological split; no tuning is performed by this runner.
    n = len(records)
    d = max(1, n * 6 // 10)
    v = max(d + 1, n * 9 // 10)
    splits = {"discovery": records[:d], "validation": records[d:v], "confirmation": records[v:]}
    report = {"instrument": instrument, "quality": quality, "records": n, "splits": {}}
    cost_pct = cost_bps / 100.0
    for name, subset in splits.items():
        out = {"days": len(subset), "windows": {}}
        out["clock_windows_from_open"] = {
            f"{horizon}m": _stats([r[f"open_ret_{horizon}m"] for r in subset if f"open_ret_{horizon}m" in r], cost_pct)
            for horizon in HORIZONS
        }
        for window in WINDOWS:
            metrics = {}
            for label in ("gap_continuation", "gap_fill_state", "long_bias", "short_bias"):
                for horizon in HORIZONS:
                    vals = [r[f"w{window}"].get(f"ret_{horizon}m") for r in subset if r[f"w{window}"].get(label) and f"ret_{horizon}m" in r[f"w{window}"]]
                    if label.startswith("short"):
                        vals = [-v for v in vals]
                    metrics[f"{label}_{horizon}m"] = _stats(vals, cost_pct)
            for label in ("overnight_reversal_long", "overnight_reversal_short"):
                for horizon in HORIZONS:
                    vals = [r[f"w{window}"].get(f"ret_{horizon}m") for r in subset if r[f"w{window}"].get(label) and f"ret_{horizon}m" in r[f"w{window}"]]
                    if label.endswith("short"):
                        vals = [-v for v in vals]
                    metric = _stats(vals, cost_pct)
                    metric["stability"] = _stability([v - cost_pct for v in vals])
                    metrics[f"{label}_{horizon}m"] = metric
            for direction in ("long", "short"):
                for horizon in HORIZONS:
                    vals = [r[f"w{window}"].get(f"break_ret_{horizon}m") for r in subset if r[f"w{window}"].get("first_break") == direction and f"break_ret_{horizon}m" in r[f"w{window}"]]
                    metrics[f"first_break_{direction}_{horizon}m"] = _stats(vals, cost_pct)
                    failed = [r[f"w{window}"].get(f"failed_ret_{horizon}m") for r in subset if r[f"w{window}"].get("failed_breakout") and r[f"w{window}"].get("failed_breakout_direction") == direction and f"failed_ret_{horizon}m" in r[f"w{window}"]]
                    metrics[f"failed_breakout_{direction}_{horizon}m"] = _stats(failed, cost_pct)
            out["windows"][str(window)] = metrics
        report["splits"][name] = out
    return report


def _cross_market_records(bars: list[Bar]) -> dict[date, dict]:
    days, _ = _rth_days(bars)
    result = {}
    for day, rows in days.items():
        if len(rows) >= 60:
            result[day] = {
                "open5": rows[4].close / rows[0].open - 1.0,
                "next60": rows[59].close / rows[4].close - 1.0,
            }
    return result


def cross_market_report(qqq_bars: list[Bar], nq_bars: list[Bar], cost_bps: float) -> dict:
    qqq = _cross_market_records(qqq_bars)
    nq = _cross_market_records(nq_bars)
    rows = []
    for day in sorted(set(qqq) & set(nq)):
        q, n = qqq[day], nq[day]
        if q["open5"] > 0 and n["open5"] > 0:
            state = "agreement_long"
            values = [(q["next60"] + n["next60"]) / 2]
        elif q["open5"] < 0 and n["open5"] < 0:
            state = "agreement_short"
            values = [-(q["next60"] + n["next60"]) / 2]
        else:
            state = "divergence"
            values = [q["next60"], n["next60"]]
        rows.append((state, values))
    out = {"matched_days": len(rows), "states": {}}
    for state in ("agreement_long", "agreement_short", "divergence"):
        values = [v for s, vs in rows if s == state for v in vs]
        out["states"][state] = _stats([v * 100 for v in values], cost_bps / 100.0)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qqq", type=Path, required=True)
    parser.add_argument("--nq", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cost-bps", type=float, default=2.0)
    args = parser.parse_args()
    qqq_bars = load_qqq(args.qqq)
    nq_bars = load_nq(args.nq)
    report = {
        "spec": "OPENING_BEHAVIOR_RESEARCH_SPEC.md",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "cost_assumption_bps": args.cost_bps,
        "qqq": analyze("QQQ", qqq_bars, args.cost_bps),
        "nq": analyze("NQ", nq_bars, args.cost_bps),
    }
    report["cross_market"] = cross_market_report(qqq_bars, nq_bars, args.cost_bps)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: {"records": report[k]["records"], "quality": report[k]["quality"]} for k in ("qqq", "nq")}, indent=2))


if __name__ == "__main__":
    main()
