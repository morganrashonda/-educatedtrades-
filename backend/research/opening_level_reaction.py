"""Measurement-only observer for accepted, failed, and unresolved NQ breaks.

The observer reads historical bars and an audited SQLite evidence store.  It
has no broker, execution, production, learning, or Tier 3 imports.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
TICK = 0.25
PRICE_SCALE = 1_000_000_000
OPEN = time(9, 30)
DECISION_SECONDS = (5, 10, 30, 60)
HORIZON_SECONDS = (30, 60, 120, 180, 300)
MAX_QUOTE_DELAY_NS = 2_000_000_000


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    instrument_id: int


@dataclass(frozen=True)
class KnownLevel:
    price: float
    names: tuple[str, ...]
    eligible_seconds: int
    allowed_sides: tuple[int, ...]


@dataclass(frozen=True)
class SessionMap:
    instrument_id: int
    levels: tuple[KnownLevel, ...]
    context: dict


@dataclass(frozen=True)
class SecondState:
    bucket_ns: int
    instrument_id: int
    event_count: int
    trade_count: int
    buy_volume: float
    sell_volume: float
    open_mid: float
    high_mid: float
    low_mid: float
    close_mid: float
    ofi: float
    bid_queue_add: float
    bid_queue_remove: float
    ask_queue_add: float
    ask_queue_remove: float
    mean_depth: float | None
    mean_queue_imbalance: float | None
    mean_spread: float | None
    mean_microprice_displacement: float | None


@dataclass(frozen=True)
class Quote:
    ts_ns: int
    bid: float
    ask: float


@dataclass(frozen=True)
class Headline:
    event_id: str
    published_at: datetime
    scope: str
    sentiment: str
    significance: str
    source: str
    symbols: tuple[str, ...]


def _ns(day: date, value: time) -> int:
    return int(datetime.combine(day, value, ET).timestamp() * 1_000_000_000)


def _bar_rows(path: Path):
    with path.open() as source:
        for line in source:
            if not line.strip():
                continue
            raw = json.loads(line)
            header = raw["hd"]
            yield Bar(
                ts=datetime.fromtimestamp(int(header["ts_event"]) / 1e9, timezone.utc),
                open=int(raw["open"]) / PRICE_SCALE,
                high=int(raw["high"]) / PRICE_SCALE,
                low=int(raw["low"]) / PRICE_SCALE,
                close=int(raw["close"]) / PRICE_SCALE,
                instrument_id=int(header["instrument_id"]),
            )


def _empty_range() -> dict:
    return {
        "high": -math.inf,
        "low": math.inf,
        "close": None,
        "instrument_id": None,
        "instrument_ids": set(),
    }


def _update_range(item: dict, bar: Bar) -> None:
    item["high"] = max(item["high"], bar.high)
    item["low"] = min(item["low"], bar.low)
    item["close"] = bar.close
    item["instrument_id"] = bar.instrument_id
    item["instrument_ids"].add(bar.instrument_id)


def _sides_for_names(names: tuple[str, ...]) -> tuple[int, ...]:
    high = any(name.endswith("high") for name in names)
    low = any(name.endswith("low") for name in names)
    if high and not low:
        return (1,)
    if low and not high:
        return (-1,)
    return (1, -1)


def _cluster_levels(levels: list[KnownLevel]) -> tuple[KnownLevel, ...]:
    """Cluster nearby levels only when they share an eligibility clock."""

    grouped: dict[int, list[KnownLevel]] = defaultdict(list)
    for level in levels:
        grouped[level.eligible_seconds].append(level)
    result: list[KnownLevel] = []
    for eligible, values in grouped.items():
        clusters: list[list[KnownLevel]] = []
        for level in sorted(values, key=lambda item: item.price):
            if clusters and abs(level.price - mean(item.price for item in clusters[-1])) <= 4 * TICK:
                clusters[-1].append(level)
            else:
                clusters.append([level])
        for cluster in clusters:
            names = tuple(sorted({name for item in cluster for name in item.names}))
            result.append(KnownLevel(
                price=mean(item.price for item in cluster),
                names=names,
                eligible_seconds=eligible,
                allowed_sides=_sides_for_names(names),
            ))
    return tuple(sorted(result, key=lambda item: (item.eligible_seconds, item.price)))


def build_session_maps_from_bars(
    bars: Iterable[Bar],
) -> tuple[dict[date, SessionMap], dict]:
    """Build levels and context using information available before each level."""

    rth: dict[date, dict] = defaultdict(_empty_range)
    overnight: dict[date, dict] = defaultdict(_empty_range)
    premarket: dict[date, dict] = defaultdict(_empty_range)
    cash_open: dict[date, tuple[float, int]] = {}
    cadence_seconds: Counter[int] = Counter()
    previous_timestamp: datetime | None = None
    source_rows = 0
    for bar in bars:
        source_rows += 1
        if previous_timestamp is not None:
            delta = int((bar.ts - previous_timestamp).total_seconds())
            if 0 < delta <= 600:
                cadence_seconds[delta] += 1
        previous_timestamp = bar.ts
        local = bar.ts.astimezone(ET)
        day = local.date()
        clock = local.time().replace(tzinfo=None)
        if time(9, 30) <= clock < time(16, 0):
            _update_range(rth[day], bar)
            if clock == time(9, 30):
                cash_open[day] = (bar.open, bar.instrument_id)
        if time(9, 0) <= clock < time(9, 30):
            _update_range(premarket[day], bar)
        if clock >= time(18, 0):
            _update_range(overnight[day + timedelta(days=1)], bar)
        elif clock < time(9, 30):
            _update_range(overnight[day], bar)

    days = sorted(rth)
    prior_day = {day: days[index - 1] for index, day in enumerate(days) if index}
    week_ranges: dict[tuple[int, int], dict] = defaultdict(_empty_range)
    for day in days:
        iso = day.isocalendar()
        item = week_ranges[(iso.year, iso.week)]
        daily = rth[day]
        item["high"] = max(item["high"], daily["high"])
        item["low"] = min(item["low"], daily["low"])
        item["close"] = daily["close"]
        item["instrument_id"] = daily["instrument_id"]
        item["instrument_ids"].update(daily["instrument_ids"])
    week_keys = sorted(week_ranges)
    prior_week = {key: week_keys[index - 1] for index, key in enumerate(week_keys) if index}

    result: dict[date, SessionMap] = {}
    excluded = Counter()
    for day in sorted(set(cash_open) & set(prior_day)):
        open_price, instrument = cash_open[day]
        previous_day = prior_day[day]
        previous = rth[previous_day]
        levels: list[KnownLevel] = []

        def add_pair(item: dict | None, prefix: str) -> None:
            if not item or item["instrument_ids"] != {instrument}:
                excluded[f"{prefix}_instrument_mismatch_or_missing"] += 1
                return
            levels.extend([
                KnownLevel(item["high"], (f"{prefix}_high",), 0, (1,)),
                KnownLevel(item["low"], (f"{prefix}_low",), 0, (-1,)),
            ])

        add_pair(previous, "prior_rth")
        add_pair(overnight.get(day), "overnight")
        add_pair(premarket.get(day), "premarket_30m")
        iso = day.isocalendar()
        current_week = (iso.year, iso.week)
        add_pair(week_ranges.get(prior_week.get(current_week)), "prior_week_rth")

        history = [
            rth[item]
            for item in days
            if item < day and rth[item]["instrument_ids"] == {instrument}
        ]
        prior_ranges = [item["high"] - item["low"] for item in history[-20:]]
        closes = [item["close"] for item in history if item["close"] is not None]
        prior_close = (
            previous["close"]
            if previous["instrument_ids"] == {instrument}
            else None
        )
        gap = open_price - prior_close if prior_close is not None else None
        mean_range = mean(prior_ranges) if prior_ranges else None
        result[day] = SessionMap(
            instrument_id=instrument,
            levels=_cluster_levels(levels),
            context={
                "cash_open": open_price,
                "prior_rth_close": prior_close,
                "gap_points": gap,
                "prior_20d_mean_range": mean_range,
                "gap_to_prior_20d_range": gap / mean_range if gap is not None and mean_range else None,
                "prior_5d_close_return": (
                    closes[-1] / closes[-6] - 1 if len(closes) >= 6 and closes[-6] else None
                ),
            },
        )
    return result, {
        "source_rows": source_rows,
        "sessions": len(result),
        "observed_bar_cadence_seconds": {
            str(seconds): count
            for seconds, count in cadence_seconds.most_common(5)
        },
        "excluded": dict(sorted(excluded.items())),
    }


def build_session_maps(path: Path) -> tuple[dict[date, SessionMap], dict]:
    return build_session_maps_from_bars(_bar_rows(path))


def load_headlines(path: Path | None) -> tuple[list[Headline], str]:
    if path is None:
        return [], "DATA_GATED"
    values = []
    with path.open() as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError("headline published_at must include a timezone")
            values.append(Headline(
                event_id=str(row["event_id"]),
                published_at=published.astimezone(timezone.utc),
                scope=str(row["scope"]),
                sentiment=str(row["sentiment"]),
                significance=str(row["significance"]),
                source=str(row["source"]),
                symbols=tuple(str(value) for value in row.get("symbols", [])),
            ))
    return sorted(values, key=lambda item: item.published_at), "PROVIDED"


def _headline_context(headlines: list[Headline], cutoff_ns: int, day: date, status: str) -> dict:
    if status == "DATA_GATED":
        return {"status": status, "events": []}
    cutoff = datetime.fromtimestamp(cutoff_ns / 1e9, timezone.utc)
    events = [
        item for item in headlines
        if item.published_at.astimezone(ET).date() == day and item.published_at <= cutoff
    ]
    return {
        "status": status,
        "events": [
            {
                "event_id": item.event_id,
                "published_at": item.published_at.isoformat(),
                "scope": item.scope,
                "sentiment": item.sentiment,
                "significance": item.significance,
                "source": item.source,
                "symbols": list(item.symbols),
            }
            for item in events
        ],
    }


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def load_session_evidence(
    conn: sqlite3.Connection, day: str, instrument_id: int,
) -> tuple[list[SecondState], list[Quote]]:
    seconds = [
        SecondState(*row)
        for row in conn.execute(
            """
            SELECT bucket_ns, instrument_id, event_count, trade_count,
                   buy_volume, sell_volume, open_mid, high_mid, low_mid,
                   close_mid, ofi, bid_queue_add, bid_queue_remove,
                   ask_queue_add, ask_queue_remove, mean_depth,
                   mean_queue_imbalance, mean_spread,
                   mean_microprice_displacement
            FROM mbp1_1s
            WHERE session_date = ? AND instrument_id = ?
            ORDER BY bucket_ns
            """,
            (day, instrument_id),
        ).fetchall()
    ]
    quotes = [
        Quote(int(row[0]), int(row[1]) / PRICE_SCALE, int(row[2]) / PRICE_SCALE)
        for row in conn.execute(
            """
            SELECT ts_event, bid_px_i, ask_px_i FROM bbo_1s
            WHERE session_date = ? AND instrument_id = ? ORDER BY ts_event
            """,
            (day, instrument_id),
        ).fetchall()
    ]
    return seconds, quotes


def _opening_range_level(seconds: list[SecondState], open_ns: int) -> tuple[KnownLevel, ...]:
    window = [row for row in seconds if open_ns <= row.bucket_ns < open_ns + 60_000_000_000]
    if not window:
        return ()
    return (
        KnownLevel(max(row.high_mid for row in window), ("opening_range_60s_high",), 60, (1,)),
        KnownLevel(min(row.low_mid for row in window), ("opening_range_60s_low",), 60, (-1,)),
    )


def _aggregate(rows: list[SecondState], level: float, side: int) -> dict:
    if not rows:
        return {"status": "MISSING_EVIDENCE"}
    buys = sum(row.buy_volume for row in rows)
    sells = sum(row.sell_volume for row in rows)
    total = buys + sells
    trade_count = sum(row.trade_count for row in rows)
    depths = [row.mean_depth for row in rows if row.mean_depth is not None]
    mean_depth = mean(depths) if depths else None
    ofi = sum(row.ofi for row in rows)
    bid_add = sum(row.bid_queue_add for row in rows)
    bid_remove = sum(row.bid_queue_remove for row in rows)
    ask_add = sum(row.ask_queue_add for row in rows)
    ask_remove = sum(row.ask_queue_remove for row in rows)
    bid_refill = bid_add / (sells + bid_remove) if sells + bid_remove else None
    ask_refill = ask_add / (buys + ask_remove) if buys + ask_remove else None
    raw_flow = (buys - sells) / total if total else None
    raw_ofi = ofi / mean_depth if mean_depth else None
    progress = side * (rows[-1].close_mid - rows[0].open_mid)
    effort = buys if side > 0 else sells
    distance = side * (rows[-1].close_mid - level)
    refill_balance = (
        side * (bid_refill - ask_refill)
        if bid_refill is not None and ask_refill is not None else None
    )
    same_side_refill = bid_refill if side > 0 else ask_refill
    opposing_refill = ask_refill if side > 0 else bid_refill
    flow_directional = side * raw_flow if raw_flow is not None else None
    ofi_directional = side * raw_ofi if raw_ofi is not None else None
    if distance >= TICK and flow_directional is not None and flow_directional > 0 and ofi_directional is not None and ofi_directional > 0:
        classification = "accepted_break"
        mechanism = "accepted_flow"
    elif distance <= -TICK and flow_directional is not None and flow_directional < 0 and ofi_directional is not None and ofi_directional < 0:
        classification = "failed_break"
        mechanism = "opposite_dominance"
    else:
        classification = "unresolved"
        mechanism = (
            "absorption_divergence"
            if distance <= 0
            and flow_directional is not None and flow_directional > 0
            and refill_balance is not None and refill_balance < 0
            else "mixed_or_missing"
        )
    queues = [row.mean_queue_imbalance for row in rows if row.mean_queue_imbalance is not None]
    spreads = [row.mean_spread for row in rows if row.mean_spread is not None]
    micros = [
        row.mean_microprice_displacement
        for row in rows if row.mean_microprice_displacement is not None
    ]
    closes = [row.close_mid for row in rows]
    distances = [side * (value - level) for value in closes]
    directional_returns = [side * (row.close_mid - row.open_mid) for row in rows]
    return {
        "status": "OK",
        "classification": classification,
        "mechanism": mechanism,
        "feature_cutoff_exclusive_ns": rows[-1].bucket_ns + 1_000_000_000,
        "trade_count": trade_count,
        "buy_volume": buys,
        "sell_volume": sells,
        "total_aggressive_volume": total,
        "distance_from_level_points": distance,
        "price_progress_points": progress,
        "price_range_points": max(row.high_mid for row in rows) - min(row.low_mid for row in rows),
        "maximum_distance_from_level_points": max(distances),
        "minimum_distance_from_level_points": min(distances),
        "directional_realized_volatility_points": math.sqrt(
            sum(value * value for value in directional_returns)
        ),
        "directional_aggressive_volume": effort,
        "progress_per_aggressive_contract": progress / effort if effort else None,
        "signed_trade_imbalance": raw_flow,
        "directional_trade_imbalance": flow_directional,
        "ofi": ofi,
        "depth_normalized_ofi": raw_ofi,
        "directional_depth_normalized_ofi": ofi_directional,
        "bid_refill_ratio": bid_refill,
        "ask_refill_ratio": ask_refill,
        "directional_refill_balance": refill_balance,
        "same_side_refill_proxy": same_side_refill,
        "opposing_side_refill_proxy": opposing_refill,
        "opposing_side_attack_volume": buys if side > 0 else sells,
        "bid_add_volume": bid_add,
        "ask_add_volume": ask_add,
        "bid_remove_volume": bid_remove,
        "ask_remove_volume": ask_remove,
        "top_level_addition_imbalance": (
            (bid_add - ask_add) / (bid_add + ask_add)
            if bid_add + ask_add else None
        ),
        "top_level_removal_imbalance": (
            (bid_remove - ask_remove) / (bid_remove + ask_remove)
            if bid_remove + ask_remove else None
        ),
        "mean_queue_imbalance": mean(queues) if queues else None,
        "first_queue_imbalance": queues[0] if queues else None,
        "last_queue_imbalance": queues[-1] if queues else None,
        "queue_imbalance_change": queues[-1] - queues[0] if queues else None,
        "mean_spread_points": mean(spreads) if spreads else None,
        "minimum_spread_points": min(spreads) if spreads else None,
        "maximum_spread_points": max(spreads) if spreads else None,
        "mean_microprice_displacement_points": mean(micros) if micros else None,
        "first_microprice_displacement_points": micros[0] if micros else None,
        "last_microprice_displacement_points": micros[-1] if micros else None,
        "microprice_displacement_change_points": (
            micros[-1] - micros[0] if micros else None
        ),
        "mean_depth": mean_depth,
        "minimum_depth": min(depths) if depths else None,
        "maximum_depth": max(depths) if depths else None,
        "outside_fraction": sum(
            side * (row.close_mid - level) >= TICK for row in rows
        ) / len(rows),
        "rows": len(rows),
    }


def _quote_at_or_after(quotes: list[Quote], target_ns: int) -> Quote | None:
    for quote in quotes:
        if quote.ts_ns >= target_ns:
            return quote if quote.ts_ns - target_ns <= MAX_QUOTE_DELAY_NS else None
    return None


def _side_path(entry: Quote, path: list[Quote], side: int) -> dict:
    entry_price = entry.ask if side > 0 else entry.bid
    points = [
        (quote.bid - entry_price) if side > 0 else (entry_price - quote.ask)
        for quote in path
    ]
    best_index = max(range(len(points)), key=points.__getitem__)
    worst_index = min(range(len(points)), key=points.__getitem__)
    return {
        "side": side,
        "entry_price": entry_price,
        "exit_price": path[-1].bid if side > 0 else path[-1].ask,
        "crossing_points": points[-1],
        "one_tick_each_side_stress_points": points[-1] - 2 * TICK,
        "mfe_points": max(points[best_index], 0.0),
        "mae_points": max(-points[worst_index], 0.0),
        "mfe_timestamp_ns": path[best_index].ts_ns,
        "mae_timestamp_ns": path[worst_index].ts_ns,
    }


def _outcomes(
    quotes: list[Quote], decision_ns: int, break_side: int, level: float,
    horizon_seconds: tuple[int, ...] = HORIZON_SECONDS,
) -> dict:
    entry = _quote_at_or_after(quotes, decision_ns)
    if entry is None:
        return {"status": "MISSING_ENTRY_QUOTE", "horizons": {}}
    horizons = {}
    for horizon in horizon_seconds:
        exit_quote = _quote_at_or_after(quotes, decision_ns + horizon * 1_000_000_000)
        if exit_quote is None:
            horizons[str(horizon)] = None
            continue
        path = [quote for quote in quotes if entry.ts_ns <= quote.ts_ns <= exit_quote.ts_ns]
        if not path:
            horizons[str(horizon)] = None
            continue
        locations = [
            break_side * (((quote.bid + quote.ask) / 2) - level)
            for quote in path
        ]
        elapsed = [
            (path[index + 1].ts_ns - path[index].ts_ns) / 1e9
            for index in range(len(path) - 1)
        ] + [0.0]
        horizons[str(horizon)] = {
            "entry_quote_ns": entry.ts_ns,
            "exit_quote_ns": exit_quote.ts_ns,
            "outside_observations": sum(value >= TICK for value in locations),
            "inside_observations": sum(value <= -TICK for value in locations),
            "boundary_observations": sum(-TICK < value < TICK for value in locations),
            "outside_fraction": sum(value >= TICK for value in locations) / len(locations),
            "inside_fraction": sum(value <= -TICK for value in locations) / len(locations),
            "observed_path_seconds": sum(elapsed),
            "outside_seconds": sum(
                duration
                for value, duration in zip(locations, elapsed)
                if value >= TICK
            ),
            "inside_seconds": sum(
                duration
                for value, duration in zip(locations, elapsed)
                if value <= -TICK
            ),
            "boundary_seconds": sum(
                duration
                for value, duration in zip(locations, elapsed)
                if -TICK < value < TICK
            ),
            "continuation": _side_path(entry, path, break_side),
            "reversal": _side_path(entry, path, -break_side),
        }
    return {"status": "OK", "horizons": horizons}


def observe_level(
    *,
    day: date,
    level: KnownLevel,
    seconds: list[SecondState],
    quotes: list[Quote],
    headlines: list[Headline],
    headline_status: str,
    context: dict,
    decision_seconds: tuple[int, ...] = DECISION_SECONDS,
    horizon_seconds: tuple[int, ...] = HORIZON_SECONDS,
) -> list[dict]:
    open_ns = _ns(day, OPEN)
    eligible_ns = open_ns + level.eligible_seconds * 1_000_000_000
    events = []
    next_eligible_ns = eligible_ns
    for index in range(1, len(seconds)):
        row, previous = seconds[index], seconds[index - 1]
        observed_ns = row.bucket_ns + 1_000_000_000
        if observed_ns < next_eligible_ns:
            continue
        found_side = None
        for side in level.allowed_sides:
            threshold = level.price + side * TICK
            if side * previous.close_mid < side * threshold <= side * row.close_mid:
                found_side = side
                break
        if found_side is None:
            continue
        event = {
            "session_date": day.isoformat(),
            "instrument_id": row.instrument_id,
            "level": level.price,
            "level_names": list(level.names),
            "level_eligible_ns": eligible_ns,
            "break_side": found_side,
            "break_bucket_ns": row.bucket_ns,
            "break_observed_ns": observed_ns,
            "context": context,
            "decisions": {},
        }
        for offset in decision_seconds:
            decision_ns = observed_ns + offset * 1_000_000_000
            feature_rows = [
                item for item in seconds
                if observed_ns <= item.bucket_ns < decision_ns
            ]
            coverage_complete = bool(feature_rows) and (
                feature_rows[0].bucket_ns <= observed_ns
                and feature_rows[-1].bucket_ns + 1_000_000_000 >= decision_ns
            )
            features = (
                _aggregate(feature_rows, level.price, found_side)
                if coverage_complete
                else {
                    "status": "MISSING_EVIDENCE_AT_DECISION",
                    "classification": "unresolved",
                    "mechanism": "mixed_or_missing",
                    "rows": len(feature_rows),
                }
            )
            classification = features.get("classification", "unresolved")
            expected_side = (
                found_side if classification == "accepted_break"
                else -found_side if classification == "failed_break"
                else 0
            )
            event["decisions"][str(offset)] = {
                "decision_ns": decision_ns,
                "feature_start_ns": observed_ns,
                "feature_end_exclusive_ns": decision_ns,
                "features": features,
                "expected_side": expected_side,
                "headline_context": _headline_context(
                    headlines, decision_ns, day, headline_status
                ),
                "outcomes": _outcomes(
                    quotes, decision_ns, found_side, level.price,
                    horizon_seconds=horizon_seconds,
                ),
            }
        events.append(event)
        next_eligible_ns = observed_ns + 300 * 1_000_000_000
    return events


def _summary(events: list[dict]) -> dict:
    by_decision = {}
    for offset in DECISION_SECONDS:
        decisions = [event["decisions"][str(offset)] for event in events]
        classes = Counter(item["features"].get("classification", "unresolved") for item in decisions)
        mechanisms = Counter(item["features"].get("mechanism", "mixed_or_missing") for item in decisions)
        expected = {}
        for horizon in HORIZON_SECONDS:
            values = []
            for item in decisions:
                side = item["expected_side"]
                outcome = item["outcomes"]["horizons"].get(str(horizon))
                if not side or outcome is None:
                    continue
                branch = "continuation" if side == outcome["continuation"]["side"] else "reversal"
                values.append(outcome[branch]["one_tick_each_side_stress_points"])
            expected[str(horizon)] = {
                "n": len(values),
                "mean_points": mean(values) if values else None,
                "wins": sum(value > 0 for value in values),
                "win_rate": sum(value > 0 for value in values) / len(values) if values else None,
            }
        by_decision[str(offset)] = {
            "events": len(decisions),
            "classifications": dict(sorted(classes.items())),
            "mechanisms": dict(sorted(mechanisms.items())),
            "descriptive_expected_side_outcomes": expected,
        }
    return {"events": len(events), "by_decision_seconds": by_decision}


def run(
    db_path: Path,
    bars_path: Path,
    *,
    headline_path: Path | None = None,
) -> dict:
    maps, map_quality = build_session_maps(bars_path)
    headlines, headline_status = load_headlines(headline_path)
    events = []
    sessions = []
    conn = _readonly_connection(db_path)
    try:
        available = [
            row[0] for row in conn.execute(
                """
            SELECT DISTINCT session_date FROM requests
                WHERE schema_name = 'mbp-1' AND status = 'complete'
                ORDER BY session_date
                """
            )
        ]
        for day_text in available:
            day = date.fromisoformat(day_text)
            session_map = maps.get(day)
            if session_map is None:
                sessions.append({"session_date": day_text, "status": "NO_POINT_IN_TIME_MAP"})
                continue
            seconds, quotes = load_session_evidence(conn, day_text, session_map.instrument_id)
            if not seconds or not quotes:
                sessions.append({"session_date": day_text, "status": "MISSING_EVIDENCE"})
                continue
            open_ns = _ns(day, OPEN)
            levels = _cluster_levels([
                *session_map.levels,
                *_opening_range_level(seconds, open_ns),
            ])
            day_events = []
            for level in levels:
                day_events.extend(observe_level(
                    day=day,
                    level=level,
                    seconds=seconds,
                    quotes=quotes,
                    headlines=headlines,
                    headline_status=headline_status,
                    context=session_map.context,
                ))
            events.extend(day_events)
            sessions.append({
                "session_date": day_text,
                "status": "OK",
                "levels": len(levels),
                "events": len(day_events),
            })
    finally:
        conn.close()
    return {
        "status": "DISCOVERY_MEASUREMENT_ONLY",
        "spec": "docs/OPENING_LEVEL_REACTION_OBSERVER_SPEC_20260819.md",
        "map_quality": map_quality,
        "headline_context_status": headline_status,
        "sessions": sessions,
        "summary": _summary(events),
        "events": events,
        "limitations": [
            "Historical MBP dates were inspected in related research and are not untouched confirmation.",
            "The initial evidence window ends near 09:36 ET, so later outcomes are explicitly missing.",
            "Classifications are descriptive hypotheses, not proof of economic causation or edge.",
            "No result from this report authorizes production, learning, Tier 3, or orders.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--nq-bars", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headlines", type=Path)
    args = parser.parse_args()
    report = run(args.db, args.nq_bars, headline_path=args.headlines)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
