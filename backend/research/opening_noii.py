"""Research-only QQQ NOII mechanism test for the NQ large-gap fade."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

try:
    from .opening_two_minute import summarize
except ImportError:  # Direct script execution from backend/research.
    from opening_two_minute import summarize


ET = ZoneInfo("America/New_York")
SNAPSHOTS = {"snapshot_2900": time(9, 29, 0), "snapshot_2950": time(9, 29, 50)}


def parse_ns_timestamp(value: str) -> datetime:
    """Parse Databento's nanosecond ISO timestamp on Python 3.9."""
    core = value[:-1] if value.endswith("Z") else value
    if "." in core:
        head, fraction = core.split(".", 1)
        core = head + "." + fraction[:6].ljust(6, "0")
        return datetime.strptime(core, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    return datetime.strptime(core, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def _price(value) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 and math.isfinite(result) else None


def load_sessions(path: Path) -> dict[str, dict]:
    sessions = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            sessions[row["day"]] = {
                key: value if key == "day" else float(value)
                for key, value in row.items()
            }
    return sessions


def load_snapshots(path: Path) -> tuple[dict[str, dict], dict]:
    candidates: dict[str, dict[str, tuple[datetime, dict]]] = defaultdict(dict)
    quality = defaultdict(int)
    with path.open() as fh:
        for line in fh:
            quality["source_records"] += 1
            row = json.loads(line)
            if row.get("symbol") != "QQQ":
                quality["non_qqq_records"] += 1
                continue
            if row.get("auction_type") != "O":
                quality["excluded_non_opening_records"] += 1
                continue
            quality["opening_records"] += 1
            ts = parse_ns_timestamp(row["ts_recv"]).astimezone(ET)
            if ts.time() < time(9, 28):
                continue
            day = ts.date().isoformat()
            for name, cutoff in SNAPSHOTS.items():
                if ts.time() <= cutoff:
                    previous = candidates[day].get(name)
                    if previous is None or ts > previous[0]:
                        candidates[day][name] = (ts, row)
    return {
        day: {name: pair[1] for name, pair in snapshots.items()}
        for day, snapshots in candidates.items()
    }, dict(quality)


def snapshot_features(row: dict, fade_direction: int) -> dict:
    side = 1 if row.get("side") == "B" else -1 if row.get("side") == "A" else 0
    paired = float(row.get("paired_qty") or 0)
    imbalance = float(row.get("total_imbalance_qty") or 0)
    signed_ratio = side * imbalance / max(paired, 1.0)
    ref = _price(row.get("ref_price"))
    near = _price(row.get("cont_book_clr_price"))
    near_bps = (near / ref - 1.0) * 10_000.0 if ref and near else 0.0
    return {
        "ref_price": ref or 0.0,
        "near_clearing_price": near or 0.0,
        "signed_imbalance_ratio": signed_ratio,
        "near_displacement_bps": near_bps,
        "log_paired_qty": math.log1p(paired),
        "log_imbalance_qty": math.log1p(imbalance),
        "fade_aligned_imbalance": fade_direction * signed_ratio,
        "fade_aligned_near": fade_direction * near_bps,
    }


def join_sessions(sessions: dict[str, dict], snapshots: dict[str, dict]) -> tuple[list[dict], dict]:
    output = []
    excluded = defaultdict(int)
    for day, session in sorted(sessions.items()):
        if abs(session["nq_overnight_ret"]) < 1.0:
            continue
        day_snapshots = snapshots.get(day, {})
        if not set(SNAPSHOTS).issubset(day_snapshots):
            excluded["missing_snapshot"] += 1
            continue
        fade = -1 if session["nq_overnight_ret"] > 0 else 1
        record = {
            "day": day,
            "fade_direction": fade,
            "overnight_abs_pct": abs(session["nq_overnight_ret"]),
            "signed_fade_points": fade * session["target_points"],
        }
        for name in SNAPSHOTS:
            for feature, value in snapshot_features(day_snapshots[name], fade).items():
                record[f"{name}_{feature}"] = value
        for feature in ("fade_aligned_imbalance", "fade_aligned_near"):
            record[f"change_{feature}"] = (
                record[f"snapshot_2950_{feature}"] - record[f"snapshot_2900_{feature}"]
            )
        output.append(record)
    return output, {"joined_large_gap_sessions": len(output), "excluded": dict(excluded)}


def directions(record: dict, snapshot: str) -> dict[str, bool]:
    imbalance = record[f"{snapshot}_fade_aligned_imbalance"]
    near = record[f"{snapshot}_fade_aligned_near"]
    return {
        "base_fade": True,
        "imbalance_support": imbalance > 0,
        "near_price_support": near > 0,
        "dual_support": imbalance > 0 and near > 0,
        "no_opposition": imbalance >= 0 and near >= 0,
    }


def _rule_report(rows: list[dict], snapshot: str, seed: int) -> dict:
    names = list(directions(rows[0], snapshot)) if rows else []
    result = {}
    for index, name in enumerate(names):
        values = [row["signed_fade_points"] for row in rows if directions(row, snapshot)[name]]
        report = summarize(values, len(rows), seed + index)
        if values:
            trimmed = values.copy()
            trimmed.remove(max(trimmed))
            report["mean_without_best_trade"] = sum(trimmed) / len(trimmed) if trimmed else None
        result[name] = report
    return result


MODEL_FEATURES = (
    "snapshot_2900_fade_aligned_imbalance",
    "snapshot_2900_fade_aligned_near",
    "snapshot_2950_fade_aligned_imbalance",
    "snapshot_2950_fade_aligned_near",
    "change_fade_aligned_imbalance",
    "change_fade_aligned_near",
    "snapshot_2950_log_paired_qty",
    "snapshot_2950_log_imbalance_qty",
    "overnight_abs_pct",
)


def _fit_model(rows: list[dict]) -> dict:
    x = np.array([[row[name] for name in MODEL_FEATURES] for row in rows], dtype=float)
    y = np.array([row["signed_fade_points"] for row in rows], dtype=float)
    center, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale == 0] = 1.0
    z = (x - center) / scale
    design = np.column_stack((np.ones(len(z)), z))
    penalty = np.eye(design.shape[1])
    penalty[0, 0] = 0
    beta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    return {"center": center, "scale": scale, "beta": beta}


def _model_report(model: dict, rows: list[dict], seed: int) -> dict:
    x = np.array([[row[name] for name in MODEL_FEATURES] for row in rows], dtype=float)
    z = (x - model["center"]) / model["scale"]
    predictions = np.column_stack((np.ones(len(z)), z)) @ model["beta"]
    values = [row["signed_fade_points"] for row, prediction in zip(rows, predictions) if prediction > 0]
    result = summarize(values, len(rows), seed)
    result["predicted_positive_fraction"] = len(values) / len(rows) if rows else None
    if values:
        trimmed = values.copy()
        trimmed.remove(max(trimmed))
        result["mean_without_best_trade"] = sum(trimmed) / len(trimmed) if trimmed else None
    return result


def _correlations(rows: list[dict]) -> dict:
    y = np.array([row["signed_fade_points"] for row in rows], dtype=float)
    result = {}
    for name in MODEL_FEATURES:
        x = np.array([row[name] for row in rows], dtype=float)
        result[name] = float(np.corrcoef(x, y)[0, 1]) if np.std(x) and np.std(y) else 0.0
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
    model = _fit_model(periods["discovery"])
    report = {
        "period_boundaries": {
            name: [part[0]["day"], part[-1]["day"], len(part)]
            for name, part in periods.items()
        },
        "model_coefficients": {
            "intercept": float(model["beta"][0]),
            **{name: float(value) for name, value in zip(MODEL_FEATURES, model["beta"][1:])},
        },
        "periods": {},
    }
    for index, (name, part) in enumerate(periods.items()):
        report["periods"][name] = {
            "snapshot_2900": _rule_report(part, "snapshot_2900", 1000 + index * 100),
            "snapshot_2950": _rule_report(part, "snapshot_2950", 2000 + index * 100),
            "frozen_model": _model_report(model, part, 3000 + index),
            "feature_correlations": _correlations(part),
        }
    return report


def write_records(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--noii", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records-output", type=Path)
    args = parser.parse_args()
    sessions = load_sessions(args.sessions)
    snapshots, raw_quality = load_snapshots(args.noii)
    rows, join_quality = join_sessions(sessions, snapshots)
    if len(rows) < 60:
        raise SystemExit(f"Insufficient joined large-gap sessions: {len(rows)}")
    report = analyze(rows)
    report["quality"] = {"raw": raw_quality, "join": join_quality}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.records_output:
        write_records(args.records_output, rows)
    print(json.dumps({"quality": report["quality"], "period_boundaries": report["period_boundaries"]}, indent=2))


if __name__ == "__main__":
    main()
