from __future__ import annotations

import numpy as np

from backend.research.opening_microstructure_analysis import (
    _auc,
    _prepare,
    _training_majority_side,
    chronological_folds,
    fit_logistic,
    signal_metrics,
)


def test_chronological_folds_never_train_on_future_dates() -> None:
    days = [f"2026-01-{day:02}" for day in range(1, 21)]
    for train, test in chronological_folds(days):
        assert max(train) < min(test)
        assert train.isdisjoint(test)


def test_training_only_scaling_and_logistic_direction() -> None:
    train_x = np.array([[-2.0], [-1.0], [1.0], [2.0]])
    train_y = np.array([0.0, 0.0, 1.0, 1.0])
    test_x = np.array([[-10.0], [10.0]])
    prepared_train, prepared_test, center, scale = _prepare(train_x, test_x)
    assert center[0] == 0.0
    assert 0 < scale[0] <= np.std(train_x[:, 0])
    intercept, weights = fit_logistic(prepared_train, train_y)
    probability = 1 / (1 + np.exp(-(intercept + prepared_test @ weights)))
    assert probability[0] < 0.5 < probability[1]
    assert _auc(np.array([0, 1]), probability) == 1.0


def test_training_only_winsorization_bounds_future_extreme() -> None:
    train_x = np.array([[0.0], [1.0], [2.0], [3.0], [4.0]])
    test_x = np.array([[1_000_000.0]])
    _, prepared_test, _, _ = _prepare(train_x, test_x)
    # A future extreme is clipped to a training-derived upper bound before
    # scaling, so it cannot dictate its own normalization.
    assert prepared_test[0, 0] < 2.0


def test_signal_metrics_use_executable_side_and_cost_once() -> None:
    rows = [
        {
            "direction": 1,
            "long_executable_points": 2.0,
            "short_executable_points": -2.5,
        },
        {
            "direction": -1,
            "long_executable_points": -1.5,
            "short_executable_points": 1.0,
        },
    ]
    report = signal_metrics(rows, np.array([1, -1]))
    assert report["accuracy"] == 1.0
    assert report["gross_executable_mean_points"] == 1.5
    assert report["cost_stress"]["0.25"]["mean_net_points"] == 1.25


def test_majority_baseline_uses_training_rows_only() -> None:
    training = [{"direction": -1}, {"direction": -1}, {"direction": 1}]
    future = training + [{"direction": 1}] * 100
    assert _training_majority_side(training) == -1
    assert _training_majority_side(future) == 1
