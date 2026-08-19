"""Read-only MBP-1 feature extraction for opening-behaviour research.

The extractor emits research features only. It does not submit orders, write
learning outcomes, or alter the production coordinator.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PRICE_SCALE = 1_000_000_000


def _price(value: str | int | float) -> float:
    return float(value) / PRICE_SCALE


def _minute(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns / 1e9, timezone.utc).replace(second=0, microsecond=0)


def extract(path: Path) -> list[dict]:
    buckets: dict[datetime, dict] = defaultdict(lambda: {
        "records": 0,
        "trade_volume": 0.0,
        "buy_volume": 0.0,
        "sell_volume": 0.0,
        "book_imbalance_sum": 0.0,
        "book_imbalance_min": 1.0,
        "book_imbalance_max": -1.0,
        "spread_sum": 0.0,
        "microprice_displacement_sum": 0.0,
        "book_updates": 0,
        "trades": 0,
    })
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            ts = _minute(int(row["hd"]["ts_event"]))
            out = buckets[ts]
            out["records"] += 1
            level = (row.get("levels") or [{}])[0]
            bid = level.get("bid_px")
            ask = level.get("ask_px")
            bid_size = float(level.get("bid_sz") or 0)
            ask_size = float(level.get("ask_sz") or 0)
            if bid is not None and ask is not None and bid_size + ask_size > 0:
                bid_price, ask_price = _price(bid), _price(ask)
                imbalance = (bid_size - ask_size) / (bid_size + ask_size)
                mid = (bid_price + ask_price) / 2
                micro = (ask_price * bid_size + bid_price * ask_size) / (bid_size + ask_size)
                out["book_imbalance_sum"] += imbalance
                out["book_imbalance_min"] = min(out["book_imbalance_min"], imbalance)
                out["book_imbalance_max"] = max(out["book_imbalance_max"], imbalance)
                out["spread_sum"] += ask_price - bid_price
                out["microprice_displacement_sum"] += micro - mid
                out["book_updates"] += 1
            if row.get("action") == "T":
                size = float(row.get("size") or 0)
                out["trade_volume"] += size
                out["trades"] += 1
                # Databento trade side is the aggressor side: B=buy, A=sell.
                if row.get("side") == "B":
                    out["buy_volume"] += size
                elif row.get("side") == "A":
                    out["sell_volume"] += size

    result = []
    for ts, item in sorted(buckets.items()):
        book_n = item["book_updates"]
        total = item["buy_volume"] + item["sell_volume"]
        result.append({
            "timestamp_utc": ts.isoformat(),
            "records": item["records"],
            "trades": item["trades"],
            "trade_volume": item["trade_volume"],
            "buy_volume": item["buy_volume"],
            "sell_volume": item["sell_volume"],
            "signed_trade_imbalance": ((item["buy_volume"] - item["sell_volume"]) / total) if total else None,
            "mean_book_imbalance": item["book_imbalance_sum"] / book_n if book_n else None,
            "min_book_imbalance": item["book_imbalance_min"] if book_n else None,
            "max_book_imbalance": item["book_imbalance_max"] if book_n else None,
            "mean_spread": item["spread_sum"] / book_n if book_n else None,
            "mean_microprice_displacement": item["microprice_displacement_sum"] / book_n if book_n else None,
            # Absorption requires both executed flow and contemporaneous book
            # response.  A trades-only file must not emit a fabricated value.
            "absorption_proxy": (
                item["trade_volume"] / max(abs(item["microprice_displacement_sum"]), 1e-9)
                if item["trades"] and book_n else None
            ),
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = extract(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["timestamp_utc"]
    with args.output.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"features={len(rows)} output={args.output}")


if __name__ == "__main__":
    main()
