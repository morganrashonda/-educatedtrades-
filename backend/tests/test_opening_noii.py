import json
from pathlib import Path

from backend.research.opening_noii import directions, load_snapshots, parse_ns_timestamp, snapshot_features


def _row(timestamp: str, auction_type: str = "O") -> dict:
    return {
        "ts_recv": timestamp,
        "symbol": "QQQ",
        "auction_type": auction_type,
        "side": "B",
        "paired_qty": 100,
        "total_imbalance_qty": 25,
        "ref_price": "500.000000000",
        "cont_book_clr_price": "500.500000000",
    }


def test_nanosecond_timestamp_parser_preserves_ordering():
    first = parse_ns_timestamp("2026-08-18T13:29:00.010954024Z")
    second = parse_ns_timestamp("2026-08-18T13:29:00.020000000Z")
    assert first < second


def test_snapshot_cutoff_uses_received_time_without_lookahead(tmp_path: Path):
    path = tmp_path / "noii.jsonl"
    rows = [
        _row("2026-08-18T13:28:59.900000000Z"),
        _row("2026-08-18T13:29:00.010000000Z"),  # too late for 09:29:00
        _row("2026-08-18T13:29:49.900000000Z"),
        _row("2026-08-18T13:29:50.010000000Z"),  # too late for 09:29:50
        _row("2026-08-18T13:28:58.000000000Z", auction_type="C"),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    snapshots, quality = load_snapshots(path)
    day = snapshots["2026-08-18"]
    assert day["snapshot_2900"]["ts_recv"] == rows[0]["ts_recv"]
    assert day["snapshot_2950"]["ts_recv"] == rows[2]["ts_recv"]
    assert quality["opening_records"] == 4
    assert quality["excluded_non_opening_records"] == 1


def test_fade_alignment_and_frozen_filters():
    features = snapshot_features(_row("2026-08-18T13:28:59.000000000Z"), fade_direction=1)
    assert features["signed_imbalance_ratio"] == 0.25
    assert features["fade_aligned_imbalance"] == 0.25
    assert round(features["near_displacement_bps"], 6) == 10.0
    record = {
        "snapshot_2900_fade_aligned_imbalance": features["fade_aligned_imbalance"],
        "snapshot_2900_fade_aligned_near": features["fade_aligned_near"],
    }
    filters = directions(record, "snapshot_2900")
    assert filters == {
        "base_fade": True,
        "imbalance_support": True,
        "near_price_support": True,
        "dual_support": True,
        "no_opposition": True,
    }
