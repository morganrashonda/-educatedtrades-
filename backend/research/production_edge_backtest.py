"""Production-parity, leakage-resistant edge testing for Main 5.

This module is deliberately research-only.  It never imports ``main``, opens
the production pattern database, calls a broker, or writes learning state.
It reuses the pure indicator functions in ``patterns.py`` and consumes a
frozen OHLCV CSV so every run is reproducible.

The primary estimate is expanding-window walk-forward performance.  A purged
6-group/2-test-group CPCV report is included as a robustness diagnostic; it is
not described as a causal deployment estimate because some CPCV training
groups can occur after a test group.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import random
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist, mean
from typing import Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
import patterns as production_patterns  # noqa: E402


SCHEMA_VERSION = "production-edge-backtest-v1"
NEW_YORK = ZoneInfo("America/New_York")


def _is_regular_session_start(timestamp: datetime) -> bool:
    local = timestamp.astimezone(NEW_YORK)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class StrategyConfig:
    name: str = "production_defaults"
    rsi_period: int = 14
    adx_period: int = 14
    ema_short: int = 20
    ema_long: int = 50
    volatility_period: int = 20
    indicator_fetch_bars: int = 200
    oversold: float = 30.0
    overbought: float = 70.0
    minimum_conviction: float = 0.10
    execution_conviction: float = 0.20
    stop_loss_pct: float = 0.025
    take_profit_pct: float = 0.030
    max_hold_bars: int = 13
    round_trip_cost_bps: float = 3.0
    slippage_bps_per_side: float = 1.0
    risk_fraction: float = 0.004


@dataclass(frozen=True)
class Signal:
    index: int
    timestamp: datetime
    symbol: str
    side: int
    conviction: float
    regime: str
    strategy: str
    pattern_key: str
    record_pattern_key: str = ""


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_index: int
    entry_index: int
    exit_index: int
    signal_time: datetime
    entry_time: datetime
    exit_time: datetime
    side: int
    entry_price: float
    exit_price: float
    gross_return: float
    net_return: float
    reason: str
    pattern_key: str
    record_pattern_key: str = ""


@dataclass(frozen=True)
class Split:
    split_id: str
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    purged_indices: tuple[int, ...] = ()


def _parse_timestamp(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def load_bars_csv(path: Path, default_symbol: Optional[str] = None) -> list[Bar]:
    """Load strict timestamp/symbol/OHLCV CSV input.

    ``symbol`` may be omitted only when ``default_symbol`` is supplied.
    Duplicate symbol/timestamp rows, non-positive prices, crossed OHLC ranges,
    and non-monotonic per-symbol timestamps are rejected rather than repaired.
    """

    rows: list[Bar] = []
    seen: set[tuple[str, datetime]] = set()
    last_time: dict[str, datetime] = {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "open", "high", "low", "close"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"bars CSV missing columns: {sorted(missing)}")
        for line_number, raw in enumerate(reader, start=2):
            symbol = (raw.get("symbol") or default_symbol or "").strip().upper()
            if not symbol:
                raise ValueError(f"line {line_number}: symbol is required")
            timestamp = _parse_timestamp(raw["timestamp"])
            values = [float(raw[name]) for name in ("open", "high", "low", "close")]
            open_, high, low, close = values
            volume = float(raw.get("volume") or 0.0)
            if not all(math.isfinite(value) and value > 0 for value in values):
                raise ValueError(f"line {line_number}: OHLC must be finite and positive")
            if high < max(open_, close) or low > min(open_, close) or high < low:
                raise ValueError(f"line {line_number}: invalid OHLC range")
            if volume < 0 or not math.isfinite(volume):
                raise ValueError(f"line {line_number}: invalid volume")
            key = (symbol, timestamp)
            if key in seen:
                raise ValueError(f"line {line_number}: duplicate {symbol} {timestamp.isoformat()}")
            if symbol in last_time and timestamp <= last_time[symbol]:
                raise ValueError(f"line {line_number}: {symbol} timestamps are not increasing")
            seen.add(key)
            last_time[symbol] = timestamp
            rows.append(Bar(timestamp, symbol, open_, high, low, close, volume))
    if not rows:
        raise ValueError("bars CSV is empty")
    return sorted(rows, key=lambda bar: (bar.timestamp, bar.symbol))


def load_point_in_time_news(path: Optional[Path]) -> dict[tuple[str, datetime], float]:
    """Validate an optional real-news input without using it as price alpha.

    Current Main 5 treats news as telemetry and a fail-closed availability
    gate.  This loader exists so a future, separately preregistered news model
    can use genuine point-in-time observations.  It never substitutes price
    momentum or synthetic headlines.
    """

    if path is None:
        return {}
    observations: dict[tuple[str, datetime], float] = {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"decision_timestamp", "observed_at", "symbol", "score", "source"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"news CSV missing columns: {sorted(missing)}")
        for line_number, raw in enumerate(reader, start=2):
            decision = _parse_timestamp(raw["decision_timestamp"])
            observed = _parse_timestamp(raw["observed_at"])
            if observed > decision:
                raise ValueError(f"line {line_number}: news observed after decision time")
            symbol = raw["symbol"].strip().upper()
            source = raw["source"].strip()
            score = float(raw["score"])
            if not symbol or not source or not math.isfinite(score) or not -1 <= score <= 1:
                raise ValueError(f"line {line_number}: invalid news observation")
            observations[(symbol, decision)] = score
    return observations


def aggregate_bars(bars: Sequence[Bar], minutes: int) -> list[Bar]:
    """Aggregate smaller UTC bars into deterministic wall-clock buckets."""

    if minutes <= 0:
        raise ValueError("aggregation minutes must be positive")
    buckets: dict[tuple[str, datetime], list[Bar]] = defaultdict(list)
    for bar in bars:
        minute = (bar.timestamp.minute // minutes) * minutes
        bucket = bar.timestamp.replace(minute=0, second=0, microsecond=0)
        bucket = bucket.replace(minute=minute % 60)
        if minutes >= 60:
            total_minutes = bar.timestamp.hour * 60 + bar.timestamp.minute
            start = (total_minutes // minutes) * minutes
            bucket = bar.timestamp.replace(
                hour=(start // 60) % 24, minute=start % 60, second=0, microsecond=0
            )
        buckets[(bar.symbol, bucket)].append(bar)
    result = []
    for (symbol, timestamp), group in sorted(buckets.items(), key=lambda item: (item[0][1], item[0][0])):
        result.append(
            Bar(
                timestamp,
                symbol,
                group[0].open,
                max(item.high for item in group),
                min(item.low for item in group),
                group[-1].close,
                sum(item.volume for item in group),
            )
        )
    return result


def _pattern_key(symbol: str, conviction: float, rsi: float) -> str:
    sentiment_zone = production_patterns.classify_sentiment_zone(conviction)
    zone = production_patterns.classify_rsi_zone(rsi)
    # Main 5 does not pass previous EMAs into PatternEngine.evaluate(), so
    # detect_ema_cross() returns no_cross regardless of current alignment.
    return f"{symbol}|{sentiment_zone}|{zone}|no_cross"


def compute_signal_candidates(bars: Sequence[Bar], config: StrategyConfig) -> list[Signal]:
    """Compute raw production price signals using information through bar t.

    Entries are not filled here.  The simulator fills at bar t+1 open, which
    prevents a signal from trading the same close that created it.
    """

    by_symbol: dict[str, list[tuple[int, Bar]]] = defaultdict(list)
    for global_index, bar in enumerate(bars):
        by_symbol[bar.symbol].append((global_index, bar))

    snapshots: dict[int, dict] = {}
    for symbol, indexed in by_symbol.items():
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        for local_index, (global_index, bar) in enumerate(indexed):
            highs.append(bar.high)
            lows.append(bar.low)
            closes.append(bar.close)
            if local_index + 1 < max(config.ema_long, 2 * config.adx_period + 1):
                continue
            history_start = max(0, len(closes) - config.indicator_fetch_bars)
            recent_highs = highs[history_start:]
            recent_lows = lows[history_start:]
            recent_closes = closes[history_start:]
            rsi = production_patterns.compute_rsi(recent_closes, config.rsi_period)
            adx = production_patterns.compute_adx(
                recent_highs, recent_lows, recent_closes, config.adx_period
            )
            ema_s = production_patterns.compute_ema(recent_closes, config.ema_short)
            ema_l = production_patterns.compute_ema(recent_closes, config.ema_long)
            volatility = production_patterns.realized_volatility_pct(
                recent_closes, config.volatility_period
            )
            if None in (rsi, adx, ema_s, ema_l, volatility):
                continue
            snapshots[global_index] = {
                "symbol": symbol,
                "timestamp": bar.timestamp,
                "rsi": float(rsi),
                "adx": float(adx),
                "ema_short": float(ema_s),
                "ema_long": float(ema_l),
                "volatility": float(volatility),
                # A production decision is executable only when the next bar
                # opens during RTH. This correctly maps the final premarket
                # bar to a 09:30 entry and refuses a 16:00 extended-hours fill.
                "entry_eligible": (
                    local_index + 1 < len(indexed)
                    and _is_regular_session_start(indexed[local_index + 1][1].timestamp)
                ),
            }

    by_time: dict[datetime, list[dict]] = defaultdict(list)
    for snapshot in snapshots.values():
        by_time[snapshot["timestamp"]].append(snapshot)

    signals: list[Signal] = []
    for global_index, snapshot in sorted(snapshots.items()):
        if not snapshot["entry_eligible"]:
            continue
        peers = by_time[snapshot["timestamp"]]
        avg_adx = mean(item["adx"] for item in peers)
        regime = production_patterns.classify_regime(avg_adx)
        strategy = production_patterns.get_strategy_for_regime(regime)
        trend_conviction = production_patterns.trend_conviction(
            snapshot["adx"], snapshot["ema_short"], snapshot["ema_long"],
            snapshot["volatility"],
        )
        side = 0
        conviction = 0.0
        if strategy == "mean_reversion":
            if snapshot["rsi"] <= config.oversold:
                side = 1
            elif snapshot["rsi"] >= config.overbought:
                side = -1
            if side:
                conviction = production_patterns.mean_reversion_conviction(snapshot["rsi"])
        elif strategy == "trend_following":
            conviction = trend_conviction
            if conviction >= config.minimum_conviction:
                side = 1
            elif conviction <= -config.minimum_conviction:
                side = -1
        if side and abs(conviction) >= config.execution_conviction:
            signals.append(
                Signal(
                    global_index,
                    snapshot["timestamp"],
                    snapshot["symbol"],
                    side,
                    float(conviction),
                    regime,
                    strategy,
                    _pattern_key(snapshot["symbol"], trend_conviction, snapshot["rsi"]),
                    # Production currently records rsi_value=50.0 after a
                    # fill. Preserve that behavior so the backtest can reveal
                    # evaluation/learning key mismatches instead of hiding it.
                    _pattern_key(snapshot["symbol"], conviction, 50.0),
                )
            )
    return signals


def simulate_trades(
    bars: Sequence[Bar],
    signals: Sequence[Signal],
    config: StrategyConfig,
    allowed_signal_indices: Optional[set[int]] = None,
) -> list[Trade]:
    """Fill next-bar entries and conservatively resolve stop/target conflicts."""

    by_symbol: dict[str, list[int]] = defaultdict(list)
    rth_by_symbol: dict[str, list[int]] = defaultdict(list)
    location: dict[int, int] = {}
    rth_location: dict[int, int] = {}
    for global_index, bar in enumerate(bars):
        location[global_index] = len(by_symbol[bar.symbol])
        by_symbol[bar.symbol].append(global_index)
        if _is_regular_session_start(bar.timestamp):
            rth_location[global_index] = len(rth_by_symbol[bar.symbol])
            rth_by_symbol[bar.symbol].append(global_index)
    occupied_until: dict[str, int] = defaultdict(lambda: -1)
    trades: list[Trade] = []
    half_slippage = config.slippage_bps_per_side / 10_000.0
    fixed_cost = config.round_trip_cost_bps / 10_000.0

    for signal in sorted(signals, key=lambda item: item.index):
        if allowed_signal_indices is not None and signal.index not in allowed_signal_indices:
            continue
        symbol_indices = by_symbol[signal.symbol]
        local = location[signal.index]
        if local + 1 >= len(symbol_indices) or signal.index <= occupied_until[signal.symbol]:
            continue
        entry_index = symbol_indices[local + 1]
        if entry_index not in rth_location:
            # Signal construction should make this impossible; fail closed if
            # a caller supplies a hand-built signal that violates the gate.
            continue
        entry_bar = bars[entry_index]
        entry = entry_bar.open * (1 + half_slippage if signal.side > 0 else 1 - half_slippage)
        stop = entry * (1 - config.stop_loss_pct if signal.side > 0 else 1 + config.stop_loss_pct)
        target = entry * (1 + config.take_profit_pct if signal.side > 0 else 1 - config.take_profit_pct)
        exit_index = entry_index
        exit_price = entry
        reason = "end_of_data"
        rth_indices = rth_by_symbol[signal.symbol]
        entry_rth_local = rth_location[entry_index]
        end_rth_local = min(
            len(rth_indices) - 1, entry_rth_local + config.max_hold_bars - 1
        )
        for candidate_local in range(entry_rth_local, end_rth_local + 1):
            candidate_index = rth_indices[candidate_local]
            bar = bars[candidate_index]
            stop_hit = bar.low <= stop if signal.side > 0 else bar.high >= stop
            target_hit = bar.high >= target if signal.side > 0 else bar.low <= target
            if stop_hit:
                exit_index, exit_price, reason = candidate_index, stop, "stop"
                break
            if target_hit:
                exit_index, exit_price, reason = candidate_index, target, "target"
                break
            exit_index, exit_price, reason = candidate_index, bar.close, "time"
        exit_price *= 1 - half_slippage if signal.side > 0 else 1 + half_slippage
        gross = signal.side * (exit_price - entry) / entry
        net = gross - fixed_cost
        trades.append(
            Trade(
                signal.symbol,
                signal.index,
                entry_index,
                exit_index,
                signal.timestamp,
                entry_bar.timestamp,
                bars[exit_index].timestamp,
                signal.side,
                entry,
                exit_price,
                gross,
                net,
                reason,
                signal.pattern_key,
                signal.record_pattern_key,
            )
        )
        occupied_until[signal.symbol] = exit_index
    return trades


def _equity_returns(trades: Sequence[Trade], risk_fraction: float) -> list[float]:
    return [trade.net_return * risk_fraction / 0.025 for trade in trades]


def summarize(trades: Sequence[Trade], risk_fraction: float = 0.004) -> dict:
    returns = _equity_returns(trades, risk_fraction)
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak)
    std = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    trade_sharpe = mean(returns) / std if std > 1e-12 else 0.0
    mean_return_z = trade_sharpe * math.sqrt(len(returns)) if returns else 0.0
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) if trades else None,
        "mean_net_return_pct": mean([t.net_return for t in trades]) * 100 if trades else None,
        "total_return_pct": (equity - 1) * 100,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "trade_sharpe": trade_sharpe,
        "mean_return_z_score": mean_return_z,
        "max_drawdown_pct": max_drawdown * 100,
    }


def contiguous_groups(indices: Sequence[int], groups: int = 6) -> list[tuple[int, ...]]:
    if groups < 2 or len(indices) < groups:
        raise ValueError("need at least one observation per chronological group")
    chunks = np.array_split(np.asarray(sorted(indices), dtype=int), groups)
    return [tuple(int(value) for value in chunk) for chunk in chunks]


def chronological_groups(
    indices: Sequence[int], bars: Sequence[Bar], groups: int = 6
) -> list[tuple[int, ...]]:
    """Split chronologically without separating symbols at one timestamp."""

    by_timestamp: dict[datetime, list[int]] = defaultdict(list)
    for index in sorted(indices):
        by_timestamp[bars[index].timestamp].append(index)
    timestamps = sorted(by_timestamp)
    if len(timestamps) < groups:
        raise ValueError("need at least one distinct timestamp per chronological group")
    timestamp_chunks = np.array_split(np.asarray(timestamps, dtype=object), groups)
    return [
        tuple(sorted(index for timestamp in chunk for index in by_timestamp[timestamp]))
        for chunk in timestamp_chunks
    ]


def _purge_training(
    train: set[int], test: set[int], bars: Sequence[Bar], purge_bars: int, embargo_bars: int
) -> tuple[set[int], set[int]]:
    if purge_bars < 0 or embargo_bars < 0:
        raise ValueError("purge and embargo must be non-negative")
    by_symbol: dict[str, list[int]] = defaultdict(list)
    local_position: dict[int, int] = {}
    for index, bar in enumerate(bars):
        local_position[index] = len(by_symbol[bar.symbol])
        by_symbol[bar.symbol].append(index)
    forbidden: set[int] = set()
    for test_index in test:
        symbol = bars[test_index].symbol
        sequence = by_symbol[symbol]
        position = local_position[test_index]
        start = max(0, position - purge_bars)
        end = min(len(sequence), position + embargo_bars + 1)
        forbidden.update(sequence[start:end])
    removed = train & forbidden
    return train - forbidden, removed


def cpcv_6x2_splits(
    indices: Sequence[int], bars: Sequence[Bar], purge_bars: int, embargo_bars: int
) -> list[Split]:
    groups = chronological_groups(indices, bars, 6)
    universe = set(indices)
    splits = []
    for left, right in itertools.combinations(range(6), 2):
        test = set(groups[left]) | set(groups[right])
        train, removed = _purge_training(
            universe - test, test, bars, purge_bars, embargo_bars
        )
        splits.append(
            Split(
                f"cpcv-g{left + 1}-g{right + 1}",
                tuple(sorted(train)),
                tuple(sorted(test)),
                tuple(sorted(removed)),
            )
        )
    return splits


def expanding_walk_forward_splits(
    indices: Sequence[int], bars: Sequence[Bar], groups: int, purge_bars: int
) -> list[Split]:
    chunks = chronological_groups(indices, bars, groups)
    splits = []
    for test_group in range(1, groups):
        train = set(itertools.chain.from_iterable(chunks[:test_group]))
        test = set(chunks[test_group])
        train, removed = _purge_training(train, test, bars, purge_bars, 0)
        splits.append(
            Split(
                f"walk-forward-{test_group}",
                tuple(sorted(train)),
                tuple(sorted(test)),
                tuple(sorted(removed)),
            )
        )
    return splits


def _pattern_allowlist(train_trades: Sequence[Trade], family_size: int) -> set[str]:
    outcomes: dict[str, list[float]] = defaultdict(list)
    for trade in train_trades:
        outcomes[trade.record_pattern_key or trade.pattern_key].append(trade.net_return)
    allowed = set()
    for key, values in outcomes.items():
        wins = sum(value > 0 for value in values)
        stats = production_patterns.PatternStats(
            pattern_id=hashlib.sha256(key.encode()).hexdigest()[:16],
            count=len(values),
            wins=wins,
            losses=len(values) - wins,
            total_profit_pct=sum(values) * 100,
        )
        if stats.corrected_signal_strength(max(1, family_size)) > 0:
            allowed.add(key)
    return allowed


def evaluate_split(
    bars: Sequence[Bar], signals: Sequence[Signal], config: StrategyConfig, split: Split,
    track: str,
) -> dict:
    train_set = set(split.train_indices)
    test_set = set(split.test_indices)
    if track == "cold_start":
        selected = [signal for signal in signals if signal.strategy == "mean_reversion"]
    elif track == "raw_price":
        selected = list(signals)
    elif track == "trained_pattern":
        train_trades = simulate_trades(bars, signals, config, train_set)
        family_size = len({trade.pattern_key for trade in train_trades})
        allowed = _pattern_allowlist(train_trades, family_size)
        selected = [
            signal for signal in signals
            if signal.strategy == "mean_reversion" or signal.pattern_key in allowed
        ]
    else:
        raise ValueError(f"unknown track: {track}")
    test_trades = simulate_trades(bars, selected, config, test_set)
    return {
        "split_id": split.split_id,
        "train_observations": len(split.train_indices),
        "test_observations": len(split.test_indices),
        "purged_observations": len(split.purged_indices),
        "metrics": summarize(test_trades, config.risk_fraction),
    }


def block_bootstrap_mean(
    trades: Sequence[Trade], seed: int = 7, samples: int = 5000,
    block_days: int = 5,
) -> Optional[list[float]]:
    if not trades:
        return None
    daily: dict[str, float] = defaultdict(float)
    for trade in trades:
        daily[trade.entry_time.date().isoformat()] += trade.net_return
    values = [daily[day] for day in sorted(daily)]
    rng = random.Random(seed)
    block_days = max(1, min(block_days, len(values)))
    estimates = []
    for _ in range(samples):
        sampled = []
        while len(sampled) < len(values):
            start = rng.randrange(len(values))
            sampled.extend(
                values[(start + offset) % len(values)] for offset in range(block_days)
            )
        estimates.append(mean(sampled[:len(values)]))
    estimates.sort()
    return [estimates[int(samples * 0.025)] * 100, estimates[int(samples * 0.975)] * 100]


def random_side_baseline(
    bars: Sequence[Bar], signals: Sequence[Signal], config: StrategyConfig, seed: int = 11
) -> dict:
    rng = random.Random(seed)
    randomized = [
        Signal(
            item.index, item.timestamp, item.symbol,
            1 if rng.random() < 0.5 else -1,
            item.conviction, item.regime, item.strategy, item.pattern_key,
            item.record_pattern_key,
        )
        for item in signals
    ]
    return summarize(simulate_trades(bars, randomized, config), config.risk_fraction)


def sidak_adjusted_p(z_score: float, observations: int, trials: int) -> Optional[float]:
    if observations < 2 or trials < 1:
        return None
    one_sided = 1 - NormalDist().cdf(z_score)
    return 1 - (1 - one_sided) ** trials


def buy_and_hold_benchmark(bars: Sequence[Bar]) -> dict:
    by_symbol: dict[str, list[Bar]] = defaultdict(list)
    for bar in bars:
        by_symbol[bar.symbol].append(bar)
    returns = {
        symbol: (series[-1].close / series[0].close - 1) * 100
        for symbol, series in by_symbol.items() if len(series) > 1
    }
    return {
        "per_symbol_return_pct": returns,
        "equal_weight_return_pct": mean(returns.values()) if returns else None,
    }


def trade_attribution(trades: Sequence[Trade]) -> dict:
    dimensions = {
        "symbol": lambda trade: trade.symbol,
        "side": lambda trade: "long" if trade.side > 0 else "short",
        "exit_reason": lambda trade: trade.reason,
    }
    output = {}
    for name, key_function in dimensions.items():
        buckets: dict[str, list[Trade]] = defaultdict(list)
        for trade in trades:
            buckets[key_function(trade)].append(trade)
        output[name] = {
            key: summarize(values) for key, values in sorted(buckets.items())
        }
    return output


def edge_verdict(metrics: dict, interval: Optional[list[float]], adjusted_p: Optional[float]) -> dict:
    blockers = []
    if metrics["trades"] < 100:
        blockers.append("fewer than 100 completed trades")
    if metrics["mean_net_return_pct"] is None or metrics["mean_net_return_pct"] <= 0:
        blockers.append("non-positive net expectancy")
    if interval is None or interval[0] <= 0:
        blockers.append("95% daily block-bootstrap interval does not clear zero")
    if adjusted_p is None or adjusted_p >= 0.05:
        blockers.append("Sidak-adjusted one-sided significance does not clear 5%")
    return {
        "classification": "CANDIDATE_EDGE" if not blockers else "NO_EDGE_DEMONSTRATED",
        "blockers": blockers,
    }


def run_backtest(
    bars: Sequence[Bar], config: StrategyConfig, news: Optional[dict] = None
) -> dict:
    signals = compute_signal_candidates(bars, config)
    signal_indices = sorted({signal.index for signal in signals})
    if len(signal_indices) < 12:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INSUFFICIENT_DATA",
            "reason": "fewer than 12 executable raw signals",
            "bars": len(bars),
            "signals": len(signals),
        }
    all_trades = simulate_trades(bars, signals, config)
    walk_splits = expanding_walk_forward_splits(
        signal_indices, bars, groups=6, purge_bars=config.max_hold_bars
    )
    cpcv_splits = cpcv_6x2_splits(
        signal_indices, bars, purge_bars=config.max_hold_bars,
        embargo_bars=config.max_hold_bars,
    )
    tracks = ("cold_start", "raw_price", "trained_pattern")
    walk = {
        track: [evaluate_split(bars, signals, config, split, track) for split in walk_splits]
        for track in tracks
    }
    cpcv = {
        track: [evaluate_split(bars, signals, config, split, track) for split in cpcv_splits]
        for track in tracks
    }
    metrics = summarize(all_trades, config.risk_fraction)
    bootstrap_interval = block_bootstrap_mean(all_trades)
    adjusted_p = sidak_adjusted_p(
        metrics["mean_return_z_score"], metrics["trades"], 3
    )
    cost_stress = {}
    for cost_bps in (0.0, 3.0, 6.0, 10.0):
        stressed = StrategyConfig(**{
            **asdict(config), "round_trip_cost_bps": cost_bps,
            "name": f"{config.name}_cost_{cost_bps:g}bps",
        })
        cost_stress[f"{cost_bps:g}_bps"] = summarize(
            simulate_trades(bars, signals, stressed), stressed.risk_fraction
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETE",
        "research_only": True,
        "production_state_mutated": False,
        "production_indicator_source": {
            "path": str(Path(production_patterns.__file__).resolve()),
            "sha256": hashlib.sha256(
                Path(production_patterns.__file__).read_bytes()
            ).hexdigest(),
        },
        "input": {
            "bars": len(bars),
            "symbols": sorted({bar.symbol for bar in bars}),
            "start": min(bar.timestamp for bar in bars).isoformat(),
            "end": max(bar.timestamp for bar in bars).isoformat(),
            "real_news_observations": len(news or {}),
            "news_role": "telemetry_availability_only",
        },
        "config": asdict(config),
        "raw_price": {
            "signals": len(signals),
            "metrics": metrics,
            "block_bootstrap_daily_mean_net_return_ci95_pct": bootstrap_interval,
            "sidak_adjusted_one_sided_p": adjusted_p,
            "verdict": edge_verdict(metrics, bootstrap_interval, adjusted_p),
            "attribution": trade_attribution(all_trades),
            "cost_stress": cost_stress,
        },
        "benchmarks": {
            "random_side_same_signals": random_side_baseline(bars, signals, config),
            "buy_and_hold": buy_and_hold_benchmark(bars),
        },
        "walk_forward_primary": walk,
        "cpcv_6_groups_choose_2_diagnostic": {
            "folds": 15,
            "causal_deployment_estimate": False,
            "tracks": cpcv,
        },
        "limitations": [
            "Current news is telemetry-only and was not treated as alpha.",
            "Historical news-fetch health was unavailable; edge testing assumes the availability gate was healthy.",
            "Bar OHLC cannot prove fill queue position or order-book absorption.",
            "CPCV is a robustness diagnostic; expanding walk-forward is the causal estimate.",
            "A backtest is not authorization for paper or live deployment.",
        ],
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--symbol", help="Default symbol when the CSV has no symbol column")
    parser.add_argument("--aggregate-minutes", type=int, default=0)
    parser.add_argument("--news", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cost-bps", type=float, default=3.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    bars = load_bars_csv(args.bars, args.symbol)
    if args.aggregate_minutes:
        bars = aggregate_bars(bars, args.aggregate_minutes)
    news = load_point_in_time_news(args.news)
    config = StrategyConfig(
        round_trip_cost_bps=args.cost_bps,
        slippage_bps_per_side=args.slippage_bps,
    )
    report = run_backtest(bars, config, news)
    report.setdefault("input", {})["bars_file_sha256"] = hashlib.sha256(
        args.bars.read_bytes()
    ).hexdigest()
    report["input"]["bars_file"] = str(args.bars.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "status": report["status"],
        "output": str(args.output),
        "bars": report.get("input", {}).get("bars", report.get("bars")),
        "signals": report.get("raw_price", {}).get("signals", report.get("signals")),
    }))
    return 0 if report["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
