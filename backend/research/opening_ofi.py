"""Event-level opening order-flow research for Databento MBP-1 data.

This module is deliberately isolated from the production coordinator.  It
reads historical JSONL and emits research features; it cannot place orders or
write learning outcomes.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from contextlib import nullcontext
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


PRICE_SCALE = 1_000_000_000


@dataclass(frozen=True)
class BookState:
    """Validated top-of-book state after one MBP-1 event."""

    ts_ns: int
    bid_px: float
    ask_px: float
    bid_sz: float
    ask_sz: float

    @property
    def mid(self) -> float:
        return (self.bid_px + self.ask_px) / 2.0

    @property
    def depth(self) -> float:
        return (self.bid_sz + self.ask_sz) / 2.0

    @property
    def queue_imbalance(self) -> float | None:
        total = self.bid_sz + self.ask_sz
        return (self.bid_sz - self.ask_sz) / total if total else None

    @property
    def microprice(self) -> float | None:
        total = self.bid_sz + self.ask_sz
        if not total:
            return None
        return (self.ask_px * self.bid_sz + self.bid_px * self.ask_sz) / total


def _price(value: str | int | float) -> float:
    return float(value) / PRICE_SCALE


def state_from_row(row: dict) -> BookState | None:
    """Return a usable BBO state, or ``None`` for a malformed/empty update."""

    level = (row.get("levels") or [{}])[0]
    try:
        state = BookState(
            ts_ns=int(row["hd"]["ts_event"]),
            bid_px=_price(level["bid_px"]),
            ask_px=_price(level["ask_px"]),
            bid_sz=float(level.get("bid_sz") or 0),
            ask_sz=float(level.get("ask_sz") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if state.bid_px <= 0 or state.ask_px <= state.bid_px:
        return None
    return state


def book_event_components(previous: BookState, current: BookState) -> dict[str, float]:
    """Compute Cont-style top-level OFI and explicit queue-flow components.

    Positive OFI denotes net upward pressure. Queue additions/removals include
    price-level replacement: a worse bid removes the old bid queue, while a
    better bid adds the new queue; the ask-side interpretation is symmetric.
    """

    bid_add = bid_remove = ask_add = ask_remove = 0.0

    if current.bid_px > previous.bid_px:
        bid_add = current.bid_sz
    elif current.bid_px < previous.bid_px:
        bid_remove = previous.bid_sz
    else:
        delta = current.bid_sz - previous.bid_sz
        bid_add = max(delta, 0.0)
        bid_remove = max(-delta, 0.0)

    if current.ask_px < previous.ask_px:
        ask_add = current.ask_sz
    elif current.ask_px > previous.ask_px:
        ask_remove = previous.ask_sz
    else:
        delta = current.ask_sz - previous.ask_sz
        ask_add = max(delta, 0.0)
        ask_remove = max(-delta, 0.0)

    ofi = bid_add - bid_remove - ask_add + ask_remove
    return {
        "ofi": ofi,
        "bid_queue_add": bid_add,
        "bid_queue_remove": bid_remove,
        "ask_queue_add": ask_add,
        "ask_queue_remove": ask_remove,
    }


def _new_bucket(start_ns: int) -> dict:
    return {
        "bucket_start_ns": start_ns,
        "first_event_ns": None,
        "last_event_ns": None,
        "open_mid": None,
        "close_mid": None,
        "events": 0,
        "trades": 0,
        "buy_volume": 0.0,
        "sell_volume": 0.0,
        "add_bid_volume": 0.0,
        "add_ask_volume": 0.0,
        "cancel_bid_volume": 0.0,
        "cancel_ask_volume": 0.0,
        "modify_bid_volume": 0.0,
        "modify_ask_volume": 0.0,
        "ofi": 0.0,
        "bid_queue_add": 0.0,
        "bid_queue_remove": 0.0,
        "ask_queue_add": 0.0,
        "ask_queue_remove": 0.0,
        "depth_sum": 0.0,
        "queue_imbalance_sum": 0.0,
        "microprice_displacement_sum": 0.0,
        "spread_sum": 0.0,
        "book_states": 0,
        "queue_states": 0,
        "microprice_states": 0,
    }


def _finalize_bucket(item: dict, interval_seconds: int) -> dict:
    n = item["book_states"]
    trade_total = item["buy_volume"] + item["sell_volume"]
    cancel_total = item["cancel_bid_volume"] + item["cancel_ask_volume"]
    add_total = item["add_bid_volume"] + item["add_ask_volume"]
    mean_depth = item["depth_sum"] / n if n else None
    queue_n = item["queue_states"]
    micro_n = item["microprice_states"]
    mean_queue_imbalance = item["queue_imbalance_sum"] / queue_n if queue_n else None
    mean_micro_displacement = (
        item["microprice_displacement_sum"] / micro_n if micro_n else None
    )
    start_ns = item["bucket_start_ns"]
    decision_ns = start_ns + interval_seconds * 1_000_000_000

    ask_attack = item["buy_volume"] + item["ask_queue_remove"]
    bid_attack = item["sell_volume"] + item["bid_queue_remove"]
    ask_refill_ratio = item["ask_queue_add"] / ask_attack if ask_attack else None
    bid_refill_ratio = item["bid_queue_add"] / bid_attack if bid_attack else None

    return {
        "timestamp_utc": datetime.fromtimestamp(start_ns / 1e9, timezone.utc).isoformat(),
        "decision_timestamp_utc": datetime.fromtimestamp(decision_ns / 1e9, timezone.utc).isoformat(),
        "bucket_start_ns": start_ns,
        "first_event_ns": item["first_event_ns"],
        "last_event_ns": item["last_event_ns"],
        "events": item["events"],
        "trades": item["trades"],
        "open_mid": item["open_mid"],
        "close_mid": item["close_mid"],
        "mid_return": (
            item["close_mid"] / item["open_mid"] - 1.0
            if item["open_mid"] and item["close_mid"] else None
        ),
        "buy_volume": item["buy_volume"],
        "sell_volume": item["sell_volume"],
        "signed_trade_imbalance": (
            (item["buy_volume"] - item["sell_volume"]) / trade_total
            if trade_total else None
        ),
        "ofi": item["ofi"],
        "mean_depth": mean_depth,
        "depth_normalized_ofi": item["ofi"] / mean_depth if mean_depth else None,
        "mean_queue_imbalance": mean_queue_imbalance,
        "mean_spread": item["spread_sum"] / n if n else None,
        "mean_microprice_displacement": mean_micro_displacement,
        "bid_queue_add": item["bid_queue_add"],
        "bid_queue_remove": item["bid_queue_remove"],
        "ask_queue_add": item["ask_queue_add"],
        "ask_queue_remove": item["ask_queue_remove"],
        "bid_refill_ratio": bid_refill_ratio,
        "ask_refill_ratio": ask_refill_ratio,
        "cancel_imbalance": (
            (item["cancel_ask_volume"] - item["cancel_bid_volume"]) / cancel_total
            if cancel_total else None
        ),
        "addition_imbalance": (
            (item["add_bid_volume"] - item["add_ask_volume"]) / add_total
            if add_total else None
        ),
        "add_bid_volume": item["add_bid_volume"],
        "add_ask_volume": item["add_ask_volume"],
        "cancel_bid_volume": item["cancel_bid_volume"],
        "cancel_ask_volume": item["cancel_ask_volume"],
        "modify_bid_volume": item["modify_bid_volume"],
        "modify_ask_volume": item["modify_ask_volume"],
    }


def extract_event_buckets(
    path: Path,
    *,
    interval_seconds: int = 1,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> list[dict]:
    """Stream MBP-1 JSONL into fixed event-time feature buckets."""

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    interval_ns = interval_seconds * 1_000_000_000
    buckets: dict[int, dict] = defaultdict(dict)
    previous: BookState | None = None
    previous_instrument: int | None = None

    source = nullcontext(sys.stdin) if str(path) == "-" else path.open()
    with source as fh:
        for line_number, line in enumerate(fh, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at line {line_number}") from exc
            if row.get("action") == "R":
                previous = None
                previous_instrument = None
                continue
            current = state_from_row(row)
            if current is None:
                continue
            try:
                instrument = int(row["hd"]["instrument_id"])
            except (KeyError, TypeError, ValueError):
                instrument = None
            if previous is not None and current.ts_ns < previous.ts_ns:
                raise ValueError(f"event timestamps are not monotonic at line {line_number}")
            if previous_instrument is not None and instrument != previous_instrument:
                previous = None
            if start_ns is not None and current.ts_ns < start_ns:
                previous = current
                previous_instrument = instrument
                continue
            if end_ns is not None and current.ts_ns >= end_ns:
                break

            bucket_start = current.ts_ns - current.ts_ns % interval_ns
            if not buckets[bucket_start]:
                buckets[bucket_start] = _new_bucket(bucket_start)
            out = buckets[bucket_start]
            out["events"] += 1
            out["first_event_ns"] = current.ts_ns if out["first_event_ns"] is None else out["first_event_ns"]
            out["last_event_ns"] = current.ts_ns
            out["open_mid"] = current.mid if out["open_mid"] is None else out["open_mid"]
            out["close_mid"] = current.mid
            out["depth_sum"] += current.depth
            out["spread_sum"] += current.ask_px - current.bid_px
            if current.queue_imbalance is not None:
                out["queue_imbalance_sum"] += current.queue_imbalance
                out["queue_states"] += 1
            if current.microprice is not None:
                out["microprice_displacement_sum"] += current.microprice - current.mid
                out["microprice_states"] += 1
            out["book_states"] += 1

            if previous is not None:
                components = book_event_components(previous, current)
                for key, value in components.items():
                    out[key] += value

            action = row.get("action")
            side = row.get("side")
            size = float(row.get("size") or 0)
            if action == "T":
                out["trades"] += 1
                if side == "B":
                    out["buy_volume"] += size
                elif side == "A":
                    out["sell_volume"] += size
            elif action == "A" and side in {"A", "B"}:
                out[f"add_{'ask' if side == 'A' else 'bid'}_volume"] += size
            elif action == "C" and side in {"A", "B"}:
                out[f"cancel_{'ask' if side == 'A' else 'bid'}_volume"] += size
            elif action == "M" and side in {"A", "B"}:
                out[f"modify_{'ask' if side == 'A' else 'bid'}_volume"] += size
            previous = current
            previous_instrument = instrument

    return [_finalize_bucket(buckets[key], interval_seconds) for key in sorted(buckets)]


def attach_forward_returns(rows: list[dict], horizons_seconds: Iterable[int]) -> list[dict]:
    """Attach close-to-close future returns using exact later bucket closes.

    A feature bucket ending at ``t`` is labelled from its close to the close of
    the bucket ending at ``t + horizon``. Exact bucket matching prevents a
    future event from being silently pulled backward across a data gap.
    """

    by_start = {int(row["bucket_start_ns"]): row for row in rows}
    horizons = tuple(sorted(set(int(value) for value in horizons_seconds)))
    for horizon in horizons:
        if horizon <= 0:
            raise ValueError("forward horizons must be positive")
    for row in rows:
        for horizon in horizons:
            future = by_start.get(int(row["bucket_start_ns"]) + horizon * 1_000_000_000)
            value = None
            if future and row.get("close_mid") and future.get("close_mid"):
                value = future["close_mid"] / row["close_mid"] - 1.0
            row[f"forward_return_{horizon}s"] = value
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["timestamp_utc"]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=1)
    parser.add_argument("--horizons", default="10,30,60,120")
    args = parser.parse_args()
    horizons = [int(value) for value in args.horizons.split(",") if value]
    rows = extract_event_buckets(args.input, interval_seconds=args.interval_seconds)
    attach_forward_returns(rows, horizons)
    write_csv(args.output, rows)
    print(f"features={len(rows)} output={args.output}")


if __name__ == "__main__":
    main()
