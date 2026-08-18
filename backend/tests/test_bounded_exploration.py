import pytest

from backend.trading import bounded_signal_quantity


def test_quantity_override_can_only_reduce_normal_risk_size():
    assert bounded_signal_quantity(25, 1) == 1
    assert bounded_signal_quantity(1, 25) == 1
    assert bounded_signal_quantity(25, None) == 25


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "1"])
def test_quantity_override_rejects_invalid_values(bad):
    with pytest.raises(ValueError):
        bounded_signal_quantity(25, bad)
