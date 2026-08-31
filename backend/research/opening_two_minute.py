"""Session-level, research-only NQ cash-open prediction study.

The implementation follows docs/OPENING_TWO_MINUTE_RESEARCH_SPEC.md.  It has
no imports from the production package and cannot place orders or write to the
learning database.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import numpy as np


ET = ZoneInfo("America/New_York")
FEATURES = (
    "nq_ret_5m",
    "qqq_ret_5m",
    "nq_ret_30m",
    "qqq_ret_30m",
    "nq_overnight_ret",
    "qqq_overnight_ret",
    "nq_overnight_location",
    "qqq_premarket_location",
    "nq_overnight_range_pct",
    "qqq_premarket_range_pct",
    "late_divergence",
    "overnight_divergence",
    "prior_nq_rth_ret",
    "prior_nq_rth_range_pct",
    "qqq_relative_premarket_volume",
)
COST_POINTS = (0.0, 0.5, 1.0, 2.0, 3.0)


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    instrument_id: int = 0


@dataclass
class SessionAgg:
    high: float = -math.inf
    low: float = math.inf
    volume: float = 0.0
    marks: dict[tuple[int, int], Bar] = field(default_factory=dict)

    def add(self, local: datetime, bar: Bar) -> None:
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.volume += bar.volume
        self.marks[(local.hour, local.minute)] = bar


def _valid(bar: Bar) -> bool:
    return (
        min(bar.open, bar.close) >= bar.low > 0
        and bar.high >= max(bar.open, bar.close)
    )


def _iter_nq(path: Path):
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            ts = datetime.fromtimestamp(int(row["hd"]["ts_event"]) / 1e9, timezone.utc)
            scale = 1_000_000_000
            yield Bar(
                ts,
                *(int(row[k]) / scale for k in ("open", "high", "low", "close")),
                float(row["volume"]),
                int(row["hd"]["instrument_id"]),
            )


def _iter_qqq(path: Path):
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            yield Bar(
                ts,
                *(float(row[k]) for k in ("open", "high", "low", "close")),
                float(row["volume"]),
            )


def _next_calendar_day(day: date) -> date:
    return day + timedelta(days=1)


def aggregate(path: Path, instrument: str) -> tuple[dict, dict, dict]:
    pre: dict[date, SessionAgg] = defaultdict(SessionAgg)
    rth: dict[date, SessionAgg] = defaultdict(SessionAgg)
    bad = duplicates = rows = 0
    seen: set[datetime] = set()
    iterator = _iter_nq(path) if instrument == "NQ" else _iter_qqq(path)
    for bar in iterator:
        rows += 1
        if bar.ts in seen:
            duplicates += 1
        seen.add(bar.ts)
        if not _valid(bar):
            bad += 1
            continue
        local = bar.ts.astimezone(ET)
        hm = (local.hour, local.minute)
        if (9, 30) <= hm < (16, 0):
            rth[local.date()].add(local, bar)
        if instrument == "NQ":
            if hm >= (18, 0):
                pre[_next_calendar_day(local.date())].add(local, bar)
            elif hm <= (9, 28):
                pre[local.date()].add(local, bar)
        elif (4, 0) <= hm <= (9, 28):
            pre[local.date()].add(local, bar)
    quality = {
        "source_rows": rows,
        "invalid_rows": bad,
        "duplicate_timestamps": duplicates,
        "pre_sessions": len(pre),
        "rth_sessions": len(rth),
    }
    return dict(pre), dict(rth), quality


def _pct(end: float, start: float) -> float:
    return (end / start - 1.0) * 100.0


def _location(value: float, low: float, high: float) -> float:
    return (value - low) / (high - low) if high > low else 0.5


def build_records(nq_path: Path, qqq_path: Path) -> tuple[list[dict], dict]:
    nq_pre, nq_rth, nq_quality = aggregate(nq_path, "NQ")
    q_pre, q_rth, q_quality = aggregate(qqq_path, "QQQ")
    common = sorted(set(nq_rth) & set(q_rth) & set(nq_pre) & set(q_pre))
    nq_days = sorted(nq_rth)
    q_days = sorted(q_rth)
    nq_prev = {day: nq_rth[nq_days[i - 1]] for i, day in enumerate(nq_days) if i}
    q_prev = {day: q_rth[q_days[i - 1]] for i, day in enumerate(q_days) if i}
    q_vol_history: list[float] = []
    records: list[dict] = []
    excluded = defaultdict(int)
    required_pre = {(8, 59), (9, 24), (9, 28)}
    required_rth = {(9, 30), (9, 31), (15, 59)}
    for day in common:
        if day not in nq_prev or day not in q_prev:
            excluded["no_prior_session"] += 1
            continue
        np, qp, nr, qr = nq_pre[day], q_pre[day], nq_rth[day], q_rth[day]
        if not required_pre.issubset(np.marks) or not required_pre.issubset(qp.marks):
            excluded["missing_preopen_mark"] += 1
            continue
        if not required_rth.issubset(nr.marks) or not required_rth.issubset(qr.marks):
            excluded["missing_rth_mark"] += 1
            continue
        nprev, qprev = nq_prev[day], q_prev[day]
        if (9, 30) not in nprev.marks or (15, 59) not in nprev.marks or (15, 59) not in qprev.marks:
            excluded["incomplete_prior_session"] += 1
            continue
        n_end = np.marks[(9, 28)].close
        q_end = qp.marks[(9, 28)].close
        n_prior_close = nprev.marks[(15, 59)].close
        q_prior_close = qprev.marks[(15, 59)].close
        prior_open = nprev.marks[(9, 30)].open
        prior_range = nprev.high - nprev.low
        # A continuous symbol can switch its underlying futures contract.  A
        # prior close and current pre-open price from different contracts can
        # create a synthetic "overnight gap", so the transition day is never
        # eligible for the signal study.
        if np.marks[(9, 28)].instrument_id != nprev.marks[(15, 59)].instrument_id:
            excluded["nq_roll_transition"] += 1
            continue
        rolling_volume = median(q_vol_history[-20:]) if len(q_vol_history) >= 20 else None
        rec = {
            "day": str(day),
            "nq_entry": nr.marks[(9, 30)].open,
            "nq_exit": nr.marks[(9, 31)].close,
            "target_points": nr.marks[(9, 31)].close - nr.marks[(9, 30)].open,
            "nq_prior_close": n_prior_close,
            "qqq_prior_close": q_prior_close,
            "nq_0928_close": n_end,
            "qqq_0928_close": q_end,
            "qqq_0930_open": qr.marks[(9, 30)].open,
            "nq_ret_5m": _pct(n_end, np.marks[(9, 24)].open),
            "qqq_ret_5m": _pct(q_end, qp.marks[(9, 24)].open),
            "nq_ret_30m": _pct(n_end, np.marks[(8, 59)].open),
            "qqq_ret_30m": _pct(q_end, qp.marks[(8, 59)].open),
            "nq_overnight_ret": _pct(n_end, n_prior_close),
            "qqq_overnight_ret": _pct(q_end, q_prior_close),
            "nq_overnight_location": _location(n_end, np.low, np.high),
            "qqq_premarket_location": _location(q_end, qp.low, qp.high),
            "nq_overnight_range_pct": (np.high - np.low) / n_prior_close * 100.0,
            "qqq_premarket_range_pct": (qp.high - qp.low) / q_prior_close * 100.0,
            "prior_nq_rth_ret": _pct(n_prior_close, prior_open),
            "prior_nq_rth_range_pct": prior_range / n_prior_close * 100.0,
            "qqq_relative_premarket_volume": qp.volume / rolling_volume if rolling_volume else 1.0,
        }
        rec["late_divergence"] = rec["nq_ret_5m"] - rec["qqq_ret_5m"]
        rec["overnight_divergence"] = rec["nq_overnight_ret"] - rec["qqq_overnight_ret"]
        records.append(rec)
        q_vol_history.append(qp.volume)
    return records, {
        "nq": nq_quality,
        "qqq": q_quality,
        "common_sessions": len(common),
        "usable_sessions": len(records),
        "excluded": dict(excluded),
    }


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def rule_directions(row: dict) -> dict[str, int]:
    n5, q5 = _sign(row["nq_ret_5m"]), _sign(row["qqq_ret_5m"])
    no, qo = _sign(row["nq_overnight_ret"]), _sign(row["qqq_overnight_ret"])
    loc = row["nq_overnight_location"]
    breakout = n5 if (loc >= 0.8 and n5 > 0) or (loc <= 0.2 and n5 < 0) else 0
    rejection = n5 if (loc >= 0.8 and n5 < 0) or (loc <= 0.2 and n5 > 0) else 0
    return {
        "nq_5m_continuation": n5,
        "qqq_5m_continuation": q5,
        "late_agreement": n5 if n5 == q5 else 0,
        "nq_overnight_continuation": no,
        "overnight_agreement": no if no == qo else 0,
        "overnight_extreme_breakout": breakout,
        "overnight_extreme_rejection": rejection,
    }


def _wilson(wins: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.959963984540054
    p = wins / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [center - half, center + half]


def summarize(signed_points: list[float], sessions: int, seed: int) -> dict:
    if not signed_points:
        return {"sessions": sessions, "trades": 0, "abstentions": sessions}
    wins = sum(x > 0 for x in signed_points)
    losses = [-x for x in signed_points if x < 0]
    win_values = [x for x in signed_points if x > 0]
    gross_loss = sum(losses)
    equity = peak = max_dd = 0.0
    for value in signed_points:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    rng = random.Random(seed)
    boot = sorted(mean(rng.choice(signed_points) for _ in signed_points) for _ in range(5000))
    result = {
        "sessions": sessions,
        "trades": len(signed_points),
        "abstentions": sessions - len(signed_points),
        "wins": wins,
        "direction_accuracy": wins / len(signed_points),
        "direction_accuracy_wilson95": _wilson(wins, len(signed_points)),
        "gross_mean_points": mean(signed_points),
        "gross_median_points": median(signed_points),
        "gross_total_points": sum(signed_points),
        "profit_factor_gross": sum(win_values) / gross_loss if gross_loss else None,
        "maximum_drawdown_points": max_dd,
        "worst_trade_points": min(signed_points),
        "best_trade_points": max(signed_points),
        "bootstrap_mean_points_ci95": [boot[124], boot[4874]],
        "cost_scenarios": {},
    }
    for cost in COST_POINTS:
        net = [x - cost for x in signed_points]
        result["cost_scenarios"][str(cost)] = {
            "mean_net_points": mean(net),
            "total_net_points": sum(net),
            "mean_nq_dollars": mean(net) * 20.0,
            "total_nq_dollars": sum(net) * 20.0,
            "mean_mnq_dollars": mean(net) * 2.0,
            "total_mnq_dollars": sum(net) * 2.0,
        }
    return result


def _evaluate_rules(rows: list[dict], seed: int) -> dict:
    names = list(rule_directions(rows[0])) if rows else []
    output = {}
    for j, name in enumerate(names):
        signed = []
        monthly: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            direction = rule_directions(row)[name]
            if direction:
                value = direction * row["target_points"]
                signed.append(value)
                monthly[row["day"][:7]].append(value)
        report = summarize(signed, len(rows), seed + j)
        report["monthly_gross_mean_points"] = {m: mean(v) for m, v in sorted(monthly.items())}
        output[name] = report
    return output


def _correlations(rows: list[dict]) -> dict:
    y = np.array([r["target_points"] for r in rows], dtype=float)
    result = {}
    for name in FEATURES:
        x = np.array([r[name] for r in rows], dtype=float)
        result[name] = float(np.corrcoef(x, y)[0, 1]) if np.std(x) and np.std(y) else 0.0
    return result


def _fit_ridge(train: list[dict]) -> dict:
    x = np.array([[r[f] for f in FEATURES] for r in train], dtype=float)
    y = np.array([_sign(r["target_points"]) for r in train], dtype=float)
    center = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale == 0] = 1.0
    z = (x - center) / scale
    design = np.column_stack((np.ones(len(z)), z))
    penalty = np.eye(design.shape[1])
    penalty[0, 0] = 0.0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    return {"center": center, "scale": scale, "beta": beta}


def _ridge_directions(model: dict, rows: list[dict]) -> list[int]:
    x = np.array([[r[f] for f in FEATURES] for r in rows], dtype=float)
    z = (x - model["center"]) / model["scale"]
    score = np.column_stack((np.ones(len(z)), z)) @ model["beta"]
    return [_sign(float(v)) for v in score]


def analyze(records: list[dict]) -> dict:
    n = len(records)
    cut1, cut2 = n // 3, 2 * n // 3
    periods = {
        "discovery": records[:cut1],
        "validation": records[cut1:cut2],
        "retrospective_confirmation": records[cut2:],
        "all": records,
    }
    model = _fit_ridge(periods["discovery"])
    report = {
        "target": "NQ 09:30 bar open to 09:31 bar close",
        "decision_time_et": "09:29:00",
        "latest_feature_bar_et": "09:28",
        "period_boundaries": {
            name: [rows[0]["day"], rows[-1]["day"], len(rows)]
            for name, rows in periods.items()
            if rows
        },
        "feature_correlations_all": _correlations(records),
        "ridge_coefficients": {
            "intercept": float(model["beta"][0]),
            **{f: float(v) for f, v in zip(FEATURES, model["beta"][1:])},
        },
        "periods": {},
    }
    for i, (name, rows) in enumerate(periods.items()):
        directions = _ridge_directions(model, rows)
        signed = [d * r["target_points"] for d, r in zip(directions, rows) if d]
        report["periods"][name] = {
            "unconditional_target": summarize([r["target_points"] for r in rows], len(rows), 9000 + i),
            "rules": _evaluate_rules(rows, 10000 + i * 100),
            "frozen_ridge_score": summarize(signed, len(rows), 20000 + i),
            "feature_correlations": _correlations(rows),
        }
    cutoff = datetime.fromisoformat(records[-1]["day"]).date() - timedelta(days=365)
    recent = [r for r in records if datetime.fromisoformat(r["day"]).date() >= cutoff]
    report["most_recent_12_months"] = {
        "range": [recent[0]["day"], recent[-1]["day"]],
        "rules": _evaluate_rules(recent, 30000),
    }
    return report


def write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nq", type=Path, required=True)
    parser.add_argument("--qqq", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records-output", type=Path)
    args = parser.parse_args()
    records, quality = build_records(args.nq, args.qqq)
    if len(records) < 300:
        raise SystemExit(f"Insufficient usable sessions: {len(records)}")
    report = analyze(records)
    report["quality"] = quality
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.records_output:
        write_records(args.records_output, records)
    print(json.dumps({"quality": quality, "period_boundaries": report["period_boundaries"]}, indent=2))


if __name__ == "__main__":
    main()
