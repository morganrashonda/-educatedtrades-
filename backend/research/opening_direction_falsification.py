"""Retrospective direction-versus-volatility falsification for the NQ open.

This module is research-only.  It reads frozen opening-research artifacts and
cannot place orders, change production configuration, or write learning data.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import random
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np

try:
    from .opening_fair_value import rolling_close_volatility
except ImportError:
    from opening_fair_value import rolling_close_volatility


ET = ZoneInfo("America/New_York")
COST_POINTS = (0.0, 1.0, 2.0, 3.0)
DEFAULT_RANDOMIZATIONS = 50_000


def load_csv(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="") as fh:
        for raw in csv.DictReader(fh):
            rows.append(
                {
                    key: value if key == "day" else float(value)
                    for key, value in raw.items()
                }
            )
    return rows


def _wilson(wins: int, observations: int) -> list[float] | None:
    if not observations:
        return None
    z = 1.959963984540054
    p = wins / observations
    denominator = 1 + z * z / observations
    center = (p + z * z / (2 * observations)) / denominator
    half = z * math.sqrt(
        p * (1 - p) / observations + z * z / (4 * observations**2)
    ) / denominator
    return [center - half, center + half]


def bootstrap_mean_interval(
    values: list[float], *, seed: int, samples: int = DEFAULT_RANDOMIZATIONS
) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(seed)
    estimates = sorted(mean(rng.choice(values) for _ in values) for _ in range(samples))
    return [
        estimates[int(samples * 0.025)],
        estimates[min(samples - 1, int(samples * 0.975))],
    ]


def random_direction_test(
    targets: list[float], observed_mean: float, *, seed: int, samples: int = DEFAULT_RANDOMIZATIONS
) -> dict:
    """Use the identical days and magnitudes with an independent random side."""

    if not targets:
        return {"samples": 0}
    rng = random.Random(seed)
    null = []
    for _ in range(samples):
        null.append(mean((1 if rng.random() < 0.5 else -1) * value for value in targets))
    null.sort()
    exceedances = sum(value >= observed_mean for value in null)
    return {
        "samples": samples,
        "null_mean_points": mean(null),
        "null_ci95_points": [null[int(samples * 0.025)], null[int(samples * 0.975)]],
        "one_sided_p_value": (exceedances + 1) / (samples + 1),
    }


def _volatility_terciles(rows: list[dict]) -> tuple[float, float]:
    values = np.array([row["prior_20d_volatility_pct"] for row in rows], dtype=float)
    return float(np.quantile(values, 1 / 3)), float(np.quantile(values, 2 / 3))


def _permutation_block(row: dict, cuts: tuple[float, float]) -> tuple[str, int]:
    day = date.fromisoformat(row["day"])
    quarter = (day.month - 1) // 3 + 1
    volatility = row["prior_20d_volatility_pct"]
    bucket = 0 if volatility <= cuts[0] else 1 if volatility <= cuts[1] else 2
    return f"{day.year}-Q{quarter}", bucket


def blocked_direction_permutation(
    rows: list[dict], observed_mean: float, *, seed: int, samples: int = DEFAULT_RANDOMIZATIONS
) -> dict:
    """Shuffle frozen fade sides within calendar-quarter/volatility blocks."""

    if not rows:
        return {"samples": 0}
    cuts = _volatility_terciles(rows)
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in rows:
        groups[_permutation_block(row, cuts)].append(row)
    rng = random.Random(seed)
    null = []
    for _ in range(samples):
        total = 0.0
        for group in groups.values():
            directions = [int(row["base_direction"]) for row in group]
            rng.shuffle(directions)
            total += sum(
                direction * row["target_points"]
                for direction, row in zip(directions, group)
            )
        null.append(total / len(rows))
    null.sort()
    exceedances = sum(value >= observed_mean for value in null)
    variable_blocks = sum(
        len({int(row["base_direction"]) for row in group}) > 1
        for group in groups.values()
    )
    return {
        "samples": samples,
        "blocks": len(groups),
        "blocks_with_both_directions": variable_blocks,
        "volatility_tercile_cuts": list(cuts),
        "null_mean_points": mean(null),
        "null_ci95_points": [null[int(samples * 0.025)], null[int(samples * 0.975)]],
        "one_sided_p_value": (exceedances + 1) / (samples + 1),
    }


def _trimmed(values: list[float], remove: Callable[[list[float]], float]) -> float | None:
    if len(values) <= 1:
        return None
    remaining = values.copy()
    remaining.remove(remove(remaining))
    return mean(remaining)


def summarize_strategy(
    rows: list[dict], direction: Callable[[dict], int], *, seed: int
) -> dict:
    points = [direction(row) * row["target_points"] for row in rows]
    wins = sum(value > 0 for value in points)
    gross_mean = mean(points) if points else 0.0
    report = {
        "trades": len(points),
        "wins": wins,
        "win_rate": wins / len(points) if points else None,
        "win_rate_wilson95": _wilson(wins, len(points)),
        "gross_mean_points": gross_mean,
        "gross_median_points": median(points) if points else None,
        "gross_total_points": sum(points),
        "mean_without_best_points": _trimmed(points, max),
        "mean_without_worst_points": _trimmed(points, min),
        "best_points": max(points) if points else None,
        "worst_points": min(points) if points else None,
        "bootstrap_mean_ci95_points": bootstrap_mean_interval(points, seed=seed),
        "cost_scenarios": {
            str(cost): {
                "mean_net_points": mean(value - cost for value in points) if points else None,
                "total_net_points": sum(value - cost for value in points),
            }
            for cost in COST_POINTS
        },
    }
    return report


def compare_directions(rows: list[dict], *, seed: int) -> dict:
    fade = summarize_strategy(rows, lambda row: int(row["base_direction"]), seed=seed)
    fade_mean = fade["gross_mean_points"]
    return {
        "fade": fade,
        "continuation": summarize_strategy(
            rows, lambda row: -int(row["base_direction"]), seed=seed + 1
        ),
        "always_long": summarize_strategy(rows, lambda row: 1, seed=seed + 2),
        "always_short": summarize_strategy(rows, lambda row: -1, seed=seed + 3),
        "same_days_random_direction": random_direction_test(
            [row["target_points"] for row in rows], fade_mean, seed=seed + 4
        ),
        "blocked_gap_direction_permutation": blocked_direction_permutation(
            rows, fade_mean, seed=seed + 5
        ),
        "direction_agnostic_same_day_movement": {
            "mean_absolute_two_minute_points": mean(abs(row["target_points"]) for row in rows),
            "median_absolute_two_minute_points": median(abs(row["target_points"]) for row in rows),
            "fade_mean_as_fraction_of_mean_absolute_move": fade_mean
            / mean(abs(row["target_points"]) for row in rows),
        },
    }


def add_prior_volatility(rows: list[dict], rth_closes: dict[str, float]) -> list[dict]:
    """Reconstruct each row's prior cash close from the raw NQ source."""

    enriched = []
    ordered = sorted(rows, key=lambda row: row["day"])
    close_days = sorted(rth_closes)
    for row in ordered:
        previous_index = bisect.bisect_left(close_days, row["day"]) - 1
        if previous_index < 0:
            continue
        previous_day = close_days[previous_index]
        enriched.append({**row, "nq_prior_close": rth_closes[previous_day]})
    volatility = rolling_close_volatility(enriched)
    output = []
    for row in enriched:
        if row["day"] in volatility:
            output.append({**row, "prior_20d_volatility_pct": volatility[row["day"]]})
    return output


