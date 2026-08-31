"""Integrity tests for the separate pre-open conditional-state observer."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import date, datetime, time, timezone

import pytest

from backend.research import opening_preopen_conditional_forward as forward
from backend.research.opening_level_reaction import Quote, SecondState


DAY = date(2026, 8, 24)
INSTRUMENT = 42


def second(offset: int, *, close: float = 100.0, buys: float = 20.0, sells: float = 1.0) -> SecondState:
    stamp = forward._ns(DAY, time(9, 0)) + offset * 1_000_000_000
    return SecondState(
        bucket_ns=stamp,
        instrument_id=INSTRUMENT,
        event_count=10,
        trade_count=5,
        buy_volume=buys,
        sell_volume=sells,
        open_mid=close,
        high_mid=close,
        low_mid=close,
        close_mid=close,
        ofi=buys - sells,
        bid_queue_add=1.0,
        bid_queue_remove=0.5,
        ask_queue_add=1.0,
        ask_queue_remove=0.5,
        mean_depth=20.0,
        mean_queue_imbalance=0.1,
        mean_spread=0.25,
        mean_microprice_displacement=0.01,
    )


def bundle(*, aligned: bool = True, high_flow: bool = True, missing_offset: int | None = None) -> forward.PreopenBundle:
    if aligned:
        rows = [second(i, close=100.0 + (i / 1799), buys=20.0 if high_flow else 1.0, sells=1.0 if high_flow else 1.0) for i in range(1800)]
    else:
        rows = [second(i, close=100.0 - (i / 1799), buys=20.0 if high_flow else 1.0, sells=1.0 if high_flow else 1.0) for i in range(1800)]
    if missing_offset is not None:
        rows.pop(missing_offset)
    open_ns = forward._ns(DAY, time(9, 30))
    quotes = (
        (INSTRUMENT, Quote(open_ns, 100.0, 100.25)),
        (INSTRUMENT, Quote(open_ns + 120 * 1_000_000_000, 102.0, 102.25)),
    )
    return forward.PreopenBundle(tuple(rows), quotes, {"fixture": True})


def test_complete_aligned_high_flow_state_produces_baseline_and_candidate() -> None:
    result = forward.evaluate_bundle(DAY, bundle())
    assert result["status"] == "COMPLETE"
    assert result["preopen"]["seconds_observed"] == 1800
    assert result["preopen"]["flow_price_aligned"] is True
    assert result["preopen"]["high_flow_aligned_eligible"] is True
    assert result["outcomes"]["preopen_flow_direction_all"]["side"] == 1
    assert result["outcomes"]["high_flow_aligned_continuation"]["executable_points"] == pytest.approx(1.75)
    assert result["outcomes"]["high_flow_aligned_continuation"]["stress_points"] == pytest.approx(-0.50)
    assert result["execution_authorized"] is False


def test_missing_preopen_second_refuses() -> None:
    with pytest.raises(forward.ForwardRefusal, match="complete 1800-second"):
        forward.evaluate_bundle(DAY, bundle(missing_offset=777))


def test_unaligned_state_is_baseline_only() -> None:
    result = forward.evaluate_bundle(DAY, bundle(aligned=False))
    assert result["preopen"]["flow_price_aligned"] is False
    assert result["preopen"]["high_flow_aligned_eligible"] is False
    assert result["outcomes"]["preopen_flow_direction_all"]["side"] == 1
    assert result["outcomes"]["high_flow_aligned_continuation"] is None


def test_low_flow_state_abstains_from_candidate() -> None:
    result = forward.evaluate_bundle(DAY, bundle(high_flow=False))
    assert abs(result["preopen"]["flow_score"]) < forward.FLOW_ABS_THRESHOLD
    assert result["preopen"]["high_flow_aligned_eligible"] is False
    assert result["outcomes"]["high_flow_aligned_continuation"] is None


def test_multiple_instruments_refuse_instead_of_selecting_one() -> None:
    original = bundle()
    extra = second(0)
    extra = SecondState(**{**extra.__dict__, "instrument_id": 99})
    with pytest.raises(forward.ForwardRefusal, match="multiple instruments"):
        forward.evaluate_bundle(DAY, forward.PreopenBundle(original.seconds + (extra,), original.quotes, original.provenance))


def test_store_is_idempotent_and_immutable(tmp_path) -> None:
    store = forward.PreopenStore(tmp_path / "preopen.sqlite")
    result = forward.evaluate_bundle(DAY, bundle())
    recorded_at = datetime(2026, 8, 24, 21, tzinfo=timezone.utc)
    assert store.record(result, recorded_at) is True
    assert store.record(result, recorded_at) is False
    changed = deepcopy(result)
    changed["preopen"]["flow_score"] = 0.11
    with pytest.raises(forward.ForwardRefusal, match="immutable"):
        store.record(changed, recorded_at)
    summary = store.summary()
    assert summary["complete_sessions"] == 1
    assert summary["candidates"]["high_flow_aligned_continuation"]["n"] == 1


def test_check_mode_is_read_only_and_does_not_need_credentials(tmp_path, monkeypatch, capsys) -> None:
    db = tmp_path / "missing.sqlite"
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["observer", "--db", str(db), "--check"])
    assert forward.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "READY_NO_NETWORK"
    assert output["execution_capability"] is False
    assert not db.exists()
