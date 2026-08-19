"""Research-only QQQ close-imbalance to NQ overnight/opening analysis."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import time
from pathlib import Path
from statistics import median

import numpy as np

try:
    from .opening_fair_value import ET, rolling_close_volatility
    from .opening_noii import parse_ns_timestamp
    from .opening_two_minute import summarize
except ImportError:
    from opening_fair_value import ET, rolling_close_volatility
    from opening_noii import parse_ns_timestamp
    from opening_two_minute import summarize


CUTOFF = time(15, 59, 50)
INHERITED_NORMALIZED_GAP_THRESHOLD = 1.170437162019228


def load_sessions(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return [
            {key: value if key == "day" else float(value) for key, value in row.items()}
            for row in csv.DictReader(fh)
        ]


def load_close_snapshots(path: Path) -> tuple[dict[str, dict], dict]:
    candidates = {}
    quality = defaultdict(int)
    with path.open() as fh:
        for line in fh:
            quality["source_records"] += 1
            row = json.loads(line)
            if row.get("auction_type") != "C":
                continue
            quality["closing_records"] += 1
            received = parse_ns_timestamp(row["ts_recv"])
            local = received.astimezone(ET)
            if local.time().replace(tzinfo=None) > CUTOFF:
                continue
            day = local.date().isoformat()
            previous = candidates.get(day)
            if previous is None or received > previous[0]:
                candidates[day] = (received, row)
    quality["snapshot_days"] = len(candidates)
    return {day: item[1] for day, item in candidates.items()}, dict(quality)


def signed_ratio(snapshot: dict) -> float:
    sign = 1.0 if snapshot.get("side") == "B" else -1.0 if snapshot.get("side") == "A" else 0.0
    paired = float(snapshot.get("paired_qty") or 0)
    imbalance = float(snapshot.get("total_imbalance_qty") or 0)
    return sign * imbalance / paired if paired > 0 else 0.0


def join(rows: list[dict], snapshots: dict[str, dict]) -> tuple[list[dict], dict]:
    output = []
    excluded = defaultdict(int)
    volatility = rolling_close_volatility(rows)
    for index in range(1, len(rows)):
        current = rows[index]
        prior_day = rows[index - 1]["day"]
        snapshot = snapshots.get(prior_day)
        if snapshot is None:
            excluded["missing_prior_close_snapshot"] += 1
            continue
        ratio = signed_ratio(snapshot)
        overnight = current["nq_overnight_ret"]
        inventory_direction = -1 if ratio > 0 else 1 if ratio < 0 else 0
        gap_direction = 1 if overnight > 0 else -1 if overnight < 0 else 0
        record = {
            "day": current["day"],
            "prior_day": prior_day,
            "signed_close_imbalance_ratio": ratio,
            "absolute_close_imbalance_ratio": abs(ratio),
            "inventory_direction": inventory_direction,
            "nq_overnight_return_pct": overnight,
            "inventory_aligned_overnight_return_pct": inventory_direction * overnight,
            "inventory_predicts_gap_sign": inventory_direction == gap_direction,
            "absolute_gap_pct": abs(overnight),
            "base_direction": -gap_direction,
            "target_points": current["target_points"],
            "base_fade_points": (-gap_direction) * current["target_points"],
            "normalized_gap": abs(overnight) / volatility[current["day"]]
            if current["day"] in volatility
            else None,
        }
        output.append(record)
    return output, {"joined_sessions": len(output), "excluded": dict(excluded)}


def association(rows: list[dict], strong_threshold: float) -> dict:
    def describe(sample: list[dict]) -> dict:
        aligned = [row["inventory_aligned_overnight_return_pct"] for row in sample]
        return {
            "sessions": len(sample),
            "direction_accuracy": sum(value > 0 for value in aligned) / len(aligned) if aligned else None,
            "mean_inventory_aligned_overnight_return_pct": float(np.mean(aligned)) if aligned else None,
        }

    x = np.array([row["signed_close_imbalance_ratio"] for row in rows], dtype=float)
    y = np.array([row["nq_overnight_return_pct"] for row in rows], dtype=float)
    strong = [row for row in rows if row["absolute_close_imbalance_ratio"] >= strong_threshold]
    return {
        "all": describe(rows),
        "strong": describe(strong),
        "signed_ratio_vs_next_overnight_correlation": float(np.corrcoef(x, y)[0, 1])
        if len(rows) >= 2 and np.std(x) and np.std(y)
        else None,
    }


def opening_groups(rows: list[dict], strong_threshold: float) -> dict:
    large = [
        row
        for row in rows
        if row["absolute_gap_pct"] >= 1.0
        and row["absolute_close_imbalance_ratio"] >= strong_threshold
    ]
    groups = {
        "inventory_consistent": [row for row in large if row["inventory_predicts_gap_sign"]],
        "inventory_inconsistent": [row for row in large if not row["inventory_predicts_gap_sign"]],
        "inventory_consistent_high_normalized": [
            row
            for row in large
            if row["inventory_predicts_gap_sign"]
            and row["normalized_gap"] is not None
            and row["normalized_gap"] >= INHERITED_NORMALIZED_GAP_THRESHOLD
        ],
    }
    reports = {}
    for index, (name, group) in enumerate(groups.items()):
        values = [row["base_fade_points"] for row in group]
        report = summarize(values, len(large), 5000 + index)
        if values:
            trimmed = values.copy()
            trimmed.remove(max(trimmed))
            report["mean_without_best_trade"] = sum(trimmed) / len(trimmed) if trimmed else None
        reports[name] = report
    return reports


def write_records(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def analyze(rows: list[dict]) -> dict:
    first, second = len(rows) // 3, 2 * len(rows) // 3
    periods = {
        "discovery": rows[:first],
        "validation": rows[first:second],
        "retrospective_confirmation": rows[second:],
        "all": rows,
    }
    threshold = median(row["absolute_close_imbalance_ratio"] for row in periods["discovery"])
    return {
        "frozen_strong_imbalance_threshold": threshold,
        "inherited_normalized_gap_threshold": INHERITED_NORMALIZED_GAP_THRESHOLD,
        "period_boundaries": {
            name: [part[0]["day"], part[-1]["day"], len(part)] for name, part in periods.items()
        },
        "periods": {
            name: {
                "overnight_association": association(part, threshold),
                "opening_fade_groups": opening_groups(part, threshold),
            }
            for name, part in periods.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--noii", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records-output", type=Path)
    args = parser.parse_args()
    sessions = load_sessions(args.sessions)
    snapshots, source_quality = load_close_snapshots(args.noii)
    rows, join_quality = join(sessions, snapshots)
    if len(rows) < 300:
        raise SystemExit(f"Insufficient joined sessions: {len(rows)}")
    report = analyze(rows)
    report["quality"] = {"source": source_quality, "join": join_quality}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.records_output:
        write_records(args.records_output, rows)
    print(json.dumps({"quality": report["quality"], "period_boundaries": report["period_boundaries"]}, indent=2))


if __name__ == "__main__":
    main()
