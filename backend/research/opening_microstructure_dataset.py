"""Build leakage-safe opening microstructure research rows from SQLite.

This module is research-only.  It reads the compact Databento evidence store
and never imports broker, execution, production, learning, or Tier 3 code.

For each decision timestamp, every feature is computed from records strictly
before that timestamp.  Entry and exit marks use the executable side of the
observed BBO, while direction labels use midprice only.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
PRICE_SCALE = 1_000_000_000
OPEN = time(9, 30)
WINDOW_START = time(9, 28)
DECISION_SECONDS = (0, 5, 10, 30, 60)
HORIZON_SECONDS = (120, 300)
MAX_MARK_DELAY_NS = 2_000_000_000


def _ns(day: date, value: time) -> int:
    return int(datetime.combine(day, value, ET).timestamp() * 1_000_000_000)


def _quote_at_or_after(rows: list[tuple], target_ns: int) -> tuple | None:
    for row in rows:
        if row[0] >= target_ns:
            return row if row[0] - target_ns <= MAX_MARK_DELAY_NS else None
    return None


def _bbo_stats(rows: list[tuple]) -> dict[str, float | None]:
    if not rows:
        return {
            "return_bps": None,
            "range_points": None,
            "mean_spread_points": None,
            "mean_depth": None,
            "mean_queue_imbalance": None,
            "last_queue_imbalance": None,
            "mean_microprice_displacement_points": None,
        }
    mids: list[float] = []
    spreads: list[float] = []
    depths: list[float] = []
    queues: list[float] = []
    micros: list[float] = []
    for _, _, bid_i, ask_i, bid_size, ask_size in rows:
        bid, ask = bid_i / PRICE_SCALE, ask_i / PRICE_SCALE
        mid = (bid + ask) / 2
        total = bid_size + ask_size
        mids.append(mid)
        spreads.append(ask - bid)
        depths.append(total / 2)
        if total:
            queues.append((bid_size - ask_size) / total)
            micros.append((ask * bid_size + bid * ask_size) / total - mid)
    return {
        "return_bps": (mids[-1] / mids[0] - 1) * 10_000 if mids[0] else None,
        "range_points": max(mids) - min(mids),
        "mean_spread_points": mean(spreads),
        "mean_depth": mean(depths),
        "mean_queue_imbalance": mean(queues) if queues else None,
        "last_queue_imbalance": queues[-1] if queues else None,
        "mean_microprice_displacement_points": mean(micros) if micros else None,
    }


def _trade_stats(rows: list[tuple]) -> dict[str, float | int | None]:
    buys = sum(row[3] for row in rows)
    sells = sum(row[4] for row in rows)
    unknown = sum(row[5] for row in rows)
    total = buys + sells
    all_volume = total + unknown
    return {
        "trade_count": sum(row[2] for row in rows),
        "buy_volume": buys,
        "sell_volume": sells,
        "unknown_volume": unknown,
        "signed_trade_imbalance": (buys - sells) / total if total else None,
        "unknown_volume_fraction": unknown / all_volume if all_volume else None,
    }


def _mbp_stats(rows: list[tuple]) -> dict[str, float | int | None]:
    if not rows:
        return {
            "event_count": 0,
            "trade_count": 0,
            "signed_trade_imbalance": None,
            "return_bps": None,
            "range_points": None,
            "ofi": 0.0,
            "depth_normalized_ofi": None,
            "bid_refill_ratio": None,
            "ask_refill_ratio": None,
            "mean_depth": None,
            "mean_queue_imbalance": None,
            "mean_spread_points": None,
            "mean_microprice_displacement_points": None,
            "cancel_imbalance": None,
            "addition_imbalance": None,
        }
    buys = sum(row[4] for row in rows)
    sells = sum(row[5] for row in rows)
    trade_total = buys + sells
    mids_open = rows[0][6]
    mids_close = rows[-1][9]
    ofi = sum(row[10] for row in rows)
    bid_add = sum(row[11] for row in rows)
    bid_remove = sum(row[12] for row in rows)
    ask_add = sum(row[13] for row in rows)
    ask_remove = sum(row[14] for row in rows)
    add_bid = sum(row[15] for row in rows)
    add_ask = sum(row[16] for row in rows)
    cancel_bid = sum(row[17] for row in rows)
    cancel_ask = sum(row[18] for row in rows)
    depths = [row[21] for row in rows if row[21] is not None]
    queues = [row[22] for row in rows if row[22] is not None]
    spreads = [row[23] for row in rows if row[23] is not None]
    micros = [row[24] for row in rows if row[24] is not None]
    mean_depth = mean(depths) if depths else None
    cancel_total = cancel_bid + cancel_ask
    addition_total = add_bid + add_ask
    return {
        "event_count": sum(row[2] for row in rows),
        "trade_count": sum(row[3] for row in rows),
        "signed_trade_imbalance": (buys - sells) / trade_total if trade_total else None,
        "return_bps": (mids_close / mids_open - 1) * 10_000 if mids_open else None,
        "range_points": max(row[7] for row in rows) - min(row[8] for row in rows),
        "ofi": ofi,
        "depth_normalized_ofi": ofi / mean_depth if mean_depth else None,
        "bid_refill_ratio": bid_add / (sells + bid_remove) if sells + bid_remove else None,
        "ask_refill_ratio": ask_add / (buys + ask_remove) if buys + ask_remove else None,
        "mean_depth": mean_depth,
        "mean_queue_imbalance": mean(queues) if queues else None,
        "mean_spread_points": mean(spreads) if spreads else None,
        "mean_microprice_displacement_points": mean(micros) if micros else None,
        "cancel_imbalance": (
            (cancel_ask - cancel_bid) / cancel_total if cancel_total else None
        ),
        "addition_imbalance": (
            (add_bid - add_ask) / addition_total if addition_total else None
        ),
    }


def _prefixed(prefix: str, values: dict) -> dict:
    return {f"{prefix}_{key}": value for key, value in values.items()}


def _session_rows(
    conn: sqlite3.Connection, day: str, *, include_mbp: bool
) -> tuple[list[tuple], list[tuple], list[tuple]]:
    bbo = conn.execute(
        """
        SELECT ts_event, instrument_id, bid_px_i, ask_px_i, bid_sz, ask_sz
        FROM bbo_1s WHERE session_date = ? ORDER BY ts_event, instrument_id
        """,
        (day,),
    ).fetchall()
    trades = conn.execute(
        """
        SELECT bucket_ns, instrument_id, trade_count, buy_volume, sell_volume,
               unknown_volume
        FROM trades_1s WHERE session_date = ? ORDER BY bucket_ns, instrument_id
        """,
        (day,),
    ).fetchall()
    mbp = []
    if include_mbp:
        mbp = conn.execute(
            """
            SELECT bucket_ns, instrument_id, event_count, trade_count,
                   buy_volume, sell_volume, open_mid, high_mid, low_mid,
                   close_mid, ofi, bid_queue_add, bid_queue_remove,
                   ask_queue_add, ask_queue_remove, add_bid_volume,
                   add_ask_volume, cancel_bid_volume, cancel_ask_volume,
                   modify_bid_volume, modify_ask_volume, mean_depth,
                   mean_queue_imbalance, mean_spread,
                   mean_microprice_displacement
            FROM mbp1_1s WHERE session_date = ?
            ORDER BY bucket_ns, instrument_id
            """,
            (day,),
        ).fetchall()
    return bbo, trades, mbp


def build_rows(
    db_path: Path,
    *,
    decisions: tuple[int, ...] = DECISION_SECONDS,
    horizons: tuple[int, ...] = HORIZON_SECONDS,
    require_mbp: bool = False,
) -> tuple[list[dict], dict]:
    """Return decision/horizon rows and explicit exclusion counts."""

    if any(value < 0 for value in decisions):
        raise ValueError("decision offsets must be non-negative")
    if any(value <= 0 for value in horizons):
        raise ValueError("horizons must be positive")
    conn = sqlite3.connect(db_path)
    mbp_join = (
        "JOIN requests m ON m.session_date = o.session_date "
        "AND m.schema_name = 'mbp-1' AND m.status = 'complete'"
        if require_mbp else ""
    )
    days = [
        row[0]
        for row in conn.execute(
            f"""
            SELECT o.session_date
            FROM requests o
            JOIN requests b ON b.session_date = o.session_date
            JOIN requests t ON t.session_date = o.session_date
            {mbp_join}
            WHERE o.schema_name = 'ohlcv-1s' AND o.status = 'complete'
              AND b.schema_name = 'bbo-1s' AND b.status = 'complete'
              AND t.schema_name = 'trades' AND t.status = 'complete'
            ORDER BY o.session_date
            """
        )
    ]
    output: list[dict] = []
    excluded: Counter[str] = Counter()
    try:
        for day_text in days:
            day = date.fromisoformat(day_text)
            open_ns = _ns(day, OPEN)
            start_ns = _ns(day, WINDOW_START)
            bbo, trades, mbp = _session_rows(
                conn, day_text, include_mbp=require_mbp
            )
            if not bbo or not trades:
                excluded["missing_schema_rows"] += 1
                continue
            open_quote = _quote_at_or_after(bbo, open_ns)
            if open_quote is None:
                excluded["missing_open_quote"] += 1
                continue
            instrument = int(open_quote[1])
            bbo = [row for row in bbo if int(row[1]) == instrument]
            trades = [row for row in trades if int(row[1]) == instrument]
            mbp = [row for row in mbp if int(row[1]) == instrument]
            if require_mbp and not mbp:
                excluded["missing_mbp_rows"] += 1
                continue
            pre_bbo = [row for row in bbo if start_ns <= row[0] < open_ns]
            pre_trades = [row for row in trades if start_ns <= row[0] < open_ns]
            if not pre_bbo:
                excluded["missing_preopen_bbo"] += 1
                continue
            pre_features = {
                **_prefixed("preopen_bbo", _bbo_stats(pre_bbo)),
                **_prefixed("preopen_trades", _trade_stats(pre_trades)),
            }
            if require_mbp:
                pre_mbp = [row for row in mbp if start_ns <= row[0] < open_ns]
                pre_features.update(_prefixed("preopen_mbp", _mbp_stats(pre_mbp)))
            for decision_seconds in decisions:
                decision_ns = open_ns + decision_seconds * 1_000_000_000
                entry = _quote_at_or_after(bbo, decision_ns)
                if entry is None or int(entry[1]) != instrument:
                    excluded["missing_entry_quote"] += len(horizons)
                    continue
                observed_bbo = [row for row in bbo if open_ns <= row[0] < decision_ns]
                observed_trades = [
                    row for row in trades if open_ns <= row[0] < decision_ns
                ]
                observed_features = {
                    **_prefixed("observed_bbo", _bbo_stats(observed_bbo)),
                    **_prefixed("observed_trades", _trade_stats(observed_trades)),
                }
                if require_mbp:
                    observed_mbp = [
                        row for row in mbp if open_ns <= row[0] < decision_ns
                    ]
                    observed_features.update(
                        _prefixed("observed_mbp", _mbp_stats(observed_mbp))
                    )
                entry_bid, entry_ask = entry[2] / PRICE_SCALE, entry[3] / PRICE_SCALE
                entry_mid = (entry_bid + entry_ask) / 2
                for horizon_seconds in horizons:
                    exit_ns = decision_ns + horizon_seconds * 1_000_000_000
                    exit_quote = _quote_at_or_after(bbo, exit_ns)
                    if exit_quote is None or int(exit_quote[1]) != instrument:
                        excluded["missing_exit_quote"] += 1
                        continue
                    exit_bid = exit_quote[2] / PRICE_SCALE
                    exit_ask = exit_quote[3] / PRICE_SCALE
                    exit_mid = (exit_bid + exit_ask) / 2
                    mid_move = exit_mid - entry_mid
                    output.append({
                        "session_date": day_text,
                        "instrument_id": instrument,
                        "decision_seconds": decision_seconds,
                        "horizon_seconds": horizon_seconds,
                        "feature_cutoff_ns": decision_ns,
                        "entry_quote_ns": int(entry[0]),
                        "exit_quote_ns": int(exit_quote[0]),
                        **pre_features,
                        **observed_features,
                        "entry_bid": entry_bid,
                        "entry_ask": entry_ask,
                        "exit_bid": exit_bid,
                        "exit_ask": exit_ask,
                        "mid_move_points": mid_move,
                        "direction": 1 if mid_move > 0 else -1 if mid_move < 0 else 0,
                        "long_executable_points": exit_bid - entry_ask,
                        "short_executable_points": entry_bid - exit_ask,
                    })
    finally:
        conn.close()
    return output, {
        "eligible_sessions": len(days),
        "rows": len(output),
        "decision_offsets_seconds": list(decisions),
        "horizons_seconds": list(horizons),
        "mbp_required": require_mbp,
        "excluded": dict(sorted(excluded.items())),
    }


def _finite_or_none(value):
    return value if not isinstance(value, float) or math.isfinite(value) else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-mbp", action="store_true")
    args = parser.parse_args()
    rows, quality = build_rows(args.db, require_mbp=args.require_mbp)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as target:
        for row in rows:
            target.write(json.dumps({k: _finite_or_none(v) for k, v in row.items()}) + "\n")
    print(json.dumps({**quality, "output": str(args.output)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
