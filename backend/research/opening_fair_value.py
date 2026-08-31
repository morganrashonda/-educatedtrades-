"""Research-only NQ/QQQ fair-value and overnight-path study."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

try:
    from .opening_noii import load_snapshots, snapshot_features
    from .opening_two_minute import summarize
except ImportError:
    from opening_noii import load_snapshots, snapshot_features
    from opening_two_minute import summarize


ET = ZoneInfo("America/New_York")
REQUIRED_MARKS = ((1, 59), (2, 0), (8, 29), (8, 30), (8, 31), (8, 59), (9, 0), (9, 28))


@dataclass
class MinuteBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class PathSession:
    bars: list[MinuteBar] = field(default_factory=list)
    marks: dict[tuple[int, int], MinuteBar] = field(default_factory=dict)

    def add(self, local: datetime, bar: MinuteBar) -> None:
        self.bars.append(bar)
        self.marks[(local.hour, local.minute)] = bar


def load_session_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            rows.append({key: value if key == "day" else float(value) for key, value in row.items()})
    return rows


def load_nq_paths(path: Path, session_days: set[str]) -> tuple[dict[str, PathSession], dict]:
    grouped: dict[str, PathSession] = defaultdict(PathSession)
    quality = defaultdict(int)
    with path.open() as fh:
        for line in fh:
            quality["source_rows"] += 1
            row = json.loads(line)
            ts = datetime.fromtimestamp(int(row["hd"]["ts_event"]) / 1e9, timezone.utc)
            local = ts.astimezone(ET)
            hm = (local.hour, local.minute)
            session_day: Optional[date] = None
            if hm >= (18, 0):
                session_day = local.date() + timedelta(days=1)
            elif hm <= (9, 28):
                session_day = local.date()
            if session_day is None or session_day.isoformat() not in session_days:
                continue
            scale = 1_000_000_000
            bar = MinuteBar(
                ts=ts,
                open=int(row["open"]) / scale,
                high=int(row["high"]) / scale,
                low=int(row["low"]) / scale,
                close=int(row["close"]) / scale,
            )
            grouped[session_day.isoformat()].add(local, bar)
            quality["retained_preopen_rows"] += 1
    for session in grouped.values():
        session.bars.sort(key=lambda bar: bar.ts)
    quality["path_sessions"] = len(grouped)
    return dict(grouped), dict(quality)


def _pct(end: float, start: float) -> float:
    return (end / start - 1.0) * 100.0


def path_features(session: PathSession, prior_close: float, total_gap_pct: float) -> Optional[dict]:
    if not set(REQUIRED_MARKS).issubset(session.marks):
        return None
    marks = session.marks
    sign = 1 if total_gap_pct > 0 else -1
    segment_0159 = _pct(marks[(1, 59)].close, prior_close)
    segment_0200_0829 = _pct(marks[(8, 29)].close, marks[(2, 0)].open)
    segment_0830 = _pct(marks[(8, 30)].close, marks[(8, 30)].open)
    segment_0831_0859 = _pct(marks[(8, 59)].close, marks[(8, 31)].open)
    segment_0900_0928 = _pct(marks[(9, 28)].close, marks[(9, 0)].open)

    closes = [prior_close] + [bar.close for bar in session.bars]
    cumulative = sum(abs(_pct(b, a)) for a, b in zip(closes, closes[1:]))
    efficiency = abs(total_gap_pct) / cumulative if cumulative else 0.0

    buckets: dict[tuple, list[MinuteBar]] = defaultdict(list)
    for bar in session.bars:
        local = bar.ts.astimezone(ET)
        buckets[(local.date(), local.hour, local.minute // 5)].append(bar)
    five_minute = []
    for bars in buckets.values():
        bars.sort(key=lambda bar: bar.ts)
        if len(bars) == 5 and (bars[-1].ts - bars[0].ts).total_seconds() == 240:
            five_minute.append(abs(_pct(bars[-1].close, bars[0].open)))
    largest_five = max(five_minute) if five_minute else 0.0

    if sign > 0:
        extreme = max(bar.high for bar in session.bars)
        extreme_bar = max(
            (bar for bar in session.bars if bar.high == extreme),
            key=lambda bar: bar.ts,
        )
    else:
        extreme = min(bar.low for bar in session.bars)
        extreme_bar = max(
            (bar for bar in session.bars if bar.low == extreme),
            key=lambda bar: bar.ts,
        )
    decision = datetime.combine(session.bars[-1].ts.astimezone(ET).date(), time(9, 29), ET)
    minutes_since_extreme = (decision - extreme_bar.ts.astimezone(ET)).total_seconds() / 60.0

    return {
        "segment_close_to_0159": segment_0159,
        "segment_0200_0829": segment_0200_0829,
        "segment_0830": segment_0830,
        "segment_0831_0859": segment_0831_0859,
        "segment_0900_0928": segment_0900_0928,
        "late_confirmation": sign * segment_0900_0928,
        "shock_0830_contribution": sign * segment_0830 / max(abs(total_gap_pct), 1e-12),
        "path_efficiency": efficiency,
        "largest_5m_concentration": largest_five / max(abs(total_gap_pct), 1e-12),
        "minutes_since_directional_extreme": minutes_since_extreme,
    }


def rolling_close_volatility(rows: list[dict], window: int = 20) -> dict[str, float]:
    output = {}
    prior_closes = [row["nq_prior_close"] for row in rows]
    for index, row in enumerate(rows):
        prior_returns = [
            _pct(prior_closes[j], prior_closes[j - 1])
            for j in range(max(1, index - window + 1), index + 1)
        ]
        if len(prior_returns) >= window:
            output[row["day"]] = float(np.std(prior_returns, ddof=1))
    return output


def rolling_fair_values(
    rows: list[dict], snapshots: dict[str, dict], window: int = 60, minimum: int = 40
) -> dict[str, dict]:
    history: list[tuple[float, float]] = []
    output = {}
    for row in rows:
        day_snapshot = snapshots.get(row["day"], {}).get("snapshot_2900")
        if day_snapshot is None:
            continue
        features = snapshot_features(day_snapshot, fade_direction=1)
        near = features["near_clearing_price"]
        if near <= 0 or row["qqq_prior_close"] <= 0 or row["nq_prior_close"] <= 0:
            continue
        qqq_indicative_return = near / row["qqq_prior_close"] - 1.0
        nq_open_return = row["nq_entry"] / row["nq_prior_close"] - 1.0
        if len(history) >= minimum:
            sample = history[-window:]
            x = np.array([pair[0] for pair in sample], dtype=float)
            y = np.array([pair[1] for pair in sample], dtype=float)
            design = np.column_stack((np.ones(len(x)), x))
            alpha, beta = np.linalg.lstsq(design, y, rcond=None)[0]
            expected = float(alpha + beta * qqq_indicative_return)
            observed = row["nq_0928_close"] / row["nq_prior_close"] - 1.0
            output[row["day"]] = {
                "qqq_indicative_return": qqq_indicative_return,
                "fair_value_alpha": float(alpha),
                "fair_value_beta": float(beta),
                "expected_nq_open_return": expected,
                "observed_nq_0928_return": observed,
                "fair_value_residual": observed - expected,
                "fair_value_history_n": len(sample),
            }
        # Current-session values become eligible only for later sessions.
        history.append((qqq_indicative_return, nq_open_return))
    return output


def build_records(session_rows: list[dict], paths: dict, snapshots: dict) -> tuple[list[dict], dict]:
    fair_values = rolling_fair_values(session_rows, snapshots)
    volatilities = rolling_close_volatility(session_rows)
    output = []
    excluded = defaultdict(int)
    for row in session_rows:
        day = row["day"]
        if abs(row["nq_overnight_ret"]) < 1.0:
            continue
        if day not in fair_values:
            excluded["missing_fair_value_history"] += 1
            continue
        if day not in volatilities:
            excluded["missing_volatility_history"] += 1
            continue
        if day not in paths:
            excluded["missing_path_session"] += 1
            continue
        path = path_features(paths[day], row["nq_prior_close"], row["nq_overnight_ret"])
        if path is None:
            excluded["missing_path_marks"] += 1
            continue
        base_direction = -1 if row["nq_overnight_ret"] > 0 else 1
        residual = fair_values[day]["fair_value_residual"]
        residual_direction = -1 if residual > 0 else 1 if residual < 0 else 0
        record = {
            "day": day,
            "target_points": row["target_points"],
            "base_direction": base_direction,
            "fair_value_direction": residual_direction,
            "absolute_gap_pct": abs(row["nq_overnight_ret"]),
            "prior_20d_volatility_pct": volatilities[day],
            "normalized_gap": abs(row["nq_overnight_ret"]) / volatilities[day],
            **fair_values[day],
            **path,
        }
        output.append(record)
    return output, {"eligible_records": len(output), "excluded": dict(excluded)}


def validate_exact_execution(
    rows: list[dict], exact_dir: Path, normalized_threshold: float
) -> dict:
    """Compare minute-bar outcomes with exact BBO mids and crossed spreads."""

    by_day = {row["day"]: row for row in rows}
    seen_days = set()
    outcomes = []
    for path in sorted(exact_dir.glob("nq_opening_ofi_1s_*.csv")):
        match = re.search(r"(\d{4}-\d{2}-\d{2})\.csv$", path.name)
        if not match or match.group(1) in seen_days or match.group(1) not in by_day:
            continue
        day = match.group(1)
        seen_days.add(day)
        entry = exit_row = None
        with path.open(newline="") as fh:
            for quote in csv.DictReader(fh):
                local = datetime.fromisoformat(quote["timestamp_utc"]).astimezone(ET)
                mark = (local.hour, local.minute, local.second)
                if mark == (9, 30, 0):
                    entry = quote
                elif mark == (9, 31, 59):
                    exit_row = quote
        if entry is None or exit_row is None:
            continue
        row = by_day[day]
        direction = int(row["base_direction"])
        bar_points = direction * row["target_points"]
        exact_points = direction * (float(exit_row["close_mid"]) - float(entry["open_mid"]))
        crossing_points = (float(entry["mean_spread"]) + float(exit_row["mean_spread"])) / 2.0
        outcomes.append(
            {
                "bar_points": bar_points,
                "exact_mid_points": exact_points,
                "after_crossing_points": exact_points - crossing_points,
                "selected": row["normalized_gap"] >= normalized_threshold,
            }
        )

    def summarize_exact(sample: list[dict]) -> dict:
        if not sample:
            return {"sessions": 0}
        crossed = [row["after_crossing_points"] for row in sample]
        trimmed = crossed.copy()
        trimmed.remove(max(trimmed))
        return {
            "sessions": len(sample),
            "wins_after_crossing": sum(value > 0 for value in crossed),
            "mean_bar_points": float(np.mean([row["bar_points"] for row in sample])),
            "mean_exact_mid_points": float(np.mean([row["exact_mid_points"] for row in sample])),
            "mean_after_crossing_points": float(np.mean(crossed)),
            "mean_after_crossing_without_best": float(np.mean(trimmed)) if trimmed else None,
        }

    report = {
        "all_overlap": summarize_exact(outcomes),
        "high_normalized_gap_overlap": summarize_exact(
            [row for row in outcomes if row["selected"]]
        ),
    }
    if len(outcomes) >= 2:
        bar = np.array([row["bar_points"] for row in outcomes])
        exact = np.array([row["exact_mid_points"] for row in outcomes])
        report["bar_exact_correlation"] = float(np.corrcoef(bar, exact)[0, 1])
        report["bar_exact_mean_absolute_difference_points"] = float(np.mean(np.abs(bar - exact)))
    return report


def _direction_map(row: dict, residual_threshold: float, normalized_threshold: float) -> dict[str, int]:
    base = int(row["base_direction"])
    residual_direction = int(row["fair_value_direction"])
    late_rejection = row["late_confirmation"] < 0
    return {
        "base_gap_fade": base,
        "fair_value_direction": residual_direction,
        "fair_value_selective": residual_direction
        if abs(row["fair_value_residual"]) >= residual_threshold
        else 0,
        "late_rejection_fade": base if late_rejection else 0,
        "residual_and_rejection": base
        if late_rejection and residual_direction == base
        else 0,
        "high_normalized_gap_fade": base if row["normalized_gap"] >= normalized_threshold else 0,
    }


def _rule_reports(
    rows: list[dict], residual_threshold: float, normalized_threshold: float, seed: int
) -> dict:
    names = list(_direction_map(rows[0], residual_threshold, normalized_threshold)) if rows else []
    result = {}
    for index, name in enumerate(names):
        values = []
        for row in rows:
            direction = _direction_map(row, residual_threshold, normalized_threshold)[name]
            if direction:
                values.append(direction * row["target_points"])
        report = summarize(values, len(rows), seed + index)
        if values:
            trimmed = values.copy()
            trimmed.remove(max(trimmed))
            report["mean_without_best_trade"] = sum(trimmed) / len(trimmed) if trimmed else None
        result[name] = report
    return result


def _correlations(rows: list[dict]) -> dict:
    names = (
        "fair_value_residual",
        "late_confirmation",
        "shock_0830_contribution",
        "path_efficiency",
        "largest_5m_concentration",
        "minutes_since_directional_extreme",
        "normalized_gap",
    )
    base_returns = np.array([row["base_direction"] * row["target_points"] for row in rows])
    result = {}
    for name in names:
        x = np.array([row[name] for row in rows], dtype=float)
        result[name] = float(np.corrcoef(x, base_returns)[0, 1]) if np.std(x) else 0.0
    return result


def exploratory_provenance(rows: list[dict], normalized_threshold: float) -> dict:
    """Post-hoc mechanism diagnostic; never represented as holdout evidence."""

    selected = [row for row in rows if row["normalized_gap"] >= normalized_threshold]
    event_share_threshold = 0.10
    groups = {
        "0830_created_at_least_10pct_of_gap": [
            row
            for row in selected
            if abs(row["segment_0830"]) / row["absolute_gap_pct"] >= event_share_threshold
        ],
        "0830_created_less_than_10pct_of_gap": [
            row
            for row in selected
            if abs(row["segment_0830"]) / row["absolute_gap_pct"] < event_share_threshold
        ],
    }
    result = {
        "status": "EXPLORATORY_POST_HOC_NOT_VALIDATION",
        "event_share_threshold": event_share_threshold,
        "groups": {},
    }
    for index, (name, group) in enumerate(groups.items()):
        values = [row["base_direction"] * row["target_points"] for row in group]
        summary = summarize(values, len(selected), 9000 + index)
        if values:
            trimmed = values.copy()
            trimmed.remove(max(trimmed))
            summary["mean_without_best_trade"] = sum(trimmed) / len(trimmed) if trimmed else None
        result["groups"][name] = summary
    return result


def analyze(rows: list[dict]) -> dict:
    n = len(rows)
    first, second = n // 3, 2 * n // 3
    periods = {
        "discovery": rows[:first],
        "validation": rows[first:second],
        "retrospective_confirmation": rows[second:],
        "all": rows,
    }
    residual_threshold = median(abs(row["fair_value_residual"]) for row in periods["discovery"])
    normalized_threshold = median(row["normalized_gap"] for row in periods["discovery"])
    return {
        "frozen_thresholds": {
            "absolute_fair_value_residual": residual_threshold,
            "normalized_gap": normalized_threshold,
        },
        "period_boundaries": {
            name: [part[0]["day"], part[-1]["day"], len(part)] for name, part in periods.items()
        },
        "periods": {
            name: {
                "rules": _rule_reports(part, residual_threshold, normalized_threshold, 1000 + index * 100),
                "feature_correlations_with_base_fade_return": _correlations(part),
            }
            for index, (name, part) in enumerate(periods.items())
        },
        "exploratory_gap_provenance": {
            name: exploratory_provenance(part, normalized_threshold)
            for name, part in periods.items()
        },
    }


def write_records(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--nq", type=Path, required=True)
    parser.add_argument("--noii", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records-output", type=Path)
    parser.add_argument("--exact-dir", type=Path)
    args = parser.parse_args()
    session_rows = load_session_rows(args.sessions)
    snapshots, noii_quality = load_snapshots(args.noii)
    paths, path_quality = load_nq_paths(args.nq, {row["day"] for row in session_rows})
    rows, join_quality = build_records(session_rows, paths, snapshots)
    if len(rows) < 60:
        raise SystemExit(f"Insufficient eligible records: {len(rows)}")
    report = analyze(rows)
    report["quality"] = {"noii": noii_quality, "paths": path_quality, "join": join_quality}
    if args.exact_dir:
        report["exact_execution_validation"] = validate_exact_execution(
            rows, args.exact_dir, report["frozen_thresholds"]["normalized_gap"]
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.records_output:
        write_records(args.records_output, rows)
    print(json.dumps({"quality": report["quality"], "period_boundaries": report["period_boundaries"], "thresholds": report["frozen_thresholds"]}, indent=2))


if __name__ == "__main__":
    main()
