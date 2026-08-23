from __future__ import annotations

from backend.research.opening_microstructure_sensitivity import _metrics, _side


def _row(day: str, move: float) -> dict:
    return {
        "session_date": day,
        "direction": 1 if move > 0 else -1,
        "long_executable_points": move,
        "short_executable_points": -move,
    }


def test_zero_or_missing_evidence_abstains() -> None:
    assert _side(None) == 0
    assert _side(0.0) == 0
    assert _side(-0.1) == -1
    assert _side(0.1) == 1


def test_metrics_report_coverage_and_equal_year_weighting() -> None:
    rows = [
        _row("2025-01-02", 2.0),
        _row("2025-01-03", -1.0),
        _row("2026-01-02", 4.0),
    ]
    report = _metrics(rows, [1, 0, 1], universe=3)
    assert report["observations"] == 2
    assert report["coverage"] == 2 / 3
    assert report["equal_year_weighted"]["gross_executable_mean_points"] == 3.0
    assert report["excluding_latest_year"]["excluded_year"] == "2026"
    assert report["excluding_latest_year"]["observations"] == 1
