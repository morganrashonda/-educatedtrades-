"""Research-only discovery evaluator for opening failed auctions.

Implements the state machine frozen in
docs/OPENING_FAILED_AUCTION_VIDEO_TEST_SPEC_20260818.md.  It reads historical
bars and already-derived MBP-1 features.  It has no production imports and
cannot place orders or write learning state.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
TICK = 0.25


@dataclass(frozen=True)
class Bar:
    ts: datetime
    high: float
    low: float
    close: float
    instrument_id: int


@dataclass(frozen=True)
class Level:
    price: float
    names: tuple[str, ...]
    eligible_index: int


@dataclass(frozen=True)
class Bucket:
    ts: datetime
    open_mid: float
    close_mid: float
    buy_volume: float
    sell_volume: float
    trade_imbalance: float | None
    depth_normalized_ofi: float | None
    spread: float


def _q(values: list[float], probability: float) -> float | None:
    """Deterministic linear quantile without a numerical dependency."""

    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _bar_rows(path: Path):
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            hd = row["hd"]
            yield Bar(
                ts=datetime.fromtimestamp(int(hd["ts_event"]) / 1e9, timezone.utc),
                high=int(row["high"]) / 1e9,
                low=int(row["low"]) / 1e9,
                close=int(row["close"]) / 1e9,
                instrument_id=int(hd["instrument_id"]),
            )


def build_levels(path: Path) -> tuple[dict[date, list[Level]], dict]:
    """Build point-in-time prior-RTH, overnight, and opening-range levels."""

    rth: dict[date, dict] = defaultdict(
        lambda: {"high": -math.inf, "low": math.inf, "last_id": None}
    )
    overnight: dict[date, dict] = defaultdict(
        lambda: {"high": -math.inf, "low": math.inf, "last_id": None}
    )
    opening_range: dict[date, dict] = defaultdict(
        lambda: {"high": -math.inf, "low": math.inf, "id": None}
    )
    open_instrument: dict[date, int] = {}
    rows = 0
    for bar in _bar_rows(path):
        rows += 1
        local = bar.ts.astimezone(ET)
        day = local.date()
        hm = (local.hour, local.minute)
        if (9, 30) <= hm < (16, 0):
            item = rth[day]
            item["high"] = max(item["high"], bar.high)
            item["low"] = min(item["low"], bar.low)
            item["last_id"] = bar.instrument_id
            if hm == (9, 30):
                open_instrument[day] = bar.instrument_id
            if (9, 30) <= hm < (9, 35):
                opening_range[day]["high"] = max(opening_range[day]["high"], bar.high)
                opening_range[day]["low"] = min(opening_range[day]["low"], bar.low)
                opening_range[day]["id"] = bar.instrument_id
        if hm >= (18, 0):
            target_day = day + timedelta(days=1)
            item = overnight[target_day]
            item["high"] = max(item["high"], bar.high)
            item["low"] = min(item["low"], bar.low)
            item["last_id"] = bar.instrument_id
        elif hm < (9, 30):
            item = overnight[day]
            item["high"] = max(item["high"], bar.high)
            item["low"] = min(item["low"], bar.low)
            item["last_id"] = bar.instrument_id

    rth_days = sorted(rth)
    prior = {day: rth[rth_days[i - 1]] for i, day in enumerate(rth_days) if i}
    result: dict[date, list[Level]] = {}
    excluded = defaultdict(int)
    for day in sorted(set(open_instrument) & set(prior) & set(overnight) & set(opening_range)):
        current_id = open_instrument[day]
        if prior[day]["last_id"] != current_id or overnight[day]["last_id"] != current_id:
            excluded["roll_or_instrument_mismatch"] += 1
            continue
        raw = [
            (prior[day]["high"], "prior_rth_high", 0),
            (prior[day]["low"], "prior_rth_low", 0),
            (overnight[day]["high"], "overnight_high", 0),
            (overnight[day]["low"], "overnight_low", 0),
            (opening_range[day]["high"], "opening_range_5m_high", 60),
            (opening_range[day]["low"], "opening_range_5m_low", 60),
        ]
        clusters: list[list[tuple[float, str, int]]] = []
        for item in sorted(raw):
            if clusters and abs(item[0] - mean(x[0] for x in clusters[-1])) <= 4 * TICK:
                clusters[-1].append(item)
            else:
                clusters.append([item])
        result[day] = [
            Level(
                price=mean(item[0] for item in cluster),
                names=tuple(sorted(item[1] for item in cluster)),
                eligible_index=max(item[2] for item in cluster),
            )
            for cluster in clusters
        ]
    return result, {"source_rows": rows, "sessions": len(result), "excluded": dict(excluded)}


def load_five_second(path: Path) -> list[Bucket]:
    """Aggregate one-second discovery features into non-overlapping 5s rows."""

    grouped: dict[int, list[dict]] = defaultdict(list)
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["timestamp_utc"])
            local = ts.astimezone(ET)
            if not ((9, 30) <= (local.hour, local.minute) < (10, 30)):
                continue
            key = int(ts.timestamp()) // 5 * 5
            grouped[key].append(row)
    out: list[Bucket] = []
    for key in sorted(grouped):
        rows = grouped[key]
        buy = sum(float(row["buy_volume"] or 0) for row in rows)
        sell = sum(float(row["sell_volume"] or 0) for row in rows)
        total = buy + sell
        depths = [float(row["mean_depth"]) for row in rows if row["mean_depth"]]
        ofi = sum(float(row["ofi"] or 0) for row in rows)
        spreads = [float(row["mean_spread"]) for row in rows if row["mean_spread"]]
        out.append(
            Bucket(
                ts=datetime.fromtimestamp(key, timezone.utc),
                open_mid=float(rows[0]["open_mid"]),
                close_mid=float(rows[-1]["close_mid"]),
                buy_volume=buy,
                sell_volume=sell,
                trade_imbalance=(buy - sell) / total if total else None,
                depth_normalized_ofi=ofi / mean(depths) if depths and mean(depths) else None,
                spread=mean(spreads) if spreads else 0.0,
            )
        )
    return out


def _window_metrics(rows: list[Bucket], index: int, side: int) -> tuple[float, float]:
    window = rows[max(0, index - 1) : index + 1]
    effort = sum(item.buy_volume if side > 0 else item.sell_volume for item in window)
    start, end = window[0].open_mid, window[-1].close_mid
    progress = max(side * (end - start), 0.0)
    return effort, progress / effort if effort else math.inf


def _inside(price: float, level: float, side: int, ticks: int) -> bool:
    return side * (price - level) <= -ticks * TICK


def _allowed_sides(level: Level) -> tuple[int, ...]:
    high = any(name.endswith("high") for name in level.names)
    low = any(name.endswith("low") for name in level.names)
    if high and not low:
        return (1,)
    if low and not high:
        return (-1,)
    return (1, -1)


def _result(rows: list[Bucket], decision: int, direction: int, horizon: int) -> dict | None:
    entry_index = decision + 1
    exit_index = entry_index + horizon // 5
    if exit_index >= len(rows):
        return None
    entry_row, exit_row = rows[entry_index], rows[exit_index]
    entry = entry_row.open_mid + direction * entry_row.spread / 2.0
    exit_price = exit_row.close_mid - direction * exit_row.spread / 2.0
    crossing_points = direction * (exit_price - entry)
    return {
        "entry_timestamp": entry_row.ts.isoformat(),
        "entry": entry,
        "exit": exit_price,
        "crossing_points": crossing_points,
        "one_tick_each_side_points": crossing_points - 2 * TICK,
    }


def detect_level(
    rows: list[Bucket],
    level: Level,
    baseline: dict[tuple[int, int], dict[str, list[float]]],
    *,
    minimum_baseline: int = 20,
) -> list[dict]:
    """Detect cooldown-separated nested failed-auction states at one level."""

    events: list[dict] = []
    index = max(level.eligible_index, 1)
    while index < len(rows):
        found: tuple[int, int] | None = None
        for i in range(index, len(rows)):
            for side in _allowed_sides(level):
                threshold = level.price + side * TICK
                if side * rows[i - 1].close_mid < side * threshold <= side * rows[i].close_mid:
                    found = (i, side)
                    break
            if found:
                break
        if not found:
            break
        break_index, side = found
        event = {
            "level": level.price,
            "level_names": list(level.names),
            "break_side": side,
            "break_timestamp": rows[break_index].ts.isoformat(),
            "stage": "bare_break",
            "absorption_index": None,
            "shift_reclaim_index": None,
            "full_sequence_index": None,
        }
        extreme = rows[break_index].close_mid
        absorption = None
        max_absorption_index = min(len(rows) - 1, break_index + 6)
        for j in range(break_index, max_absorption_index + 1):
            extreme = max(extreme, rows[j].close_mid) if side > 0 else min(extreme, rows[j].close_mid)
            seconds = rows[j].ts.astimezone(ET).hour * 3600 + rows[j].ts.astimezone(ET).minute * 60 + rows[j].ts.astimezone(ET).second
            key = (seconds, side)
            pool = baseline.get(key, {})
            efforts = pool.get("effort", [])
            results = pool.get("result", [])
            if len(efforts) < minimum_baseline or len(results) < minimum_baseline:
                continue
            effort, progress = _window_metrics(rows, j, side)
            effort_threshold = _q(efforts, 0.75)
            result_threshold = _q(results, 0.25)
            if (
                effort_threshold is not None
                and result_threshold is not None
                and effort >= effort_threshold
                and progress <= result_threshold
            ):
                absorption = j
                event["stage"] = "absorption"
                event["absorption_index"] = j
                event["effort"] = effort
                event["progress_per_contract"] = progress
                break

        reclaim = None
        reclaim_end = min(len(rows) - 1, break_index + 12)
        for j in range(break_index, reclaim_end + 1):
            if _inside(rows[j].close_mid, level.price, side, 1):
                reclaim = j
                break
        if reclaim is not None:
            event["failed_break_reclaim_index"] = reclaim

        shift = None
        if absorption is not None:
            shift_end = min(len(rows) - 1, absorption + 12)
            for j in range(absorption + 1, shift_end + 1):
                row = rows[j]
                if (
                    row.trade_imbalance is not None
                    and side * row.trade_imbalance <= -0.20
                    and row.depth_normalized_ofi is not None
                    and side * row.depth_normalized_ofi < 0
                    and _inside(row.close_mid, level.price, side, 1)
                ):
                    shift = j
                    event["stage"] = "shift_reclaim"
                    event["shift_reclaim_index"] = j
                    break

        full = None
        if shift is not None:
            retest_seen = False
            retest_end = min(len(rows) - 1, shift + 24)
            for j in range(shift + 1, retest_end + 1):
                row = rows[j]
                if side * (row.close_mid - extreme) > TICK:
                    break
                if abs(row.close_mid - level.price) <= 2 * TICK:
                    retest_seen = True
                    continue
                if retest_seen and _inside(row.close_mid, level.price, side, 2):
                    full = j
                    event["stage"] = "full_sequence"
                    event["full_sequence_index"] = j
                    break
            event["retest_seen"] = retest_seen

        decisions = {
            "bare_break": break_index,
            "failed_break_reclaim": reclaim,
            "absorption": absorption,
            "shift_reclaim": shift,
            "full_sequence": full,
        }
        event["outcomes"] = {}
        for name, decision in decisions.items():
            if decision is None:
                continue
            event["outcomes"][name] = {
                str(horizon): _result(rows, decision, -side, horizon)
                for horizon in (120, 300, 900)
            }
        events.append(event)
        index = break_index + 60  # frozen 300-second cooldown in 5s buckets
    return events


def _add_baseline(rows: list[Bucket], baseline: dict) -> None:
    for index in range(1, len(rows)):
        local = rows[index].ts.astimezone(ET)
        seconds = local.hour * 3600 + local.minute * 60 + local.second
        for side in (1, -1):
            effort, result = _window_metrics(rows, index, side)
            if effort:
                baseline[(seconds, side)]["effort"].append(effort)
                baseline[(seconds, side)]["result"].append(result)


def summarize(events: list[dict]) -> dict:
    stages = ("bare_break", "failed_break_reclaim", "absorption", "shift_reclaim", "full_sequence")
    result = {}
    for stage in stages:
        stage_events = [event for event in events if stage in event.get("outcomes", {})]
        horizons = {}
        for horizon in (120, 300, 900):
            values = [
                event["outcomes"][stage][str(horizon)]["one_tick_each_side_points"]
                for event in stage_events
                if event["outcomes"][stage][str(horizon)] is not None
            ]
            by_day: dict[str, list[float]] = defaultdict(list)
            for event in stage_events:
                outcome = event["outcomes"][stage][str(horizon)]
                if outcome is not None:
                    by_day[event["day"]].append(outcome["one_tick_each_side_points"])
            bootstrap = []
            days = sorted(by_day)
            if days:
                rng = random.Random(f"failed-auction:{stage}:{horizon}")
                for _ in range(5000):
                    sample = [rng.choice(days) for _ in days]
                    draw = [value for day in sample for value in by_day[day]]
                    bootstrap.append(mean(draw))
            horizons[str(horizon)] = {
                "n": len(values),
                "independent_sessions": len(days),
                "wins": sum(value > 0 for value in values),
                "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
                "mean_points": mean(values) if values else None,
                "median_points": median(values) if values else None,
                "session_bootstrap_95_ci": (
                    [_q(bootstrap, 0.025), _q(bootstrap, 0.975)] if bootstrap else None
                ),
            }
        result[stage] = {"events": len(stage_events), "horizons": horizons}
    return result


def run(nq_bars: Path, feature_dir: Path) -> dict:
    levels, level_quality = build_levels(nq_bars)
    dated_files = []
    for path in feature_dir.glob("nq_opening_ofi_1s_*.csv"):
        suffix = path.stem.removeprefix("nq_opening_ofi_1s_").removeprefix("pilot_")
        try:
            dated_files.append((date.fromisoformat(suffix), path))
        except ValueError:
            continue
    dated_files.sort()
    baseline = defaultdict(lambda: defaultdict(list))
    events: list[dict] = []
    sessions = []
    for day, path in dated_files:
        rows = load_five_second(path)
        if day not in levels:
            sessions.append({"day": str(day), "status": "NO_LEVELS", "rows": len(rows)})
            _add_baseline(rows, baseline)
            continue
        day_events = []
        for level in levels[day]:
            day_events.extend(detect_level(rows, level, baseline))
        for event in day_events:
            event["day"] = str(day)
        events.extend(day_events)
        sessions.append({"day": str(day), "status": "OK", "rows": len(rows), "events": len(day_events)})
        _add_baseline(rows, baseline)
    return {
        "status": "DISCOVERY_ONLY",
        "spec": "docs/OPENING_FAILED_AUCTION_VIDEO_TEST_SPEC_20260818.md",
        "level_quality": level_quality,
        "feature_files": len(dated_files),
        "sessions": sessions,
        "summary": summarize(events),
        "events": events,
        "limitations": [
            "May-August 2026 feature dates are contaminated discovery data.",
            "Derived 1-second rows approximate executable BBO from mid and mean spread.",
            "Validation requires newly acquired raw MBP-1 on preselected older dates.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nq-bars", type=Path, required=True)
    parser.add_argument("--feature-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.nq_bars, args.feature_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
