"""Durable, broker-free forward outcomes for Main 5 signal candidates.

Shadow observations are deliberately segregated from ``pattern_memory``.
They may qualify a pattern for a one-share PAPER exploration order, but they
are never presented as broker-confirmed learning and never enable normal-size
execution.  Signals enter at the next completed bar's open and are resolved
from later closed bars, preventing same-bar lookahead.
"""

from __future__ import annotations

import hashlib
import math
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path
from statistics import mean
from typing import Optional


SCHEMA_VERSION = 1
DEFAULT_STOP_PCT = 0.025
DEFAULT_TARGET_PCT = 0.030
DEFAULT_MAX_HOLD_BARS = 13
DEFAULT_ROUND_TRIP_COST_BPS = 3.0
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 1.0


def _timestamp(value) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(
            value, datetime_time.min, tzinfo=timezone.utc).timestamp()
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return float(value)


@dataclass(frozen=True)
class ShadowCandidate:
    symbol: str
    signal_bar_ts: float
    side: str
    strategy: str
    conviction: float
    regime: str
    pattern_hash: str
    rsi: float
    adx: float
    ema_short: float
    ema_long: float
    operationally_eligible: bool = True

    @property
    def unique_key(self) -> str:
        raw = "%s|%.6f|%s|%s|%s" % (
            self.symbol.upper(), self.signal_bar_ts, self.side,
            self.strategy, self.pattern_hash,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


class ShadowForwardStore:
    def __init__(
        self,
        db_path: Path,
        stop_pct: float = DEFAULT_STOP_PCT,
        target_pct: float = DEFAULT_TARGET_PCT,
        max_hold_bars: int = DEFAULT_MAX_HOLD_BARS,
        round_trip_cost_bps: float = DEFAULT_ROUND_TRIP_COST_BPS,
        slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_pct = float(stop_pct)
        self.target_pct = float(target_pct)
        self.max_hold_bars = int(max_hold_bars)
        self.round_trip_cost = float(round_trip_cost_bps) / 10_000.0
        self.side_slippage = float(slippage_bps_per_side) / 10_000.0
        if self.stop_pct <= 0 or self.target_pct <= 0 or self.max_hold_bars < 1:
            raise ValueError("invalid shadow risk configuration")
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self.db_path), timeout=30.0, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version INTEGER NOT NULL,
                unique_key TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                symbol TEXT NOT NULL,
                signal_bar_ts REAL NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                strategy TEXT NOT NULL,
                conviction REAL NOT NULL,
                regime TEXT NOT NULL,
                pattern_hash TEXT NOT NULL,
                rsi REAL,
                adx REAL,
                ema_short REAL,
                ema_long REAL,
                operationally_eligible INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'awaiting_entry',
                entry_bar_ts REAL,
                entry_price REAL,
                stop_price REAL,
                target_price REAL,
                last_bar_ts REAL,
                bars_held INTEGER NOT NULL DEFAULT 0,
                best_price REAL,
                worst_price REAL,
                exit_bar_ts REAL,
                exit_price REAL,
                exit_reason TEXT,
                gross_return REAL,
                net_return REAL,
                outcome TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_symbol_status
                ON shadow_signals(symbol, status);
            CREATE INDEX IF NOT EXISTS idx_shadow_pattern_outcome
                ON shadow_signals(pattern_hash, outcome);
            """
        )
        conn.commit()

    def record_candidate(self, candidate: ShadowCandidate) -> bool:
        side = candidate.side.strip().lower()
        if side not in ("buy", "sell"):
            raise ValueError("shadow side must be buy or sell")
        values = (
            SCHEMA_VERSION, candidate.unique_key, time.time(),
            candidate.symbol.upper(), float(candidate.signal_bar_ts), side,
            candidate.strategy, float(candidate.conviction), candidate.regime,
            candidate.pattern_hash, float(candidate.rsi), float(candidate.adx),
            float(candidate.ema_short), float(candidate.ema_long),
            1 if candidate.operationally_eligible else 0,
        )
        with self._write_lock:
            conn = self._connect()
            cursor = conn.execute(
                """INSERT OR IGNORE INTO shadow_signals
                   (schema_version, unique_key, created_at, symbol,
                    signal_bar_ts, side, strategy, conviction, regime,
                    pattern_hash, rsi, adx, ema_short, ema_long,
                    operationally_eligible)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            conn.commit()
            return cursor.rowcount == 1

    @staticmethod
    def _normalise_bars(ohlc: dict) -> list[dict]:
        keys = ("bar_dates", "opens", "highs", "lows", "closes")
        lengths = [len(ohlc.get(key) or []) for key in keys]
        if not lengths or min(lengths) == 0 or len(set(lengths)) != 1:
            return []
        result = []
        for values in zip(*(ohlc[key] for key in keys)):
            raw_ts, open_, high, low, close = values
            try:
                bar = {
                    "ts": _timestamp(raw_ts),
                    "open": float(open_),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                }
            except (TypeError, ValueError, OverflowError):
                continue
            prices = (bar["open"], bar["high"], bar["low"], bar["close"])
            if not all(math.isfinite(value) and value > 0 for value in prices):
                continue
            if bar["high"] < max(bar["open"], bar["close"]) \
                    or bar["low"] > min(bar["open"], bar["close"]):
                continue
            result.append(bar)
        return sorted(result, key=lambda bar: bar["ts"])

    def observe_bars(self, symbol: str, ohlc: dict) -> int:
        """Advance pending shadows from newly completed bars only."""

        bars = self._normalise_bars(ohlc)
        if not bars:
            return 0
        changed = 0
        with self._write_lock:
            conn = self._connect()
            rows = conn.execute(
                """SELECT * FROM shadow_signals
                   WHERE symbol=? AND status IN ('awaiting_entry','open')
                   ORDER BY signal_bar_ts, id""",
                (symbol.upper(),),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                fresh = [
                    bar for bar in bars
                    if bar["ts"] > float(row.get("last_bar_ts") or row["signal_bar_ts"])
                ]
                if not fresh:
                    continue
                if row["status"] == "awaiting_entry":
                    entry_bar = fresh[0]
                    side_sign = 1 if row["side"] == "buy" else -1
                    entry = entry_bar["open"] * (1 + side_sign * self.side_slippage)
                    stop = entry * (1 - self.stop_pct if side_sign > 0 else 1 + self.stop_pct)
                    target = entry * (1 + self.target_pct if side_sign > 0 else 1 - self.target_pct)
                    conn.execute(
                        """UPDATE shadow_signals SET status='open',
                           entry_bar_ts=?, entry_price=?, stop_price=?,
                           target_price=?, last_bar_ts=?, best_price=?,
                           worst_price=? WHERE id=?""",
                        (entry_bar["ts"], entry, stop, target,
                         row["signal_bar_ts"], entry, entry, row["id"]),
                    )
                    row.update({
                        "status": "open", "entry_price": entry,
                        "stop_price": stop, "target_price": target,
                        "last_bar_ts": row["signal_bar_ts"],
                        "bars_held": 0, "best_price": entry,
                        "worst_price": entry,
                    })
                for bar in fresh:
                    if row["status"] != "open":
                        break
                    row = self._process_bar(conn, row, bar)
                    changed += 1
            conn.commit()
        return changed

    def _process_bar(self, conn: sqlite3.Connection, row: dict, bar: dict) -> dict:
        side_sign = 1 if row["side"] == "buy" else -1
        bars_held = int(row.get("bars_held") or 0) + 1
        best = max(float(row["best_price"]), bar["high"]) if side_sign > 0 \
            else min(float(row["best_price"]), bar["low"])
        worst = min(float(row["worst_price"]), bar["low"]) if side_sign > 0 \
            else max(float(row["worst_price"]), bar["high"])
        stop_hit = bar["low"] <= row["stop_price"] if side_sign > 0 \
            else bar["high"] >= row["stop_price"]
        target_hit = bar["high"] >= row["target_price"] if side_sign > 0 \
            else bar["low"] <= row["target_price"]
        exit_reason: Optional[str] = None
        exit_price: Optional[float] = None
        # With OHLC the intrabar path is unknown. Stop-first is conservative.
        if stop_hit:
            # A gap through the stop fills at the bar open, not at the more
            # favourable stop level. Preserve that adverse gap in evidence.
            if side_sign > 0 and bar["open"] < row["stop_price"]:
                exit_price = bar["open"]
            elif side_sign < 0 and bar["open"] > row["stop_price"]:
                exit_price = bar["open"]
            else:
                exit_price = float(row["stop_price"])
            exit_reason = "stop"
        elif target_hit:
            exit_reason, exit_price = "target", float(row["target_price"])
        elif bars_held >= self.max_hold_bars:
            exit_reason, exit_price = "time", bar["close"]

        if exit_reason is None:
            conn.execute(
                """UPDATE shadow_signals SET last_bar_ts=?, bars_held=?,
                   best_price=?, worst_price=? WHERE id=?""",
                (bar["ts"], bars_held, best, worst, row["id"]),
            )
            row.update({"last_bar_ts": bar["ts"], "bars_held": bars_held,
                        "best_price": best, "worst_price": worst})
            return row

        exit_price *= 1 - side_sign * self.side_slippage
        gross = side_sign * (exit_price - float(row["entry_price"])) / float(row["entry_price"])
        net = gross - self.round_trip_cost
        outcome = "win" if net > 0 else "loss" if net < 0 else "breakeven"
        conn.execute(
            """UPDATE shadow_signals SET status='closed', last_bar_ts=?,
               bars_held=?, best_price=?, worst_price=?, exit_bar_ts=?,
               exit_price=?, exit_reason=?, gross_return=?, net_return=?,
               outcome=? WHERE id=?""",
            (bar["ts"], bars_held, best, worst, bar["ts"], exit_price,
             exit_reason, gross, net, outcome, row["id"]),
        )
        row.update({"status": "closed", "last_bar_ts": bar["ts"],
                    "bars_held": bars_held, "best_price": best,
                    "worst_price": worst, "net_return": net,
                    "outcome": outcome})
        return row

    @staticmethod
    def _bootstrap_lower(values_by_day: dict, seed: int = 7301,
                         samples: int = 2000, block_days: int = 5) -> Optional[float]:
        if len(values_by_day) < 2:
            return None
        values = [mean(values_by_day[day]) for day in sorted(values_by_day)]
        block_days = min(max(1, block_days), len(values))
        rng = random.Random(seed)
        estimates = []
        for _ in range(samples):
            sampled = []
            while len(sampled) < len(values):
                start = rng.randrange(len(values))
                sampled.extend(
                    values[(start + offset) % len(values)]
                    for offset in range(block_days)
                )
            estimates.append(mean(sampled[:len(values)]))
        estimates.sort()
        return estimates[int(samples * 0.025)]

    def evidence(self, pattern_hash: Optional[str] = None,
                 minimum_trades: int = 100, minimum_days: int = 20,
                 side: Optional[str] = None,
                 strategy: Optional[str] = None,
                 regime: Optional[str] = None) -> dict:
        conn = self._connect()
        where = "status='closed' AND operationally_eligible=1"
        params = []
        if pattern_hash:
            where += " AND pattern_hash=?"
            params.append(pattern_hash)
        if side:
            where += " AND side=?"
            params.append(side)
        if strategy:
            where += " AND strategy=?"
            params.append(strategy)
        if regime:
            where += " AND regime=?"
            params.append(regime)
        rows = conn.execute(
            "SELECT entry_bar_ts, net_return, outcome FROM shadow_signals WHERE " + where,
            params,
        ).fetchall()
        returns = [float(row["net_return"]) for row in rows]
        days = {}
        for row in rows:
            day = datetime.fromtimestamp(float(row["entry_bar_ts"]), timezone.utc).date().isoformat()
            days.setdefault(day, []).append(float(row["net_return"]))
        wins = [value for value in returns if value > 0]
        losses = [value for value in returns if value < 0]
        expectancy = mean(returns) if returns else None
        profit_factor = sum(wins) / -sum(losses) if losses else None
        profit_factor_passes = bool(wins) and (
            not losses or (profit_factor is not None and profit_factor > 1.0))
        # Bootstrap is the expensive part. Before the preregistered sample and
        # day minima are met, it cannot promote anything and provides false
        # precision, so do not burn CPU recomputing it on every API poll.
        lower = None
        if (len(returns) >= minimum_trades and len(days) >= minimum_days
                and expectancy is not None and expectancy > 0
                and profit_factor_passes):
            lower = self._bootstrap_lower(days)
        blockers = []
        if len(returns) < minimum_trades:
            blockers.append("%d/%d completed shadows" % (len(returns), minimum_trades))
        if len(days) < minimum_days:
            blockers.append("%d/%d distinct entry days" % (len(days), minimum_days))
        if expectancy is None or expectancy <= 0:
            blockers.append("net expectancy is not positive")
        if not profit_factor_passes:
            blockers.append("profit factor does not exceed 1.0")
        if lower is None or lower <= 0:
            blockers.append("95% moving-block bootstrap lower bound does not clear zero")
        return {
            "pattern_hash": pattern_hash,
            "side": side,
            "strategy": strategy,
            "regime": regime,
            "completed": len(returns),
            "days": len(days),
            "wins": len(wins),
            "win_rate": len(wins) / len(returns) if returns else None,
            "mean_net_return_pct": expectancy * 100 if expectancy is not None else None,
            "profit_factor": profit_factor,
            "profit_factor_infinite": bool(wins and not losses),
            "bootstrap_lower_95_pct": lower * 100 if lower is not None else None,
            "paper_exploration_eligible": not blockers,
            "blockers": blockers,
        }

    def status(self) -> dict:
        conn = self._connect()
        counts = {
            row["status"]: int(row["n"])
            for row in conn.execute(
                "SELECT status, count(*) AS n FROM shadow_signals GROUP BY status"
            ).fetchall()
        }
        return {"database": str(self.db_path), "counts": counts,
                "global_evidence": self.evidence()}
