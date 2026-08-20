"""Walk-forward analysis of leakage-safe opening microstructure rows.

All results are retrospective research.  This module does not authorize a
production strategy, Tier 3 execution, learning writes, or orders.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean, median

import numpy as np


PREOPEN_FEATURES = (
    "preopen_bbo_return_bps",
    "preopen_trades_signed_trade_imbalance",
    "preopen_bbo_mean_queue_imbalance",
    "preopen_bbo_last_queue_imbalance",
    "preopen_bbo_mean_microprice_displacement_points",
    "preopen_bbo_mean_spread_points",
    "preopen_bbo_mean_depth",
)
OBSERVED_FEATURES = (
    "observed_bbo_return_bps",
    "observed_trades_signed_trade_imbalance",
    "observed_bbo_mean_queue_imbalance",
    "observed_bbo_last_queue_imbalance",
    "observed_bbo_mean_microprice_displacement_points",
    "observed_bbo_mean_spread_points",
    "observed_bbo_mean_depth",
)
PREOPEN_MBP_FEATURES = (
    "preopen_mbp_signed_trade_imbalance",
    "preopen_mbp_depth_normalized_ofi",
    "preopen_mbp_mean_queue_imbalance",
    "preopen_mbp_mean_microprice_displacement_points",
    "preopen_mbp_bid_refill_ratio",
    "preopen_mbp_ask_refill_ratio",
    "preopen_mbp_cancel_imbalance",
    "preopen_mbp_addition_imbalance",
)
OBSERVED_MBP_FEATURES = (
    "observed_mbp_signed_trade_imbalance",
    "observed_mbp_depth_normalized_ofi",
    "observed_mbp_mean_queue_imbalance",
    "observed_mbp_mean_microprice_displacement_points",
    "observed_mbp_bid_refill_ratio",
    "observed_mbp_ask_refill_ratio",
    "observed_mbp_cancel_imbalance",
    "observed_mbp_addition_imbalance",
)
EXTRA_COST_POINTS = (0.25, 0.75, 1.25, 2.25)
SEED = 260819


def load_rows(path: Path) -> list[dict]:
    with path.open() as source:
        return [json.loads(line) for line in source if line.strip()]


def chronological_folds(days: list[str]) -> list[tuple[set[str], set[str]]]:
    unique = sorted(set(days))
    if len(unique) < 10:
        raise ValueError("at least ten sessions are required")
    cuts = [int(len(unique) * fraction) for fraction in (0.4, 0.6, 0.8)]
    return [
        (set(unique[:cuts[0]]), set(unique[cuts[0]:cuts[1]])),
        (set(unique[:cuts[1]]), set(unique[cuts[1]:cuts[2]])),
        (set(unique[:cuts[2]]), set(unique[cuts[2]:])),
    ]


def _matrix(rows: list[dict], features: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
    values = np.array(
        [[float(row[name]) if row.get(name) is not None else np.nan for name in features]
         for row in rows],
        dtype=float,
    )
    labels = np.array([1.0 if row["direction"] > 0 else 0.0 for row in rows])
    return values, labels


def _prepare(
    train_x: np.ndarray, test_x: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    medians = np.nanmedian(train_x, axis=0)
    medians = np.where(np.isfinite(medians), medians, 0.0)
    train = np.where(np.isfinite(train_x), train_x, medians)
    test = np.where(np.isfinite(test_x), test_x, medians)
    # MBP refill and OFI ratios can be heavy-tailed. Bounds are learned from
    # training only and applied unchanged to the later block.
    lower = np.quantile(train, 0.01, axis=0)
    upper = np.quantile(train, 0.99, axis=0)
    train = np.clip(train, lower, upper)
    test = np.clip(test, lower, upper)
    center = train.mean(axis=0)
    scale = train.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    return (train - center) / scale, (test - center) / scale, center, scale


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -35, 35)
    return 1.0 / (1.0 + np.exp(-clipped))


def fit_logistic(
    x: np.ndarray,
    y: np.ndarray,
    *,
    l2: float = 1.0,
    iterations: int = 1_500,
    learning_rate: float = 0.08,
) -> tuple[float, np.ndarray]:
    """Fit deterministic L2 logistic regression by batch gradient descent."""

    if not len(x) or len(np.unique(y)) < 2:
        raise ValueError("training data must contain both directions")
    weights = np.zeros(x.shape[1], dtype=float)
    intercept = math.log((y.mean() + 1e-6) / (1 - y.mean() + 1e-6))
    for _ in range(iterations):
        probability = _sigmoid(intercept + x @ weights)
        error = probability - y
        intercept -= learning_rate * float(error.mean())
        gradient = x.T @ error / len(x) + l2 * weights / len(x)
        weights -= learning_rate * gradient
    return intercept, weights


def _auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    positives = scores[labels == 1]
    negatives = scores[labels == 0]
    if not len(positives) or not len(negatives):
        return None
    # Mann-Whitney form with half credit for tied scores.
    comparisons = (positives[:, None] > negatives[None, :]).sum()
    ties = (positives[:, None] == negatives[None, :]).sum()
    return float((comparisons + 0.5 * ties) / (len(positives) * len(negatives)))


def _bootstrap_ci(values: list[float], *, samples: int = 10_000) -> list[float] | None:
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    rng = np.random.default_rng(SEED)
    indices = rng.integers(0, len(array), size=(samples, len(array)))
    estimates = np.sort(array[indices].mean(axis=1))
    return [
        float(estimates[int(samples * 0.025)]),
        float(estimates[int(samples * 0.975)]),
    ]


def signal_metrics(rows: list[dict], sides: np.ndarray) -> dict:
    labels = np.array([1 if row["direction"] > 0 else 0 for row in rows])
    predictions = (sides > 0).astype(int)
    positive_recall = float((predictions[labels == 1] == 1).mean()) if (labels == 1).any() else None
    negative_recall = float((predictions[labels == 0] == 0).mean()) if (labels == 0).any() else None
    executable = [
        row["long_executable_points"] if side > 0 else row["short_executable_points"]
        for row, side in zip(rows, sides)
    ]
    return {
        "observations": len(rows),
        "accuracy": float((predictions == labels).mean()),
        "balanced_accuracy": (
            (positive_recall + negative_recall) / 2
            if positive_recall is not None and negative_recall is not None else None
        ),
        "gross_executable_mean_points": mean(executable),
        "gross_executable_median_points": median(executable),
        "gross_executable_win_rate": sum(value > 0 for value in executable) / len(executable),
        "gross_mean_ci95_points": _bootstrap_ci(executable),
        "cost_stress": {
            str(cost): {
                "mean_net_points": mean(value - cost for value in executable),
                "median_net_points": median(value - cost for value in executable),
                "net_win_rate": sum(value - cost > 0 for value in executable) / len(executable),
            }
            for cost in EXTRA_COST_POINTS
        },
    }


def _training_majority_side(rows: list[dict]) -> int:
    """Return the majority direction using training rows only."""

    if not rows:
        raise ValueError("training rows are required")
    return 1 if mean(row["direction"] > 0 for row in rows) >= 0.5 else -1


def probability_metrics(rows: list[dict], probability: np.ndarray) -> dict:
    labels = np.array([1 if row["direction"] > 0 else 0 for row in rows])
    sides = np.where(probability >= 0.5, 1, -1)
    clipped = np.clip(probability, 1e-12, 1 - 1e-12)
    return {
        **signal_metrics(rows, sides),
        "auc": _auc(labels, probability),
        "brier": float(np.mean((probability - labels) ** 2)),
        "log_loss": float(-np.mean(labels * np.log(clipped) + (1 - labels) * np.log(1 - clipped))),
    }


def _feature_exploration(rows: list[dict], features: tuple[str, ...]) -> dict:
    target = np.array([float(row["mid_move_points"]) for row in rows])
    result = {}
    for feature in features:
        values = np.array([
            float(row[feature]) if row.get(feature) is not None else np.nan for row in rows
        ])
        valid = np.isfinite(values) & np.isfinite(target)
        x, y = values[valid], target[valid]
        if len(x) < 20 or np.std(x) <= 1e-12:
            result[feature] = {"observations": int(len(x)), "pearson_mid_move": None}
            continue
        cuts = np.quantile(x, [0.2, 0.4, 0.6, 0.8])
        buckets = np.searchsorted(cuts, x, side="right")
        means = [float(y[buckets == index].mean()) if (buckets == index).any() else None
                 for index in range(5)]
        result[feature] = {
            "observations": int(len(x)),
            "pearson_mid_move": float(np.corrcoef(x, y)[0, 1]),
            "quintile_mean_mid_move_points": means,
            "top_minus_bottom_points": means[-1] - means[0],
            "note": "full-sample exploratory; not an out-of-sample estimate",
        }
    return result


def analyze_configuration(rows: list[dict]) -> dict:
    decision = int(rows[0]["decision_seconds"])
    features = PREOPEN_FEATURES if decision == 0 else PREOPEN_FEATURES + OBSERVED_FEATURES
    mbp_available = all(name in rows[0] for name in PREOPEN_MBP_FEATURES)
    if mbp_available:
        features += PREOPEN_MBP_FEATURES
        if decision > 0:
            features += OBSERVED_MBP_FEATURES
    all_predictions: list[tuple[str, float, dict, int]] = []
    fold_reports = []
    for fold_number, (train_days, test_days) in enumerate(
        chronological_folds([row["session_date"] for row in rows]), 1
    ):
        train = [row for row in rows if row["session_date"] in train_days]
        test = [row for row in rows if row["session_date"] in test_days]
        train_x, train_y = _matrix(train, features)
        test_x, _ = _matrix(test, features)
        prepared_train, prepared_test, _, _ = _prepare(train_x, test_x)
        intercept, weights = fit_logistic(prepared_train, train_y)
        probability = _sigmoid(intercept + prepared_test @ weights)
        majority_side = _training_majority_side(train)
        all_predictions.extend(
            (row["session_date"], float(value), row, majority_side)
            for row, value in zip(test, probability)
        )
        fold_reports.append({
            "fold": fold_number,
            "train_start": min(train_days),
            "train_end": max(train_days),
            "test_start": min(test_days),
            "test_end": max(test_days),
            **probability_metrics(test, probability),
        })
    all_predictions.sort(key=lambda item: item[0])
    oos_rows = [item[2] for item in all_predictions]
    oos_probability = np.array([item[1] for item in all_predictions])
    majority_sides = np.array([item[3] for item in all_predictions])
    momentum_feature = (
        "preopen_bbo_return_bps" if decision == 0 else "observed_bbo_return_bps"
    )
    momentum_sides = np.array([
        1 if (row.get(momentum_feature) or 0) >= 0 else -1 for row in oos_rows
    ])
    flow_feature = (
        "preopen_trades_signed_trade_imbalance"
        if decision == 0 else "observed_trades_signed_trade_imbalance"
    )
    flow_sides = np.array([1 if (row.get(flow_feature) or 0) >= 0 else -1 for row in oos_rows])
    baselines = {
        "training_sample_majority": signal_metrics(oos_rows, majority_sides),
        "price_momentum": signal_metrics(oos_rows, momentum_sides),
        "signed_trade_imbalance": signal_metrics(oos_rows, flow_sides),
    }
    if mbp_available:
        ofi_feature = (
            "preopen_mbp_depth_normalized_ofi"
            if decision == 0 else "observed_mbp_depth_normalized_ofi"
        )
        ofi_sides = np.array([
            1 if (row.get(ofi_feature) or 0) >= 0 else -1 for row in oos_rows
        ])
        baselines["depth_normalized_ofi"] = signal_metrics(oos_rows, ofi_sides)
    return {
        "decision_seconds": decision,
        "horizon_seconds": int(rows[0]["horizon_seconds"]),
        "features": list(features),
        "walk_forward": {
            "folds": fold_reports,
            "combined_oos": probability_metrics(oos_rows, oos_probability),
        },
        "mbp_features_available": mbp_available,
        "baselines_on_same_oos_rows": baselines,
        "univariate_full_sample_exploration": _feature_exploration(rows, features),
    }


def analyze(rows: list[dict]) -> dict:
    configurations = []
    keys = sorted({(int(row["decision_seconds"]), int(row["horizon_seconds"])) for row in rows})
    for decision, horizon in keys:
        selected = [
            row for row in rows
            if int(row["decision_seconds"]) == decision
            and int(row["horizon_seconds"]) == horizon
            and int(row["direction"]) != 0
        ]
        configurations.append(analyze_configuration(selected))
    return {
        "status": "retrospective_research_only",
        "rows_loaded": len(rows),
        "configurations": configurations,
        "interpretation_limits": [
            "This is observational association, not proof of economic causation.",
            "Full-sample univariate tables are exploratory and multiple-tested.",
            "Walk-forward results still use historical periods previously inspected in related research.",
            "Any survivor requires a frozen shadow-forward test before Tier 3 consideration.",
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
    print(json.dumps({
        "rows_loaded": report["rows_loaded"],
        "configurations": len(report["configurations"]),
        "output": str(args.output),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