def match_ordinary_sessions(candidates: list[dict], all_rows: list[dict]) -> list[tuple[dict, dict]]:
    """Greedy unique nearest-volatility matches within calendar year."""

    candidate_days = {row["day"] for row in candidates}
    ordinary = [
        row
        for row in all_rows
        if row["day"] not in candidate_days and abs(row["nq_overnight_ret"]) < 1.0
    ]
    possible = []
    for candidate in candidates:
        year = candidate["day"][:4]
        for control in ordinary:
            if control["day"][:4] != year:
                continue
            distance = abs(
                math.log(
                    candidate["prior_20d_volatility_pct"]
                    / control["prior_20d_volatility_pct"]
                )
            )
            possible.append((distance, candidate["day"], control["day"], candidate, control))
    matches = []
    used_candidates: set[str] = set()
    used_controls: set[str] = set()
    for _, candidate_day, control_day, candidate, control in sorted(possible):
        if candidate_day in used_candidates or control_day in used_controls:
            continue
        matches.append((candidate, control))
        used_candidates.add(candidate_day)
        used_controls.add(control_day)
    return matches


def summarize_matched_volatility(
    matches: list[tuple[dict, dict]], *, seed: int, samples: int = DEFAULT_RANDOMIZATIONS
) -> dict:
    if not matches:
        return {"pairs": 0}
    differences = [
        abs(candidate["target_points"]) - abs(control["target_points"])
        for candidate, control in matches
    ]
    return {
        "pairs": len(matches),
        "unique_controls": len({control["day"] for _, control in matches}),
        "candidate_mean_prior_volatility_pct": mean(
            candidate["prior_20d_volatility_pct"] for candidate, _ in matches
        ),
        "control_mean_prior_volatility_pct": mean(
            control["prior_20d_volatility_pct"] for _, control in matches
        ),
        "candidate_mean_absolute_two_minute_points": mean(
            abs(candidate["target_points"]) for candidate, _ in matches
        ),
        "control_mean_absolute_two_minute_points": mean(
            abs(control["target_points"]) for _, control in matches
        ),
        "paired_mean_absolute_move_difference_points": mean(differences),
        "paired_difference_bootstrap_ci95_points": bootstrap_mean_interval(
            differences, seed=seed, samples=samples
        ),
    }


