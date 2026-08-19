"""Untouched ES confirmation of the frozen NQ two-minute gap direction.

Research only: this module reads historical bars and writes a report. It has
no production imports, broker access, learning writes, or order path.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import numpy as np

try:
    from .opening_direction_falsification import compare_directions, summarize_strategy
    from .opening_fair_value import rolling_close_volatility
except ImportError:
    from opening_direction_falsification import compare_directions, summarize_strategy
    from opening_fair_value import rolling_close_volatility


ET = ZoneInfo("America/New_York")
INHERITED_NORMALIZED_GAP_THRESHOLD = 1.170437
MINIMUM_CONFIRMATION_TRADES = 30


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    instrument_id: int


@dataclass
class Session:
    marks: dict[tuple[int, int], Bar] = field(default_factory=dict)

    def add(self, local: datetime, bar: Bar) -> None:
        self.marks[(local.hour, local.minute)] = bar


def _valid(bar: Bar) -> bool:
    return bar.low > 0 and bar.low <= min(bar.open, bar.close) <= max(
        bar.open, bar.close
    ) <= bar.high


def _next_day(day: date) -> date:
    return day + timedelta(days=1)


def load_sessions(path: Path) -> tuple[dict[date, Session], dict[date, Session], dict]:
    pre: dict[date, Session] = defaultdict(Session)
    rth: dict[date, Session] = defaultdict(Session)
    quality = defaultdict(int)
    with path.open() as fh:
        for line in fh:
            quality["source_rows"] += 1
            raw = json.loads(line)
            scale = 1_000_000_000
            ts = datetime.fromtimestamp(int(raw["hd"]["ts_event"]) / 1e9, timezone.utc)
            bar = Bar(
                ts=ts,
                open=int(raw["open"]) / scale,
                high=int(raw["high"]) / scale,
                low=int(raw["low"]) / scale,
                close=int(raw["close"]) / scale,
                volume=float(raw["volume"]),
                instrument_id=int(raw["hd"]["instrument_id"]),
            )
            if not _valid(bar):
                quality["invalid_rows"] += 1
                continue
            local = ts.astimezone(ET)
            hm = (local.hour, local.minute)
            if (9, 30) <= hm < (16, 0):
                rth[local.date()].add(local, bar)
            if hm >= (18, 0):
                pre[_next_day(local.date())].add(local, bar)
            elif hm <= (9, 28):
                pre[local.date()].add(local, bar)
    quality["pre_sessions"] = len(pre)
    quality["rth_sessions"] = len(rth)
    return dict(pre), dict(rth), dict(quality)


def _pct(end: float, start: float) -> float:
    return (end / start - 1.0) * 100.0


def build_records(pre: dict[date, Session], rth: dict[date, Session]) -> tuple[list[dict], dict]:
    output = []
    excluded = defaultdict(int)
    rth_days = sorted(rth)
    previous = {day: rth_days[index - 1] for index, day in enumerate(rth_days) if index}
    for day in sorted(set(pre) & set(rth)):
        if day not in previous:
            excluded["no_prior_rth_session"] += 1
            continue
        prior = rth[previous[day]]
        current_pre = pre[day]
        current_rth = rth[day]
        if (9, 28) not in current_pre.marks:
            excluded["missing_0928"] += 1
            continue
        if not {(9, 30), (9, 31)}.issubset(current_rth.marks):
            excluded["missing_two_minute_target"] += 1
            continue
        if (15, 59) not in prior.marks:
            excluded["missing_prior_close"] += 1
            continue
        pre_bar = current_pre.marks[(9, 28)]
        prior_close_bar = prior.marks[(15, 59)]
        if pre_bar.instrument_id != prior_close_bar.instrument_id:
            excluded["continuous_contract_roll_transition"] += 1
            continue
        prior_close = prior_close_bar.close
        entry = current_rth.marks[(9, 30)].open
        exit_price = current_rth.marks[(9, 31)].close
        output.append(
            {
                "day": day.isoformat(),
                "nq_prior_close": prior_close,
                "entry": entry,
                "exit": exit_price,
                "target_points": exit_price - entry,
                "overnight_gap_pct": _pct(pre_bar.close, prior_close),
                "instrument_id": pre_bar.instrument_id,
            }
        )
    volatility = rolling_close_volatility(output)
    records = []
    for row in output:
        if row["day"] not in volatility:
            excluded["insufficient_prior_volatility"] += 1
            continue
        gap = row["overnight_gap_pct"]
        records.append(
            {
                **row,
                "base_direction": -1 if gap > 0 else 1 if gap < 0 else 0,
                "absolute_gap_pct": abs(gap),
                "prior_20d_volatility_pct": volatility[row["day"]],
                "normalized_gap": abs(gap) / volatility[row["day"]],
            }
        )
    return records, {"built_before_volatility": len(output), "usable": len(records), "excluded": dict(excluded)}


def confirmation_decision(sample: dict, halves: dict[str, dict]) -> dict:
    fade = sample["fade"]
    checks = {
        "minimum_30_trades": fade["trades"] >= MINIMUM_CONFIRMATION_TRADES,
        "positive_gross_mean": fade["gross_mean_points"] > 0,
        "bootstrap_lower_above_zero": fade["bootstrap_mean_ci95_points"][0] > 0,
        "random_direction_p_below_0_05": sample["same_days_random_direction"][
            "one_sided_p_value"
        ] < 0.05,
        "positive_after_one_point": fade["cost_scenarios"]["1.0"]["mean_net_points"] > 0,
        "positive_without_best": fade["mean_without_best_points"] > 0,
        "both_chronological_halves_positive": all(
            report["gross_mean_points"] > 0 for report in halves.values()
        ),
    }
    if not checks["minimum_30_trades"]:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "PASS" if all(checks.values()) else "FAIL"
    return {"status": status, "checks": checks}


def analyze(records: list[dict], threshold: float = INHERITED_NORMALIZED_GAP_THRESHOLD) -> dict:
    absolute_gap = [row for row in records if row["absolute_gap_pct"] >= 1.0]
    high = [row for row in absolute_gap if row["normalized_gap"] >= threshold]
    split = len(high) // 2
    halves_rows = {"first_half": high[:split], "second_half": high[split:]}
    primary = compare_directions(high, seed=41_000)
    halves = {
        name: summarize_strategy(rows, lambda row: int(row["base_direction"]), seed=42_000 + i)
        for i, (name, rows) in enumerate(halves_rows.items())
    }
    decision = confirmation_decision(primary, halves)
    return {
        "status": "UNTOUCHED_ES_CROSS_INSTRUMENT_CONFIRMATION",
        "instrument": "ES.v.0 volume-based continuous front contract",
        "target": "ES 09:30 one-minute open through 09:31 one-minute close",
        "inherited_rules": {
            "minimum_absolute_gap_pct": 1.0,
            "minimum_normalized_gap": threshold,
            "volatility_window": 20,
            "cost_points": 1.0,
        },
        "quality": {
            "usable_roll_clean_sessions": len(records),
            "absolute_gap_sessions": len(absolute_gap),
            "primary_high_normalized_gap_sessions": len(high),
            "first_day": records[0]["day"] if records else None,
            "last_day": records[-1]["day"] if records else None,
        },
        "primary_high_normalized_gap": primary,
        "chronological_halves": halves,
        "secondary_absolute_gap_at_least_1pct": compare_directions(
            absolute_gap, seed=43_000
        ),
        "decision": decision,
        "limitations": [
            "OHLCV bars are not exact executable BBO prices.",
            "The ES test confirms or rejects direction transfer; it does not validate NQ execution.",
            "No macro, absorption, stop, target, or threshold variant is permitted in this gate.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--es", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pre, rth, source_quality = load_sessions(args.es)
    records, build_quality = build_records(pre, rth)
    report = analyze(records)
    report["source_quality"] = source_quality
    report["build_quality"] = build_quality
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "decision": report["decision"],
                "quality": report["quality"],
                "output": str(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
