"""Stability and acceptance-gate sensitivity for opening microstructure research.

This is retrospective, multiple-tested research.  It does not authorize a
production strategy, Tier 3 execution, learning writes, or orders.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

import numpy as np

from backend.research.opening_microstructure_analysis import EXTRA_COST_POINTS, signal_metrics


def load_rows(path: Path) -> list[dict]:
    with path.open() as source:
        return [json.loads(line) for line in source if line.strip()]


def _side(value: float | None) -> int:
    if value is None or value == 0:
        return 0
    return 1 if value > 0 else -1


def _executable(row: dict, side: int) -> float:
    return float(row["long_executable_points"] if side > 0 else row["short_executable_points"])


def _metrics(rows: list[dict], sides: list[int], universe: int) -> dict:
    selected = [(row, side) for row, side in zip(rows, sides) if side]
    if not selected:
        return {"observations": 0, "coverage": 0.0}
    kept_rows = [item[0] for item in selected]
    kept_sides = np.asarray([item[1] for item in selected])
    report = signal_metrics(kept_rows, kept_sides)
    report["coverage"] = len(kept_rows) / universe
    by_year = {}
    for year in sorted({row["session_date"][:4] for row in kept_rows}):
        indices = [index for index, row in enumerate(kept_rows) if row["session_date"].startswith(year)]
        year_rows = [kept_rows[index] for index in indices]
        year_sides = kept_sides[indices]
        by_year[year] = signal_metrics(year_rows, year_sides)
    report["by_year"] = by_year
    report["equal_year_weighted"] = {
        "gross_executable_mean_points": mean(
            item["gross_executable_mean_points"] for item in by_year.values()
        ),
        "cost_stress": {
            str(cost): {
                "mean_net_points": mean(
                    item["cost_stress"][str(cost)]["mean_net_points"]
                    for item in by_year.values()
                ),
                "positive_years": sum(
                    item["cost_stress"][str(cost)]["mean_net_points"] > 0
                    for item in by_year.values()
                ),
                "years": len(by_year),
            }
            for cost in EXTRA_COST_POINTS
        },
    }
    report["gross_points_by_year"] = {
        year: sum(
            _executable(row, side)
            for row, side in selected
            if row["session_date"].startswith(year)
        )
        for year in by_year
    }
    latest_year = max(by_year)
    earlier_indices = [
        index for index, row in enumerate(kept_rows)
        if not row["session_date"].startswith(latest_year)
    ]
    report["excluding_latest_year"] = {
        "excluded_year": latest_year,
        **signal_metrics(
            [kept_rows[index] for index in earlier_indices],
            kept_sides[earlier_indices],
        ),
    }
    return report


def _flow_side(row: dict) -> int:
    return _side(row.get("preopen_trades_signed_trade_imbalance"))


def _agrees(row: dict, field: str, side: int) -> bool:
    return side != 0 and _side(row.get(field)) == side


def analyze(rows: list[dict]) -> dict:
    base_universe = [
        row for row in rows
        if int(row["decision_seconds"]) == 0 and int(row["horizon_seconds"]) == 120
    ]
    base = [row for row in base_universe if int(row["direction"]) != 0]
    base_sides = [_flow_side(row) for row in base]
    contemporaneous = {
        "preopen_flow_all": _metrics(base, base_sides, len(base_universe)),
        "flow_plus_preopen_price_agreement": _metrics(
            base,
            [side if _agrees(row, "preopen_bbo_return_bps", side) else 0
             for row, side in zip(base, base_sides)],
            len(base_universe),
        ),
        "flow_plus_preopen_mbp_ofi_agreement": _metrics(
            base,
            [side if _agrees(row, "preopen_mbp_depth_normalized_ofi", side) else 0
             for row, side in zip(base, base_sides)],
            len(base_universe),
        ),
        "flow_plus_preopen_microprice_agreement": _metrics(
            base,
            [side if _agrees(row, "preopen_mbp_mean_microprice_displacement_points", side)
             else 0 for row, side in zip(base, base_sides)],
            len(base_universe),
        ),
        "flow_plus_preopen_refill_pressure_agreement": _metrics(
            base,
            [
                side if _side(
                    (row.get("preopen_mbp_bid_refill_ratio") or 0)
                    - (row.get("preopen_mbp_ask_refill_ratio") or 0)
                ) == side else 0
                for row, side in zip(base, base_sides)
            ],
            len(base_universe),
        ),
    }

    opening_confirmation = {}
    for decision in (5, 10, 30, 60):
        decision_universe = [
            row for row in rows
            if int(row["decision_seconds"]) == decision
            and int(row["horizon_seconds"]) == 120
        ]
        selected = [row for row in decision_universe if int(row["direction"]) != 0]
        sides = [_flow_side(row) for row in selected]
        gates = {
            "opening_price": "observed_bbo_return_bps",
            "opening_aggressive_flow": "observed_trades_signed_trade_imbalance",
            "opening_mbp_ofi": "observed_mbp_depth_normalized_ofi",
        }
        decision_report = {
            "preopen_flow_no_gate": _metrics(selected, sides, len(decision_universe)),
        }
        for name, field in gates.items():
            decision_report[f"preopen_flow_plus_{name}"] = _metrics(
                selected,
                [side if _agrees(row, field, side) else 0
                 for row, side in zip(selected, sides)],
                len(decision_universe),
            )
        decision_report["preopen_flow_plus_all_three"] = _metrics(
            selected,
            [
                side if all(_agrees(row, field, side) for field in gates.values()) else 0
                for row, side in zip(selected, sides)
            ],
            len(decision_universe),
        )
        opening_confirmation[str(decision)] = decision_report

    return {
        "status": "retrospective_exploratory_sensitivity_only",
        "sessions": len(base_universe),
        "directional_sessions": len(base),
        "contemporaneous_preopen_filters": contemporaneous,
        "opening_confirmation_gates": opening_confirmation,
        "interpretation_limits": [
            "These filters were evaluated after related opening research and are not pristine OOS tests.",
            "Many gates are reported together; the best result must not be selected as proven edge.",
            "Agreement is association, not proof that order flow caused the subsequent move.",
            "A survivor requires a frozen shadow-forward test before Tier 3 consideration.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(load_rows(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"sessions": report["sessions"], "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