def load_two_minute_paths(
    path: Path, days: set[str], close_days: set[str] | None = None
) -> tuple[dict[str, dict], dict, dict[str, float]]:
    bars: dict[str, list[dict]] = defaultdict(list)
    closes: dict[str, float] = {}
    quality = defaultdict(int)
    close_days = close_days or set()
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            ts = datetime.fromtimestamp(int(row["hd"]["ts_event"]) / 1e9, timezone.utc)
            local = ts.astimezone(ET)
            day = local.date().isoformat()
            scale = 1_000_000_000
            # Retain every raw RTH close. Some roll-transition sessions are
            # correctly absent from the roll-clean target rows, but their
            # closing price is still the prior close for the next valid day.
            if (local.hour, local.minute) == (15, 59):
                closes[day] = int(row["close"]) / scale
                quality["retained_rth_closes"] += 1
            if (local.hour, local.minute) not in {(9, 30), (9, 31)}:
                continue
            if day not in days:
                continue
            bars[day].append(
                {
                    "minute": (local.hour, local.minute),
                    "open": int(row["open"]) / scale,
                    "high": int(row["high"]) / scale,
                    "low": int(row["low"]) / scale,
                    "close": int(row["close"]) / scale,
                }
            )
            quality["retained_bars"] += 1
    result = {}
    for day, day_bars in bars.items():
        if {bar["minute"] for bar in day_bars} != {(9, 30), (9, 31)}:
            quality["incomplete_days"] += 1
            continue
        day_bars.sort(key=lambda bar: bar["minute"])
        result[day] = {
            "entry": day_bars[0]["open"],
            "exit": day_bars[-1]["close"],
            "high": max(bar["high"] for bar in day_bars),
            "low": min(bar["low"] for bar in day_bars),
        }
    quality["complete_days"] = len(result)
    quality["rth_close_days"] = len(closes)
    return result, dict(quality), closes


def summarize_excursions(rows: list[dict], paths: dict[str, dict]) -> dict:
    samples = []
    mismatches = 0
    for row in rows:
        path = paths.get(row["day"])
        if path is None:
            continue
        direction = int(row["base_direction"])
        if abs((path["exit"] - path["entry"]) - row["target_points"]) > 1e-9:
            mismatches += 1
        if direction > 0:
            mfe = max(0.0, path["high"] - path["entry"])
            mae = max(0.0, path["entry"] - path["low"])
        else:
            mfe = max(0.0, path["entry"] - path["low"])
            mae = max(0.0, path["high"] - path["entry"])
        samples.append((mfe, mae))
    return {
        "sessions": len(samples),
        "target_mismatches": mismatches,
        "mean_mfe_points": mean(value[0] for value in samples) if samples else None,
        "median_mfe_points": median(value[0] for value in samples) if samples else None,
        "mean_mae_points": mean(value[1] for value in samples) if samples else None,
        "median_mae_points": median(value[1] for value in samples) if samples else None,
        "note": "Minute-bar MFE/MAE do not reveal intrabar event order or executable fills.",
    }


