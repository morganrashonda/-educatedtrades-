import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.research.opening_two_minute import build_records, rule_directions, summarize


ET = ZoneInfo("America/New_York")


def _utc_ns(day: str, hour: int, minute: int) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02}:{minute:02}:00").replace(tzinfo=ET).timestamp() * 1e9)


def _write_nq(path: Path) -> None:
    with path.open("w") as fh:
        for day, instrument, base in (
            ("2026-01-05", 100, 20000),
            ("2026-01-06", 200, 20200),  # transition from prior close
            ("2026-01-07", 200, 20400),
        ):
            for hour, minute, offset in ((8, 59, 0), (9, 24, 10), (9, 28, 20), (9, 30, 25), (9, 31, 30), (15, 59, 40)):
                price = base + offset
                row = {
                    "hd": {"ts_event": str(_utc_ns(day, hour, minute)), "instrument_id": instrument},
                    "open": str(int(price * 1e9)),
                    "high": str(int((price + 1) * 1e9)),
                    "low": str(int((price - 1) * 1e9)),
                    "close": str(int((price + 0.5) * 1e9)),
                    "volume": "10",
                }
                fh.write(json.dumps(row) + "\n")


def _write_qqq(path: Path) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=("timestamp", "open", "high", "low", "close", "volume"))
        writer.writeheader()
        for day, base in (("2026-01-05", 500), ("2026-01-06", 501), ("2026-01-07", 502)):
            for hour, minute, offset in ((8, 59, 0), (9, 24, 0.1), (9, 28, 0.2), (9, 30, 0.3), (9, 31, 0.4), (15, 59, 0.5)):
                local = datetime.fromisoformat(f"{day}T{hour:02}:{minute:02}:00").replace(tzinfo=ET)
                price = base + offset
                writer.writerow(
                    {
                        "timestamp": local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
                        "open": price,
                        "high": price + 0.05,
                        "low": price - 0.05,
                        "close": price + 0.01,
                        "volume": 100,
                    }
                )


def test_build_records_excludes_continuous_contract_roll(tmp_path):
    nq, qqq = tmp_path / "nq.jsonl", tmp_path / "qqq.csv"
    _write_nq(nq)
    _write_qqq(qqq)
    records, quality = build_records(nq, qqq)
    assert [row["day"] for row in records] == ["2026-01-07"]
    assert quality["excluded"]["nq_roll_transition"] == 1


def test_rules_use_only_frozen_feature_values():
    row = {
        "nq_ret_5m": 0.1,
        "qqq_ret_5m": 0.2,
        "nq_overnight_ret": -1.1,
        "qqq_overnight_ret": -1.0,
        "nq_overnight_location": 0.1,
    }
    directions = rule_directions(row)
    assert directions["late_agreement"] == 1
    assert directions["overnight_agreement"] == -1
    assert directions["overnight_extreme_rejection"] == 1
    assert directions["overnight_extreme_breakout"] == 0


def test_cost_is_deducted_once_per_round_trip():
    report = summarize([3.0, -1.0], sessions=3, seed=7)
    assert report["abstentions"] == 1
    assert report["gross_mean_points"] == 1.0
    assert report["cost_scenarios"]["1.0"]["mean_net_points"] == 0.0
    assert report["cost_scenarios"]["1.0"]["total_nq_dollars"] == 0.0
