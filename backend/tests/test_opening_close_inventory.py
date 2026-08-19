import json
from pathlib import Path

from backend.research.opening_close_inventory import load_close_snapshots, signed_ratio


def _row(ts: str, auction_type: str = "C", side: str = "A") -> dict:
    return {
        "ts_recv": ts,
        "auction_type": auction_type,
        "side": side,
        "paired_qty": 100,
        "total_imbalance_qty": 25,
    }


def test_close_snapshot_uses_received_time_cutoff(tmp_path: Path):
    path = tmp_path / "noii.jsonl"
    rows = [
        _row("2026-08-18T19:59:49.900000000Z"),
        _row("2026-08-18T19:59:50.010000000Z"),
        _row("2026-08-18T19:59:40.000000000Z", auction_type="O"),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    snapshots, quality = load_close_snapshots(path)
    assert snapshots["2026-08-18"]["ts_recv"] == rows[0]["ts_recv"]
    assert quality["closing_records"] == 2


def test_close_imbalance_sign_convention():
    assert signed_ratio(_row("2026-08-18T19:59:49Z", side="B")) == 0.25
    assert signed_ratio(_row("2026-08-18T19:59:49Z", side="A")) == -0.25
    assert signed_ratio(_row("2026-08-18T19:59:49Z", side="N")) == 0.0