def exact_bbo_summary(rows: list[dict], exact_dir: Path) -> dict:
    by_day = {row["day"]: row for row in rows}
    seen = set()
    outcomes = []
    for path in sorted(exact_dir.glob("nq_opening_ofi_1s_*.csv")):
        match = re.search(r"(\d{4}-\d{2}-\d{2})\.csv$", path.name)
        if not match:
            continue
        day = match.group(1)
        if day in seen or day not in by_day:
            continue
        seen.add(day)
        entry = exit_row = None
        with path.open(newline="") as fh:
            for raw in csv.DictReader(fh):
                local = datetime.fromisoformat(raw["timestamp_utc"]).astimezone(ET)
                if (local.hour, local.minute, local.second) == (9, 30, 0):
                    entry = raw
                elif (local.hour, local.minute, local.second) == (9, 31, 59):
                    exit_row = raw
        if entry is None or exit_row is None:
            continue
        direction = int(by_day[day]["base_direction"])
        exact_mid = direction * (float(exit_row["close_mid"]) - float(entry["open_mid"]))
        crossing = (float(entry["mean_spread"]) + float(exit_row["mean_spread"])) / 2
        outcomes.append((exact_mid, exact_mid - crossing, crossing))
    after = [value[1] for value in outcomes]
    return {
        "sessions": len(outcomes),
        "wins_after_crossing": sum(value > 0 for value in after),
        "mean_exact_mid_points": mean(value[0] for value in outcomes) if outcomes else None,
        "mean_top_of_book_crossing_points": mean(value[2] for value in outcomes)
        if outcomes
        else None,
        "mean_after_crossing_points": mean(after) if outcomes else None,
        "mean_after_crossing_without_best": _trimmed(after, max),
        "note": "Crossing estimate excludes commissions, latency, and additional slippage.",
    }


def analyze(
    all_rows: list[dict],
    fair_rows: list[dict],
    *,
    normalized_threshold: float,
    paths: dict[str, dict] | None = None,
    rth_closes: dict[str, float] | None = None,
    exact_dir: Path | None = None,
) -> dict:
    if rth_closes is None:
        raise ValueError("rth_closes are required for volatility-matched controls")
    all_with_volatility = add_prior_volatility(all_rows, rth_closes)
    high = [row for row in fair_rows if row["normalized_gap"] >= normalized_threshold]
    samples = {
        "absolute_gap_at_least_1pct": fair_rows,
        "high_normalized_gap": high,
    }
    report = {
        "status": "RETROSPECTIVE_FALSIFICATION_NOT_UNTOUCHED_EDGE_PROOF",
        "target": "NQ 09:30 one-minute open through 09:31 one-minute close",
        "frozen_normalized_gap_threshold": normalized_threshold,
        "samples": {},
    }
    for index, (name, rows) in enumerate(samples.items()):
        n = len(rows)
        cut1, cut2 = n // 3, 2 * n // 3
        blocks = {
            "discovery": rows[:cut1],
            "validation": rows[cut1:cut2],
            "retrospective_confirmation": rows[cut2:],
        }
        sample_report = compare_directions(rows, seed=10_000 + index * 1_000)
        sample_report["chronological_fade"] = {
            block_name: summarize_strategy(
                block, lambda row: int(row["base_direction"]), seed=20_000 + index * 100 + j
            )
            for j, (block_name, block) in enumerate(blocks.items())
        }
        matches = match_ordinary_sessions(rows, all_with_volatility)
        sample_report["volatility_matched_ordinary_sessions"] = summarize_matched_volatility(
            matches, seed=30_000 + index
        )
        if paths is not None:
            sample_report["minute_bar_excursions"] = summarize_excursions(rows, paths)
        if exact_dir is not None:
            sample_report["exact_bbo_overlap"] = exact_bbo_summary(rows, exact_dir)
        report["samples"][name] = sample_report
    report["quality"] = {
        "all_sessions": len(all_rows),
        "all_sessions_with_prior_volatility": len(all_with_volatility),
        "absolute_gap_sessions": len(fair_rows),
        "high_normalized_gap_sessions": len(high),
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-sessions", type=Path, required=True)
    parser.add_argument("--fair-sessions", type=Path, required=True)
    parser.add_argument("--fair-report", type=Path, required=True)
    parser.add_argument("--nq", type=Path)
    parser.add_argument("--exact-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    all_rows = load_csv(args.all_sessions)
    fair_rows = load_csv(args.fair_sessions)
    fair_report = json.loads(args.fair_report.read_text())
    threshold = float(fair_report["frozen_thresholds"]["normalized_gap"])
    paths = quality = None
    closes: dict[str, float] = {}
    if args.nq:
        paths, quality, closes = load_two_minute_paths(
            args.nq,
            {row["day"] for row in fair_rows},
            {row["day"] for row in all_rows},
        )
    report = analyze(
        all_rows,
        fair_rows,
        normalized_threshold=threshold,
        paths=paths,
        rth_closes=closes,
        exact_dir=args.exact_dir,
    )
    if quality is not None:
        report["quality"]["minute_paths"] = quality
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"quality": report["quality"], "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
