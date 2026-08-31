"""Frozen pilot analysis for the NQ opening OFI hypothesis.

The rule is research-only and was frozen after inspecting 2026-08-10 through
2026-08-17. Later files can evaluate it, but must not be used to retune the
thresholds and then be reported as holdout evidence.
"""

from __future__ import annotations

import argparse
import csv
import random
import statistics
from datetime import datetime, time
from pathlib import Path


OFI_THRESHOLD = 104.18738
REFILL_THRESHOLD = 0.1954
OPEN_START_UTC = time(13, 30)
OPEN_END_UTC = time(13, 40)


def _number(value: str):
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return value


def load_rows(path: Path) -> list[dict]:
    with path.open() as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        row["dt"] = datetime.fromisoformat(row["timestamp_utc"])
        for key, value in list(row.items()):
            if key not in {"timestamp_utc", "decision_timestamp_utc", "dt"}:
                row[key] = _number(value)
    return rows


def build_five_second_features(rows: list[dict]) -> list[dict]:
    """Create non-overlapping five-second decision buckets."""

    by_ns = {int(row["bucket_start_ns"]): row for row in rows}
    result = []
    for current in rows:
        if not (OPEN_START_UTC <= current["dt"].time() < OPEN_END_UTC):
            continue
        current_ns = int(current["bucket_start_ns"])
        if (current_ns // 1_000_000_000) % 5 != 4:
            continue
        window = [
            by_ns.get(current_ns - offset * 1_000_000_000)
            for offset in range(4, -1, -1)
        ]
        if any(row is None for row in window):
            continue
        open_mid = window[0]["open_mid"]
        close_mid = current["close_mid"]
        if not open_mid or not close_mid or close_mid == open_mid:
            continue
        direction = 1 if close_mid > open_mid else -1
        depths = [row["mean_depth"] for row in window if row["mean_depth"] is not None]
        if not depths or statistics.mean(depths) == 0:
            continue
        mean_depth = statistics.mean(depths)
        bid_refill = sum(row["bid_queue_add"] for row in window)
        bid_attack = sum(row["sell_volume"] + row["bid_queue_remove"] for row in window)
        ask_refill = sum(row["ask_queue_add"] for row in window)
        ask_attack = sum(row["buy_volume"] + row["ask_queue_remove"] for row in window)
        refill_direction = (
            (bid_refill / bid_attack if bid_attack else 0.0)
            - (ask_refill / ask_attack if ask_attack else 0.0)
        )
        item = dict(current)
        item["direction"] = direction
        item["aligned_ofi"] = (
            direction * sum(row["ofi"] for row in window) / mean_depth
        )
        item["aligned_refill"] = direction * refill_direction
        item["mean_spread_5s"] = statistics.mean(
            row["mean_spread"] for row in window if row["mean_spread"] is not None
        )
        result.append(item)
    return result


def select_signals(
    rows: list[dict],
    *,
    ofi_threshold: float = OFI_THRESHOLD,
    refill_threshold: float = REFILL_THRESHOLD,
    cooldown_seconds: int = 120,
) -> list[dict]:
    selected = []
    last_ns = -(10**30)
    for row in rows:
        current_ns = int(row["bucket_start_ns"])
        if row["aligned_ofi"] < ofi_threshold or row["aligned_refill"] < refill_threshold:
            continue
        if current_ns - last_ns < cooldown_seconds * 1_000_000_000:
            continue
        selected.append(row)
        last_ns = current_ns
    return selected


def cluster_bootstrap_interval(values: list[float], *, samples: int = 20_000) -> tuple[float, float]:
    """Deterministic percentile interval from session-level resampling."""

    if not values:
        raise ValueError("cluster bootstrap requires at least one session")
    rng = random.Random(0)
    estimates = sorted(
        statistics.mean(rng.choices(values, k=len(values))) for _ in range(samples)
    )
    return estimates[int(0.025 * samples)], estimates[int(0.975 * samples)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--horizon", type=int, default=120)
    args = parser.parse_args()
    target = f"forward_return_{args.horizon}s"
    all_returns = []
    all_after_spread = []
    daily_after_spread = []
    for path in args.inputs:
        second_rows = load_rows(path)
        second_by_ns = {int(row["bucket_start_ns"]): row for row in second_rows}
        rows = build_five_second_features(second_rows)
        signals = [row for row in select_signals(rows) if row.get(target) is not None]
        returns = [row["direction"] * row[target] * 10_000 for row in signals]
        after_spread = []
        for row in signals:
            exit_row = second_by_ns.get(
                int(row["bucket_start_ns"]) + args.horizon * 1_000_000_000
            )
            if not exit_row or exit_row.get("mean_spread") is None:
                continue
            gross_points = row["direction"] * (
                exit_row["close_mid"] - row["close_mid"]
            )
            crossing_points = (row["mean_spread_5s"] + exit_row["mean_spread"]) / 2
            after_spread.append(gross_points - crossing_points)
        all_returns.extend(returns)
        all_after_spread.extend(after_spread)
        if after_spread:
            daily_after_spread.append(statistics.mean(after_spread))
        day = rows[0]["dt"].date().isoformat() if rows else path.name
        mean = statistics.mean(returns) if returns else float("nan")
        print(
            f"{day} signals={len(returns)} wins={sum(value > 0 for value in returns)} "
            f"mean_bp={mean:+.3f} "
            f"after_spread_points={statistics.mean(after_spread):+.3f}"
        )
    if all_returns:
        lower, upper = cluster_bootstrap_interval(daily_after_spread)
        print(
            f"TOTAL signals={len(all_returns)} wins={sum(value > 0 for value in all_returns)} "
            f"mean_bp={statistics.mean(all_returns):+.3f} "
            f"median_bp={statistics.median(all_returns):+.3f} "
            f"after_spread_points={statistics.mean(all_after_spread):+.3f} "
            f"session_bootstrap_95=[{lower:+.3f},{upper:+.3f}]"
        )
    else:
        print("TOTAL signals=0")


if __name__ == "__main__":
    main()
