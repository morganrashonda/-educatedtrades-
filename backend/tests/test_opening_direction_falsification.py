from backend.research.opening_direction_falsification import (
    add_prior_volatility,
    compare_directions,
    match_ordinary_sessions,
    random_direction_test,
    summarize_excursions,
)


def _row(day, target, direction, volatility=1.0, overnight=1.2):
    return {
        "day": day,
        "target_points": target,
        "base_direction": direction,
        "prior_20d_volatility_pct": volatility,
        "nq_overnight_ret": overnight,
    }


def test_direction_comparisons_use_identical_days_and_frozen_sides():
    rows = [
        _row("2026-01-02", -4.0, -1),
        _row("2026-02-02", 2.0, 1),
        _row("2026-04-02", -6.0, -1),
        _row("2026-05-02", 8.0, 1),
    ]
    report = compare_directions(rows, seed=7)
    assert report["fade"]["gross_mean_points"] == 5.0
    assert report["continuation"]["gross_mean_points"] == -5.0
    assert report["always_long"]["gross_mean_points"] == 0.0
    assert report["always_short"]["gross_mean_points"] == 0.0
    assert report["direction_agnostic_same_day_movement"][
        "mean_absolute_two_minute_points"
    ] == 5.0


def test_random_direction_null_is_deterministic():
    first = random_direction_test([1.0, -2.0, 3.0], 1.0, seed=42, samples=200)
    second = random_direction_test([1.0, -2.0, 3.0], 1.0, seed=42, samples=200)
    assert first == second
    assert 0 < first["one_sided_p_value"] <= 1


def test_volatility_matching_is_same_year_and_without_replacement():
    candidates = [
        _row("2025-01-03", 10, -1, 1.0),
        _row("2025-02-03", 20, 1, 2.0),
    ]
    controls = [
        _row("2025-03-03", 1, 1, 1.01, 0.2),
        _row("2025-04-03", 2, 1, 2.01, 0.3),
        _row("2024-04-03", 3, 1, 1.0, 0.2),
    ]
    matches = match_ordinary_sessions(candidates, controls)
    assert len(matches) == 2
    assert len({control["day"] for _, control in matches}) == 2
    assert all(candidate["day"][:4] == control["day"][:4] for candidate, control in matches)


def test_mfe_mae_respect_long_and_short_direction():
    rows = [
        _row("2026-01-02", 1.0, 1),
        _row("2026-01-03", -1.0, -1),
    ]
    paths = {
        "2026-01-02": {"entry": 100, "exit": 101, "high": 105, "low": 98},
        "2026-01-03": {"entry": 100, "exit": 99, "high": 103, "low": 94},
    }
    report = summarize_excursions(rows, paths)
    assert report["target_mismatches"] == 0
    assert report["mean_mfe_points"] == 5.5
    assert report["mean_mae_points"] == 2.5


def test_prior_volatility_is_reconstructed_from_previous_session_closes():
    rows = []
    closes = {}
    for index in range(25):
        day = f"2026-01-{index + 1:02d}"
        rows.append(_row(day, 1, 1))
        closes[day] = 100.0 + index
    result = add_prior_volatility(rows, closes)
    assert result
    assert result[0]["day"] == "2026-01-22"
    assert result[0]["prior_20d_volatility_pct"] > 0
