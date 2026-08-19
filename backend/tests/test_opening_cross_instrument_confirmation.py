from backend.research.opening_cross_instrument_confirmation import (
    INHERITED_NORMALIZED_GAP_THRESHOLD,
    confirmation_decision,
)


def _sample(trades=30, mean=2.0, lower=0.1, p=0.01, net=1.0, without=1.5):
    return {
        "fade": {
            "trades": trades,
            "gross_mean_points": mean,
            "bootstrap_mean_ci95_points": [lower, 3.0],
            "cost_scenarios": {"1.0": {"mean_net_points": net}},
            "mean_without_best_points": without,
        },
        "same_days_random_direction": {"one_sided_p_value": p},
    }


def test_es_translation_inherits_nq_threshold_without_refitting():
    assert INHERITED_NORMALIZED_GAP_THRESHOLD == 1.170437


def test_confirmation_requires_every_frozen_gate():
    halves = {
        "first_half": {"gross_mean_points": 1.0},
        "second_half": {"gross_mean_points": 0.5},
    }
    assert confirmation_decision(_sample(), halves)["status"] == "PASS"
    assert confirmation_decision(_sample(lower=-0.1), halves)["status"] == "FAIL"


def test_small_es_sample_is_inconclusive_not_a_failure():
    halves = {
        "first_half": {"gross_mean_points": 1.0},
        "second_half": {"gross_mean_points": 1.0},
    }
    result = confirmation_decision(_sample(trades=29), halves)
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
