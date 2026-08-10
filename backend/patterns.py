"""
Pattern-Recognition Engine for Educated Trades.

Maintains a "memory" of market patterns — combinations of news sentiment,
technical indicators (RSI, EMA cross), and subsequent price action —
and "learns" by correlating historical outcomes to refine trade entry criteria.

Pattern Schema:
  (sentiment_conviction_zone, rsi_zone, ema_cross_direction) → Outcome Stats

Database: SQLite at /home/team/shared/data/patterns.db

Usage:
    from patterns import PatternEngine

    engine = PatternEngine()
    record_id = engine.record_pattern(
        symbol="SPY", sentiment_score=0.7, conviction_score=0.65,
        rsi_value=62, ema_short=450.2, ema_long=448.1, entry_price=449.5
    )
    engine.record_outcome(record_id, exit_price=455.0, hours_later=4)
    signal = engine.evaluate(
        symbol="SPY", sentiment_score=0.55, conviction_score=0.5,
        rsi_value=58, ema_short=455.0, ema_long=453.0
    )
"""

import hashlib
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path

from market_clock import MarketClock, RTH_CLOSE
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths  (overridable via DATA_DIR env var)
# ---------------------------------------------------------------------------
_FALLBACK_DATA = "/var/lib/educated-trades/data"
DB_PATH = Path(os.environ.get("DB_PATH", os.path.join(os.environ.get("DATA_DIR", _FALLBACK_DATA), "patterns.db")))
DATA_DIR = DB_PATH.parent

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

SENTIMENT_BEARISH_THRESHOLD = -0.35
SENTIMENT_BULLISH_THRESHOLD = 0.35


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class PatternSignature:
    """Unique signature representing a market pattern."""

    sentiment_zone: str  # bearish | neutral | bullish
    rsi_zone: str  # oversold | normal | overbought
    ema_cross: str  # bearish_cross | no_cross | bullish_cross
    symbol: str = ""

    @property
    def hash_id(self) -> str:
        return sha256(
            f"{self.symbol}|{self.sentiment_zone}|{self.rsi_zone}|{self.ema_cross}".encode()
        ).hexdigest()[:16]

    @property
    def label(self) -> str:
        if self.symbol:
            return f"{self.symbol}_{self.sentiment_zone}_{self.rsi_zone}_{self.ema_cross}"
        return f"{self.sentiment_zone}_{self.rsi_zone}_{self.ema_cross}"


@dataclass
class PatternStats:
    """Learned statistics for a specific pattern signature."""

    pattern_id: str
    count: int = 0
    wins: int = 0
    losses: int = 0
    total_profit_pct: float = 0.0
    last_seen: float = 0.0

    @property
    def resolved(self) -> int:
        """Trades with a known outcome.

        `count` is incremented every time the pattern is *observed*, while
        wins/losses only move when a trade closes. Dividing by `count` would
        make a pattern look worse the more often it is seen, simply because
        open trades sit in the denominator.
        """
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        if self.resolved == 0:
            return 0.0
        return round(self.wins / self.resolved, 4)

    @property
    def avg_profit_pct(self) -> float:
        if self.resolved == 0:
            return 0.0
        return round(self.total_profit_pct / self.resolved, 4)

    @staticmethod
    def z_for_family(family_size: int = 1, alpha: float = 0.05) -> float:
        """Critical z-value corrected for testing `family_size` patterns.

        A 95% bound means a 5% chance of a false positive PER TEST. Search 500
        patterns that have no edge at all and ~14 of them clear that bar by
        luck -- the bot then trades coin flips with confidence. This is the
        multiple-comparisons problem, and it is the main way a learning
        strategy fools itself.

        Sidak correction: to keep the FAMILY-wide error at alpha across k
        independent tests, each test must clear 1 - (1-alpha)^(1/k).
        """
        from statistics import NormalDist
        k = max(1, int(family_size))
        per_test_alpha = 1.0 - (1.0 - alpha) ** (1.0 / k)
        # two-sided
        return NormalDist().inv_cdf(1.0 - per_test_alpha / 2.0)

    def corrected_signal_strength(self, family_size: int = 1) -> float:
        """signal_strength, but the bar rises with how many patterns you searched.

        Pass the number of patterns under consideration. Searching more
        candidates does not make evidence stronger -- it makes a given result
        less surprising, so the threshold has to move.
        """
        if self.resolved < 2:
            return 0.0
        z = self.z_for_family(family_size)
        low, high = self._wilson_bounds(self.wins, self.resolved, z)
        if low > 0.5:
            return round((low - 0.5) * 2.0, 4)
        if high < 0.5:
            return round((high - 0.5) * 2.0, 4)
        return 0.0

    @staticmethod
    def _wilson_bounds(wins: int, trials: int, z: float = 1.96) -> tuple:
        """95% Wilson score interval for a win rate.

        Stays well behaved at small n and near 0 or 1, which is precisely
        where a new pattern lives and where the normal approximation breaks.
        """
        if trials <= 0:
            return (0.0, 1.0)
        phat = wins / trials
        denom = 1.0 + z * z / trials
        centre = phat + z * z / (2 * trials)
        margin = z * math.sqrt(
            (phat * (1 - phat) + z * z / (4 * trials)) / trials)
        return (max(0.0, (centre - margin) / denom),
                min(1.0, (centre + margin) / denom))

    @property
    def confidence_interval(self) -> tuple:
        """95% interval on this pattern's true win rate."""
        return self._wilson_bounds(self.wins, self.resolved)

    @property
    def signal_strength(self) -> float:
        """Composite signal strength from -1 (strong sell) to +1 (strong buy).

        Scored from the CONSERVATIVE end of the confidence interval, so a
        pattern only carries weight once the evidence rules out chance:

          * lower bound above 50%  -> genuinely favourable, score positive
          * upper bound below 50%  -> genuinely unfavourable, score negative
          * interval spans 50%     -> we do not know yet, score 0

        A point estimate would call 7 wins from 10 a strong signal, when the
        true rate could be 40%. Acting on that is how a learner spends money
        chasing noise, and how a real edge gets buried under false ones.
        """
        if self.resolved < 2:
            return 0.0
        low, high = self.confidence_interval
        if low > 0.5:
            return round((low - 0.5) * 2.0, 4)
        if high < 0.5:
            return round((high - 0.5) * 2.0, 4)
        return 0.0

    @property
    def evidence_status(self) -> str:
        """Why this pattern does or does not currently carry weight."""
        if self.resolved < 2:
            return "insufficient: %d resolved trade(s)" % self.resolved
        low, high = self.confidence_interval
        if low > 0.5:
            return "favourable: win rate at least %.1f%%" % (low * 100)
        if high < 0.5:
            return "unfavourable: win rate at most %.1f%%" % (high * 100)
        return ("undecided: %.1f%%–%.1f%% spans breakeven after %d trades"
                % (low * 100, high * 100, self.resolved))

    @property
    def is_robust(self) -> bool:
        """Robust once it has >=10 trades with a *known outcome*.

        Sightings are not evidence; only closed trades are.
        """
        return self.resolved >= 10


@dataclass
class PatternRecord:
    """A single recorded pattern occurrence with its outcome."""

    id: int = 0
    timestamp: float = 0.0
    symbol: str = ""
    pattern_hash: str = ""
    sentiment_zone: str = ""
    rsi_zone: str = ""
    ema_cross: str = ""
    sentiment_score: float = 0.0
    rsi_value: float = 50.0
    conviction_score: float = 0.0
    entry_price: float = 0.0
    exit_price: Optional[float] = None
    exit_hours_later: Optional[float] = None
    profit_pct: Optional[float] = None
    outcome: Optional[str] = None  # win | loss | pending


@dataclass
class EvaluationSignal:
    """Output from pattern evaluation — the engine's recommendation."""

    symbol: str
    pattern_signature: PatternSignature
    pattern_stats: PatternStats
    action: str  # strong_buy | buy | neutral | sell | strong_sell
    conviction: float  # -1 to +1
    reason: str


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class PatternDatabase:
    """SQLite wrapper for pattern memory storage."""
    _market_clock = MarketClock()


    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        #: Connections are per-thread. One shared connection lost 30% of
        #: concurrent writes; see _connect().
        self._local = threading.local()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """A connection owned by the calling thread.

        `check_same_thread=False` silences SQLite's own guard against sharing
        a connection between threads; it does not make the sharing safe. The
        implicit-transaction state lives on the connection, so concurrent
        statements corrupt each other's bookkeeping.

        Three threads reach this: the pipeline, the 15-second position monitor
        and the API server. Measured with one shared connection -- 30
        concurrent writes against 30 concurrent reads produced 11 exceptions
        and stored 21 rows. NINE WRITES LOST, 30% of them. This database holds
        `active_positions`, which is the table the stop-loss monitor reads to
        know what it is protecting, and `pattern_memory`, which is what the
        bot learns from. A dropped write there is a position nothing watches
        or a trade that never teaches.

        The journal mode was `delete`, so readers and writers blocked each
        other as well. WAL lets them proceed concurrently, which is the access
        pattern this actually has.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
            except sqlite3.DatabaseError:
                # A database on a filesystem that cannot do WAL still works,
                # just with less concurrency. Not a reason to refuse to start.
                pass
            self._local.conn = conn
            self._conn = conn      # newest connection, for compatibility
        return conn


    def store_daily_bar(self, symbol: str, date_str: str, open_p: float, high: float, low: float, close: float, volume: int) -> None:
        try:
            bar_date = date.fromisoformat(date_str)
        except (TypeError, ValueError):
            logger.warning("Daily bar rejected: symbol=%s date=%s reason=malformed-date", symbol, date_str)
            return
        market_now = self._market_clock.now_ct()
        last_closed = market_now.date()
        if (not self._market_clock.is_trading_day(last_closed)
                or market_now.time() < RTH_CLOSE):
            last_closed -= timedelta(days=1)
        while not self._market_clock.is_trading_day(last_closed):
            last_closed -= timedelta(days=1)
        if bar_date > last_closed:
            logger.warning("Daily bar rejected: symbol=%s date=%s reason=session-not-closed", symbol, date_str)
            return
        if not self._market_clock.is_trading_day(bar_date):
            logger.warning("Daily bar rejected: symbol=%s date=%s reason=non-trading-day", symbol, date_str)
            return
        conn = self._connect()
        conn.execute("""INSERT INTO daily_bars (symbol, date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol, date) DO NOTHING""",
            (symbol, date_str, open_p, high, low, close, volume))
        conn.commit()
    def get_recent_daily_bars(self, symbol: str, limit: int = 40) -> list:
        conn = self._connect()
        rows = conn.execute(
            """SELECT symbol, date, open, high, low, close, volume 
               FROM daily_bars 
               WHERE symbol = ? 
               ORDER BY date DESC 
               LIMIT ?""",
            (symbol, limit)
        ).fetchall()
        # Return in chronological order for indicator calculation
        return [dict(r) for r in reversed(rows)]

    def close(self) -> None:
        """Close this thread's connection.

        Only the caller's own connection is closed: another thread's
        connection is not this one's to invalidate, and closing it out from
        under a running statement is how a shared connection became a problem
        in the first place.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        if self._conn is conn:
            self._conn = None

    def _init_db(self) -> None:
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pattern_memory (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       REAL NOT NULL,
                symbol          TEXT NOT NULL,
                pattern_hash    TEXT NOT NULL,
                sentiment_zone  TEXT NOT NULL,
                rsi_zone        TEXT NOT NULL,
                ema_cross       TEXT NOT NULL,
                sentiment_score REAL,
                rsi_value       REAL,
                conviction_score REAL,
                entry_price     REAL,
                exit_price      REAL,
                exit_hours_later REAL,
                profit_pct      REAL,
                outcome         TEXT DEFAULT 'pending',
                tier            TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pattern_hash
                ON pattern_memory(pattern_hash);

            CREATE INDEX IF NOT EXISTS idx_symbol
                ON pattern_memory(symbol);

            CREATE INDEX IF NOT EXISTS idx_outcome
                ON pattern_memory(outcome);

            CREATE TABLE IF NOT EXISTS pattern_stats (
                pattern_id      TEXT PRIMARY KEY,
                sentiment_zone  TEXT NOT NULL,
                rsi_zone        TEXT NOT NULL,
                ema_cross       TEXT NOT NULL,
                count           INTEGER DEFAULT 0,
                wins            INTEGER DEFAULT 0,
                losses          INTEGER DEFAULT 0,
                total_profit_pct REAL DEFAULT 0.0,
                last_seen       REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS pattern_learned_weights (
                weight_id       TEXT PRIMARY KEY,
                sentiment_mult  REAL DEFAULT 1.0,
                rsi_mult        REAL DEFAULT 1.0,
                ema_mult        REAL DEFAULT 1.0,
                updated_at      REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS milestone_tracker (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       REAL NOT NULL,
                type            TEXT NOT NULL,
                symbol          TEXT,
                value           REAL NOT NULL,
                cumulative_pnl  REAL DEFAULT 0.0,
                note            TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_milestone_type
                ON milestone_tracker(type);

            CREATE TABLE IF NOT EXISTS active_positions (
                record_id       INTEGER PRIMARY KEY,
                symbol          TEXT NOT NULL,
                entry_price     REAL NOT NULL,
                quantity        INTEGER NOT NULL,
                entry_time      REAL NOT NULL,
                side            TEXT NOT NULL,
                pattern_hash    TEXT,
                conviction      REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS daily_bars (
                symbol          TEXT NOT NULL,
                date            TEXT NOT NULL,
                open            REAL NOT NULL,
                high            REAL NOT NULL,
                low             REAL NOT NULL,
                close           REAL NOT NULL,
                volume          INTEGER NOT NULL,
                PRIMARY KEY (symbol, date)
            );
        """)
        conn.commit()
        # Create UNIQUE INDEX separately (can fail on existing duplicates)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_pattern "
                "ON pattern_memory(pattern_hash, symbol, entry_price)"
            )
        except Exception as idx_e:
            logger.critical(
                "UNIQUE INDEX creation FAILED -- "
                "run scripts/cleanup_pattern_memory.py first, "
                "then restart. Error: %s", idx_e,
            )
            raise
 
        # ---- Schema migrations for existing databases --------------------
        _logger_mig = logging.getLogger(__name__)

        # Migration 1: add data_source column (older schemas don't have it)
        try:
            conn.execute("ALTER TABLE pattern_memory ADD COLUMN data_source TEXT DEFAULT 'live'")
            conn.commit()
            _logger_mig.info("Schema migration: added data_source column to pattern_memory")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                # Column already exists — migration is a no-op
                pass
            else:
                _logger_mig.error(
                    "Schema migration 1 (data_source) FAILED: %s — "
                    "this is NOT a duplicate-column error",
                    e,
                )
                raise
        except Exception as e:
            _logger_mig.error(
                "Schema migration 1 (data_source) FAILED with unexpected error: %s",
                e,
            )
            raise

        # Migration 3: add side column. Without it an outcome cannot be
        # re-derived later -- a falling price is a win for a short and a loss
        # for a long, and pattern_memory had no way to tell them apart.
        try:
            conn.execute("ALTER TABLE pattern_memory ADD COLUMN side TEXT DEFAULT NULL")
            conn.commit()
            _logger_mig.info("Schema migration: added side column to pattern_memory")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                pass
            else:
                _logger_mig.error(
                    "Schema migration 3 (side) FAILED: %s — not a duplicate-column error", e)
                raise
        except Exception as e:
            _logger_mig.error("Schema migration 3 (side) FAILED unexpectedly: %s", e)
            raise

        # Migration 2: add tier column (older schemas don't have it)
        try:
            conn.execute("ALTER TABLE pattern_memory ADD COLUMN tier TEXT DEFAULT NULL")
            conn.commit()
            _logger_mig.info("Schema migration: added tier column to pattern_memory")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                # Column already exists — migration is a no-op
                pass
            else:
                _logger_mig.error(
                    "Schema migration 2 (tier) FAILED: %s — "
                    "this is NOT a duplicate-column error",
                    e,
                )
                raise
        except Exception as e:
            _logger_mig.error(
                "Schema migration 2 (tier) FAILED with unexpected error: %s",
                e,
            )
            raise

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def insert_pattern(
        self,
        pattern_hash: str,
        symbol: str,
        sentiment_zone: str,
        rsi_zone: str,
        ema_cross: str,
        sentiment_score: float,
        rsi_value: float,
        conviction_score: float,
        entry_price: float,
        data_source: str = "live",
        tier: Optional[str] = None,
    ) -> int:
        conn = self._connect()
        now = time.time()
        cur = conn.execute(
            """INSERT OR IGNORE INTO pattern_memory
               (timestamp, symbol, pattern_hash, sentiment_zone, rsi_zone,
                ema_cross, sentiment_score, rsi_value, conviction_score,
                entry_price, outcome, data_source, tier)
               VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
            (
                now, symbol, pattern_hash, sentiment_zone, rsi_zone,
                ema_cross, sentiment_score, rsi_value, conviction_score,
                entry_price, data_source, tier,
            ),
        )
        # rowcount: 1 = inserted, 0 = duplicate was ignored
        if cur.rowcount == 0:
            # Duplicate — fetch existing id
            row = conn.execute(
                "SELECT id FROM pattern_memory WHERE pattern_hash=? AND symbol=? AND entry_price=?",
                (pattern_hash, symbol, entry_price),
            ).fetchone()
            record_id = row["id"] if row else None
        else:
            record_id = cur.lastrowid
            # Upsert pattern_stats only for genuinely new rows
            conn.execute(
                """INSERT INTO pattern_stats
                   (pattern_id, sentiment_zone, rsi_zone, ema_cross,
                    count, last_seen)
                   VALUES (?,?,?,?, 1, ?)
                   ON CONFLICT(pattern_id) DO UPDATE SET
                    count = count + 1,
                    last_seen = excluded.last_seen""",
                (pattern_hash, sentiment_zone, rsi_zone, ema_cross, now),
            )
        conn.commit()
        return record_id

    def update_outcome(
        self, record_id: int, exit_price: float, hours_later: float,
        profit_pct: float, outcome: str, side: str = "",
    ) -> None:
        conn = self._connect()
        conn.execute(
            """UPDATE pattern_memory
               SET exit_price=?, exit_hours_later=?, profit_pct=?, outcome=?, side=?
               WHERE id=?""",
            (exit_price, hours_later, profit_pct, outcome, side, record_id),
        )

        row = conn.execute(
            "SELECT pattern_hash FROM pattern_memory WHERE id=?", (record_id,)
        ).fetchone()
        if row:
            pattern_hash = row["pattern_hash"]
            if outcome == "win":
                conn.execute(
                    """UPDATE pattern_stats
                       SET wins = wins + 1, total_profit_pct = total_profit_pct + ?
                       WHERE pattern_id = ?""",
                    (profit_pct, pattern_hash),
                )
            elif outcome == "loss":
                conn.execute(
                    """UPDATE pattern_stats
                       SET losses = losses + 1, total_profit_pct = total_profit_pct + ?
                       WHERE pattern_id = ?""",
                    (profit_pct, pattern_hash),
                )
        conn.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_pattern_stats(self, pattern_hash: str) -> Optional[PatternStats]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM pattern_stats WHERE pattern_id = ?", (pattern_hash,)
        ).fetchone()
        if row is None:
            return None
        return PatternStats(
            pattern_id=row["pattern_id"],
            count=row["count"],
            wins=row["wins"],
            losses=row["losses"],
            total_profit_pct=row["total_profit_pct"],
            last_seen=row["last_seen"],
        )

    def count_patterns(self) -> int:
        """How many distinct patterns exist -- the size of the search space.

        Needed for the multiple-testing correction: the more candidates the
        learner has considered, the less surprising any single good-looking
        result is, so the bar has to rise with this number.
        """
        try:
            row = self._connect().execute(
                "SELECT COUNT(*) AS c FROM pattern_stats").fetchone()
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    def get_all_pattern_stats(self) -> List[PatternStats]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM pattern_stats ORDER BY count DESC"
        ).fetchall()
        return [
            PatternStats(
                pattern_id=r["pattern_id"],
                count=r["count"],
                wins=r["wins"],
                losses=r["losses"],
                total_profit_pct=r["total_profit_pct"],
                last_seen=r["last_seen"],
            ) for r in rows
        ]

    def get_recent_patterns(
        self, limit: int = 50, symbol: Optional[str] = None
    ) -> List[PatternRecord]:
        conn = self._connect()
        if symbol:
            rows = conn.execute(
                "SELECT * FROM pattern_memory WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pattern_memory ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [PatternRecord(**dict(r)) for r in rows]

    def get_pending_patterns(self) -> List[PatternRecord]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM pattern_memory WHERE outcome = 'pending' ORDER BY timestamp"
        ).fetchall()
        return [PatternRecord(**dict(r)) for r in rows]

    def get_learned_weight(self, weight_id: str = "default") -> Dict[str, float]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM pattern_learned_weights WHERE weight_id = ?",
            (weight_id,)
        ).fetchone()
        if row is None:
            return {"sentiment_mult": 1.0, "rsi_mult": 1.0, "ema_mult": 1.0}
        return dict(row)

    def update_learned_weight(
        self, sentiment_mult: float, rsi_mult: float, ema_mult: float,
        weight_id: str = "default",
    ) -> None:
        conn = self._connect()
        conn.execute(
            """INSERT INTO pattern_learned_weights
               (weight_id, sentiment_mult, rsi_mult, ema_mult, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(weight_id) DO UPDATE SET
                sentiment_mult = excluded.sentiment_mult,
                rsi_mult = excluded.rsi_mult,
                ema_mult = excluded.ema_mult,
                updated_at = excluded.updated_at""",
            (weight_id, sentiment_mult, rsi_mult, ema_mult, time.time()),
        )
        conn.commit()



    # `close()` and `get_recent_daily_bars()` were each defined twice in this
    # class. The copies were byte-identical, so it looked harmless -- until
    # `close()` was corrected for per-thread connections at the FIRST
    # definition and the second silently kept overriding it. A duplicate is
    # not cosmetic; it is a trap for whoever edits the wrong one. Both are now
    # defined once, above.

    # ------------------------------------------------------------------
    # Milestone / P&L Tracking
    # ------------------------------------------------------------------
    def record_milestone(
        self,
        milestone_type: str,
        value: float,
        symbol: str = "",
        note: str = "",
    ) -> int:
        """Record a milestone event (trade, P&L check, milestone reached)."""
        conn = self._connect()
        now = time.time()

        # Get current cumulative P&L
        row = conn.execute(
            "SELECT cumulative_pnl FROM milestone_tracker "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cumulative = (row["cumulative_pnl"] if row else 0.0) + value

        cur = conn.execute(
            """INSERT INTO milestone_tracker
               (timestamp, type, symbol, value, cumulative_pnl, note)
               VALUES (?,?,?,?,?,?)""",
            (now, milestone_type, symbol, value, cumulative, note),
        )
        conn.commit()
        return cur.lastrowid

    def get_milestone_summary(self) -> dict:
        """Return profit/loss summary toward the $10K goal."""
        conn = self._connect()
        row = conn.execute(
            "SELECT cumulative_pnl FROM milestone_tracker "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        cumulative_pnl = row["cumulative_pnl"] if row else 0.0

        trade_count_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM milestone_tracker WHERE type='trade'"
        ).fetchone()
        trade_count = trade_count_row["cnt"] if trade_count_row else 0

        win_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM milestone_tracker "
            "WHERE type='trade' AND value > 0"
        ).fetchone()["cnt"]
        loss_count = trade_count - win_count

        history = conn.execute(
            "SELECT * FROM milestone_tracker ORDER BY timestamp DESC LIMIT 50"
        ).fetchall()

        return {
            "cumulative_pnl": round(cumulative_pnl, 2),
            "profit_target": 10000.0,
            "progress_pct": round(
                min(100.0, cumulative_pnl / 10000.0 * 100), 2
            ),
            "remaining": round(10000.0 - cumulative_pnl, 2),
            "trade_count": trade_count,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round(win_count / trade_count, 4) if trade_count > 0 else 0.0,
            "history": [
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "type": r["type"],
                    "symbol": r["symbol"],
                    "value": r["value"],
                    "cumulative_pnl": r["cumulative_pnl"],
                    "note": r["note"],
                }
                for r in history
            ],
        }

    # ------------------------------------------------------------------
    # Active Position Tracking
    # ------------------------------------------------------------------
    @staticmethod
    def adopted_record_id(symbol: str) -> int:
        """A stable, unique id for a position adopted from the broker.

        `record_id` is the PRIMARY KEY of active_positions and the insert is
        INSERT OR REPLACE, so startup recovery -- which passed `record_id=0`
        for every adopted position -- kept only the last one. Measured: three
        positions adopted from the broker, one tracked, two silently invisible
        to the stop/target monitor and to the unprotected-position check. The
        broker-side brackets still protected them; the bot did not know they
        existed.

        Negative, so it can never collide with a real pattern_memory id
        (positive autoincrement). Derived from the symbol rather than a
        counter, so adopting the same position twice updates one row instead
        of accumulating duplicates. sha1 rather than hash(), because hash() is
        salted per process and would change on every restart.
        """
        digest = hashlib.sha1(str(symbol).upper().encode("utf-8")).hexdigest()
        return -(int(digest[:12], 16) or 1)

    def add_active_position(
        self, record_id: int, symbol: str, entry_price: float,
        quantity: int, side: str, pattern_hash: str = "",
        conviction: float = 0.0,
    ) -> None:
        conn = self._connect()
        conn.execute(
            """INSERT OR REPLACE INTO active_positions
               (record_id, symbol, entry_price, quantity, entry_time,
                side, pattern_hash, conviction)
               VALUES (?,?,?,?,?,?,?,?)""",
            (record_id, symbol, entry_price, quantity, time.time(),
             side, pattern_hash, conviction),
        )
        conn.commit()

    def remove_active_position(self, record_id: int) -> None:
        conn = self._connect()
        conn.execute(
            "DELETE FROM active_positions WHERE record_id = ?", (record_id,)
        )
        conn.commit()

    def get_active_positions(self) -> List[dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM active_positions ORDER BY entry_time"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_position_by_symbol(self, symbol: str) -> Optional[dict]:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM active_positions WHERE symbol = ?", (symbol,)
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Technical Indicator Helpers
# ---------------------------------------------------------------------------
def compute_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Compute Relative Strength Index from a list of prices."""
    if len(prices) < period + 1:
        return None

    deltas = np.diff(prices[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def classify_rsi_zone(rsi: Optional[float]) -> str:
    """Classify RSI value into oversold / normal / overbought."""
    if rsi is None:
        return "normal"
    if rsi <= RSI_OVERSOLD:
        return "oversold"
    if rsi >= RSI_OVERBOUGHT:
        return "overbought"
    return "normal"


def detect_ema_cross(
    ema_short: float, ema_long: float,
    prev_ema_short: Optional[float] = None,
    prev_ema_long: Optional[float] = None,
) -> Tuple[str, str]:
    """
    Detect EMA cross direction.

    Returns (direction, label):
      - direction: bullish_cross | bearish_cross | no_cross
    """
    current_gap = ema_short - ema_long

    if prev_ema_short is not None and prev_ema_long is not None:
        prev_gap = prev_ema_short - prev_ema_long
        if prev_gap <= 0 and current_gap > 0:
            return ("bullish_cross", "Bullish (Golden) Cross")
        elif prev_gap >= 0 and current_gap < 0:
            return ("bearish_cross", "Bearish (Death) Cross")

    if current_gap > 0:
        return ("no_cross", "Short above Long (Bullish align)")
    elif current_gap < 0:
        return ("no_cross", "Short below Long (Bearish align)")
    return ("no_cross", "No Cross")


def classify_sentiment_zone(conviction_score: float) -> str:
    """Classify a sentiment conviction score into a zone."""
    if conviction_score <= -SENTIMENT_BULLISH_THRESHOLD:
        return "bearish"
    elif conviction_score >= SENTIMENT_BULLISH_THRESHOLD:
        return "bullish"
    return "neutral"


def compute_ema(prices: List[float], period: int) -> Optional[float]:
    """Compute Exponential Moving Average over the FULL price history.

    The previous implementation sliced to `prices[-period:]` and seeded the
    recursion with a single raw price from exactly `period` bars ago. That
    discards the rest of the series (callers pass 200 bars; an EMA-20 used 20)
    and makes the result hinge on one arbitrary bar.

    An EMA-20 has meaningful weight going back well beyond 20 bars, so the
    truncated version is a different statistic wearing the same name. Measured
    against a correct EMA on a 200-bar series, the 12/26 crossover disagreed on
    trend direction ~20% of the time -- and that crossover is one of the three
    features in every pattern signature.

    Standard construction: seed with the SMA of the first `period` values,
    then apply the smoothing recursion across everything that follows.
    """
    if len(prices) < period or period < 1:
        return None
    arr = np.asarray(prices, dtype=float)
    if not np.all(np.isfinite(arr)):
        return None
    multiplier = 2.0 / (period + 1)
    ema = float(arr[:period].mean())
    for price in arr[period:]:
        ema = (price - ema) * multiplier + ema
    return round(float(ema), 4)


def realized_volatility_pct(closes: List[float], period: int = 20) -> Optional[float]:
    """Standard deviation of recent returns, in percent.

    Used to make trend conviction dimensionless. A 0.4% EMA separation is a
    powerful signal on daily bars and unremarkable on 30-minute bars; without
    normalising, any fixed threshold is calibrated for exactly one timeframe.
    """
    if not closes or len(closes) < period + 1:
        return None
    window = np.asarray(closes[-(period + 1):], dtype=float)
    if not np.all(np.isfinite(window)) or np.any(window <= 0):
        return None
    returns = np.diff(window) / window[:-1]
    vol = float(np.std(returns)) * 100.0
    return vol if math.isfinite(vol) and vol > 0 else None


def trend_conviction(adx: Optional[float], ema_short: Optional[float],
                     ema_long: Optional[float],
                     volatility_pct: Optional[float] = None) -> float:
    """Directional conviction for a trend-following entry, from price alone.

    Sentiment used to supply this number. On SPY/QQQ/IWM that was never a
    defensible source -- broad index ETFs reprice public news in seconds, so a
    daily-bar reader is not front-running anything. Trend conviction here is
    built from two things price actually tells you:

      direction -- which side of the slow EMA the fast EMA sits, scaled by
                   their separation (a 0.5% gap means more than a 0.02% one)
      strength  -- ADX, which measures how trending the market is at all

    Returns [-1, +1]. Zero when the inputs are missing or the market is not
    trending, so "no signal" is the default rather than an accident.
    """
    if adx is None or ema_short is None or ema_long is None:
        return 0.0
    if not (math.isfinite(adx) and math.isfinite(ema_short)
            and math.isfinite(ema_long)) or ema_long <= 0:
        return 0.0

    # Separation, expressed in units of recent volatility rather than raw
    # percent. A fixed 0.5% saturation point was calibrated for daily bars:
    # on 30-minute bars the MEDIAN EMA20/EMA50 separation is already ~0.44%,
    # so conviction pinned at full strength most of the time and the gate
    # stopped discriminating at all. Normalising makes the same thresholds
    # mean the same thing at any timeframe.
    separation_pct = (ema_short - ema_long) / ema_long * 100.0
    if volatility_pct and volatility_pct > 0:
        # Saturate at 3 standard deviations of separation.
        direction = max(-1.0, min(1.0, separation_pct / (3.0 * volatility_pct)))
    else:
        direction = max(-1.0, min(1.0, separation_pct / 0.5))

    # No trend, no trend-following. Below the range ceiling this contributes
    # nothing; it ramps to full weight by the trend threshold.
    if adx <= ADX_RANGE_MAX:
        return 0.0
    strength = min(1.0, (adx - ADX_RANGE_MAX) / (ADX_TREND_MIN - ADX_RANGE_MAX))
    return round(direction * strength, 4)


def mean_reversion_conviction(rsi: Optional[float]) -> float:
    """Conviction for fading an RSI extreme. Positive = buy the oversold dip."""
    if rsi is None or not math.isfinite(rsi):
        return 0.0
    return round(max(-1.0, min(1.0, (rsi - 50.0) / -50.0)), 4)


# ADX regime thresholds (Wilder's Average Directional Index).
ADX_TREND_MIN = 25.0     # ADX > 25 → trending
ADX_RANGE_MAX = 20.0     # ADX < 20 → range-bound; 20-25 → transitioning


def compute_adx(
    highs: List[float], lows: List[float], closes: List[float],
    period: int = 14,
) -> Optional[float]:
    """
    Compute Wilder's 14-period Average Directional Index (ADX).

    ADX measures trend STRENGTH (not direction) on a 0-100 scale:
      - ADX < 20  → weak / range-bound market
      - ADX > 25  → strong trend
    Requires at least ``2 * period + 1`` bars for a stable reading.
    Returns None if there is insufficient data.
    """
    n = len(closes)
    if n < 2 * period + 1 or len(highs) != n or len(lows) != n:
        return None

    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)

    # True Range and directional movement
    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = closes[:-1]
    tr = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - prev_close),
        np.abs(lows[1:] - prev_close),
    ])

    # Wilder's smoothing (RMA)
    def _rma(x: np.ndarray, p: int) -> np.ndarray:
        out = np.zeros_like(x)
        if len(x) < p:
            return out
        out[p - 1] = x[:p].sum()
        for i in range(p, len(x)):
            out[i] = out[i - 1] - out[i - 1] / p + x[i]
        return out

    atr = _rma(tr, period)
    plus_di_s = _rma(plus_dm, period)
    minus_di_s = _rma(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * np.where(atr != 0, plus_di_s / atr, 0.0)
        minus_di = 100.0 * np.where(atr != 0, minus_di_s / atr, 0.0)
        di_sum = plus_di + minus_di
        dx = 100.0 * np.where(di_sum != 0, np.abs(plus_di - minus_di) / di_sum, 0.0)

    # ADX is Wilder's SMOOTHED average of DX -- seeded with the mean of the
    # first `period` valid DX values, then recursively smoothed. Taking a
    # simple mean of the last `period` DX values instead (as this previously
    # did) produces a noticeably more volatile series that crosses the 20/25
    # regime thresholds far more often. Measured against the correct formula
    # on a 240-bar series, the regime label disagreed ~42% of the time -- and
    # the regime selects the strategy and scales position size.
    valid_dx = dx[period:]
    if len(valid_dx) < period:
        return None
    adx = float(np.mean(valid_dx[:period]))
    for value in valid_dx[period:]:
        adx = (adx * (period - 1) + float(value)) / period
    if not math.isfinite(adx):
        return None
    return round(adx, 2)


def classify_regime(adx: Optional[float]) -> str:
    """
    Classify market regime from an ADX value:
      - "trending"      (ADX > 25)  → trend-following logic
      - "range_bound"   (ADX < 20)  → mean-reversion logic
      - "transitioning" (20-25)     → reduced position size
      - "unknown"       (no ADX)    → default (trend-following, full size)
    """
    if adx is None:
        return "unknown"
    if adx > ADX_TREND_MIN:
        return "trending"
    if adx < ADX_RANGE_MAX:
        return "range_bound"
    return "transitioning"


def get_strategy_for_regime(regime: str) -> str:
    """Map a market regime string to the strategy that should be active.

    Matches the inline logic from Orchestrator._detect_market_regime().

    Args:
        regime: One of "trending", "range_bound", "transitioning", or "unknown".

    Returns:
        "mean_reversion" for range_bound, "trend_following" for everything else.
    """
    if regime == "range_bound":
        return "mean_reversion"
    if regime == "unknown":
        return "none"
    return "trend_following"


def get_position_size_factor(regime: str) -> float:
    """Map a market regime string to a position size multiplier.

    Matches the inline logic from Orchestrator._detect_market_regime().

    Args:
        regime: One of "trending", "range_bound", "transitioning", or "unknown".

    Returns:
        0.5 for transitioning, 0.0 for unknown, 1.0 for everything else.
    """
    if regime == "transitioning":
        return 0.5
    if regime == "unknown":
        return 0.0
    return 1.0


# ---------------------------------------------------------------------------
# Pattern Engine
# ---------------------------------------------------------------------------
class PatternEngine:
    """
    Core engine that records market patterns and learns from their outcomes.

    The engine builds a 'memory' of what happens when specific combinations
    of sentiment and technical indicators occur, then uses that memory to
    score the probability of success for current conditions.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db = PatternDatabase(db_path)
        logger.debug("PatternEngine initialised (db: %s)", db_path)

    # ------------------------------------------------------------------
    # Pattern creation
    # ------------------------------------------------------------------
    def _build_signature(
        self, symbol: str, sentiment_conviction: float, rsi_value: Optional[float],
        ema_cross: str,
    ) -> PatternSignature:
        sentiment_zone = classify_sentiment_zone(sentiment_conviction)
        rsi_zone = classify_rsi_zone(rsi_value)
        return PatternSignature(
            symbol=symbol,
            sentiment_zone=sentiment_zone,
            rsi_zone=rsi_zone,
            ema_cross=ema_cross,
        )

    def record_pattern(
        self,
        symbol: str,
        sentiment_score: float,
        conviction_score: float,
        rsi_value: Optional[float],
        ema_short: float,
        ema_long: float,
        entry_price: float,
        prev_ema_short: Optional[float] = None,
        prev_ema_long: Optional[float] = None,
        data_source: str = "live",
        tier: Optional[str] = None,
    ) -> int:
        """
        Record a pattern occurrence and return record_id for outcome tracking.
        """
        ema_cross_direction, ema_cross_label = detect_ema_cross(
            ema_short, ema_long, prev_ema_short, prev_ema_long
        )
        signature = self._build_signature(
            symbol, conviction_score, rsi_value, ema_cross_direction
        )

        record_id = self.db.insert_pattern(
            pattern_hash=signature.hash_id,
            symbol=symbol,
            sentiment_zone=signature.sentiment_zone,
            rsi_zone=signature.rsi_zone,
            ema_cross=signature.ema_cross,
            sentiment_score=sentiment_score,
            rsi_value=50.0 if rsi_value is None else rsi_value,
            conviction_score=conviction_score,
            entry_price=entry_price,
            data_source=data_source,
            tier=tier,
        )

        logger.debug(
            "Pattern recorded [%s]: %s (id=%d, conviction=%.3f, "
            "rsi=%s, ema=%s)",
            symbol, signature.label, record_id, conviction_score,
            rsi_value, ema_cross_direction,
        )
        return record_id

    # ------------------------------------------------------------------
    # Outcome recording (the "learning" step)
    # ------------------------------------------------------------------
    def record_outcome(
        self, record_id: int, exit_price: float, hours_later: float,
        side: str = "buy",
    ) -> None:
        """
        Record the outcome of a previously recorded pattern and update
        the statistical memory.

        `side` is REQUIRED for correctness on shorts: a falling price is a win
        for a short and a loss for a long. Recording it direction-blind teaches
        the engine the opposite of what happened on every short trade.
        """
        import math as _math

        normalized_side = str(getattr(side, "value", side)).strip().lower()
        if normalized_side not in ("buy", "sell"):
            raise ValueError(
                "record_outcome needs a known side ('buy' or 'sell'), got %r" % (side,)
            )

        record = self.db._connect().execute(
            "SELECT entry_price FROM pattern_memory WHERE id=?",
            (record_id,),
        ).fetchone()

        if record is None:
            logger.warning("No pattern record found for id=%d", record_id)
            return

        entry_price = record["entry_price"]
        # Refuse to manufacture a percentage from an unusable price. Writing a
        # fabricated number here would poison every statistic downstream.
        try:
            entry_price = float(entry_price)
            exit_value = float(exit_price)
        except (TypeError, ValueError):
            logger.warning(
                "Outcome for id=%d skipped: non-numeric price (entry=%r exit=%r)",
                record_id, entry_price, exit_price,
            )
            return
        if not (_math.isfinite(entry_price) and _math.isfinite(exit_value)
                and entry_price > 0 and exit_value > 0):
            logger.warning(
                "Outcome for id=%d skipped: price must be positive and finite "
                "(entry=%s exit=%s) — leaving outcome unresolved rather than "
                "recording a fabricated result.",
                record_id, entry_price, exit_value,
            )
            return

        raw_pct = (exit_value - entry_price) / entry_price * 100.0
        # Short positions profit when price falls.
        profit_pct = round(raw_pct if normalized_side == "buy" else -raw_pct, 4)
        outcome = "win" if profit_pct > 0 else "loss"

        self.db.update_outcome(record_id, exit_value, hours_later, profit_pct,
                               outcome, normalized_side)

        logger.debug(
            "Outcome recorded [id=%d]: %.2f%% (%s after %.1f hours)",
            record_id, profit_pct, outcome, hours_later,
        )

        # Periodically retrain weights based on new data
        self._retrain_weights()

    # ------------------------------------------------------------------
    # Learning: weight optimisation
    # ------------------------------------------------------------------
    def _retrain_weights(self) -> None:
        """
        Analyse all pattern stats and update learned weights to reflect
        which indicators are most predictive.
        """
        all_stats = self.db.get_all_pattern_stats()
        if len(all_stats) < 3:
            return

        # Calculate how predictive each dimension is by comparing
        # win rates across zones — query the DB directly for zone info
        sentiment_wins = {"bearish": [], "neutral": [], "bullish": []}
        rsi_wins = {"oversold": [], "normal": [], "overbought": []}
        ema_wins = {"bearish_cross": [], "no_cross": [], "bullish_cross": []}

        conn = self.db._connect()
        for stat in all_stats:
            if stat.count < 5:
                continue
            row = conn.execute(
                "SELECT sentiment_zone, rsi_zone, ema_cross FROM pattern_stats WHERE pattern_id = ?",
                (stat.pattern_id,),
            ).fetchone()
            if row is None:
                continue
            sr = stat.signal_strength
            sz, rz, ec = row["sentiment_zone"], row["rsi_zone"], row["ema_cross"]
            if sz in sentiment_wins:
                sentiment_wins[sz].append(sr)
            if rz in rsi_wins:
                rsi_wins[rz].append(sr)
            if ec in ema_wins:
                ema_wins[ec].append(sr)

        # Compute multipliers: higher spread = more predictive weight
        def _avg_abs(vals: List[float]) -> float:
            return float(np.mean([abs(v) for v in vals])) if vals else 0.5

        sentiment_power = _avg_abs(
            [v for zone_vals in sentiment_wins.values() for v in zone_vals]
        )
        rsi_power = _avg_abs(
            [v for zone_vals in rsi_wins.values() for v in zone_vals]
        )
        ema_power = _avg_abs(
            [v for zone_vals in ema_wins.values() for v in zone_vals]
        )

        total = sentiment_power + rsi_power + ema_power
        if total > 0:
            s_mult = round(sentiment_power / total * 3.0, 4)
            r_mult = round(rsi_power / total * 3.0, 4)
            e_mult = round(ema_power / total * 3.0, 4)
            self.db.update_learned_weight(s_mult, r_mult, e_mult)
            logger.debug(
                "Weights retrained: sentiment=%.3f rsi=%.3f ema=%.3f",
                s_mult, r_mult, e_mult,
            )

    # ------------------------------------------------------------------
    # Evaluate current conditions
    # ------------------------------------------------------------------
    def evaluate(
        self,
        symbol: str,
        sentiment_score: float,
        conviction_score: float,
        rsi_value: Optional[float],
        ema_short: float,
        ema_long: float,
        prev_ema_short: Optional[float] = None,
        prev_ema_long: Optional[float] = None,
    ) -> EvaluationSignal:
        """
        Evaluate current market conditions against historical pattern
        memory and return a trade signal.

        Returns:
            EvaluationSignal with action, conviction, and reason.
        """
        ema_cross_direction, ema_cross_label = detect_ema_cross(
            ema_short, ema_long, prev_ema_short, prev_ema_long
        )
        signature = self._build_signature(
            symbol, conviction_score, rsi_value, ema_cross_direction
        )

        # Get historical stats for this pattern
        stats = self.db.get_pattern_stats(signature.hash_id)
        weights = self.db.get_learned_weight()

        if stats is None:
            stats = PatternStats(pattern_id=signature.hash_id)

        # No historical data — skip signal generation entirely
        if stats.count == 0:
            return EvaluationSignal(
                symbol=symbol,
                pattern_signature=signature,
                pattern_stats=stats,
                action="skip",
                conviction=0.0,
                reason="No historical samples for this pattern yet — build from real trades",
            )

        # Calculate composite conviction.
        # Corrected for the size of the search space: with many patterns under
        # consideration, some will look good by luck alone (with 500 no-edge
        # patterns, ~14 clear an uncorrected 95% bar). The threshold rises
        # with the number of candidates so the learner cannot mistake a lucky
        # bucket for an edge.
        family_size = self.db.count_patterns()
        signal_strength = stats.corrected_signal_strength(family_size)

        # Blend: 60% historical pattern stats, 40% current conviction
        blended = 0.6 * signal_strength + 0.4 * conviction_score

        # Apply learned weights
        weighted = (
            blended * weights.get("sentiment_mult", 1.0)
            * weights.get("rsi_mult", 1.0)
            * weights.get("ema_mult", 1.0)
        )
        final_conviction = round(max(-1.0, min(1.0, weighted)), 4)

        # Determine action
        if final_conviction >= 0.7:
            action = "strong_buy"
        elif final_conviction >= 0.3:
            action = "buy"
        elif final_conviction <= -0.7:
            action = "strong_sell"
        elif final_conviction <= -0.3:
            action = "sell"
        else:
            action = "neutral"

        # Build reason string
        parts = [
            f"Pattern: {signature.label}",
            f"Historical samples: {stats.count}",
        ]
        if stats.count >= 2:
            parts.append(f"Historical win rate: {stats.win_rate:.0%}")
            parts.append(f"Pattern signal: {signal_strength:+.3f}")
        else:
            parts.append("Insufficient historical data")
        parts.append(f"Sentiment: {sentiment_score:+.3f}")
        parts.append(f"RSI: {rsi_value}" if rsi_value else "RSI: N/A")
        parts.append(f"EMA: {ema_cross_label}")

        if stats.count >= 10:
            parts.append(f"Avg profit: {stats.avg_profit_pct:.2f}%")

        return EvaluationSignal(
            symbol=symbol,
            pattern_signature=signature,
            pattern_stats=stats,
            action=action,
            conviction=final_conviction,
            reason=" | ".join(parts),
        )

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------
    def summary(self) -> dict:
        """Return a summary of all learned patterns and weights."""
        all_stats = self.db.get_all_pattern_stats()
        weights = self.db.get_learned_weight()
        pending = self.db.get_pending_patterns()

        total_patterns = len(all_stats)
        total_occurrences = sum(s.count for s in all_stats)
        robust_patterns = sum(1 for s in all_stats if s.is_robust)

        return {
            "total_patterns": total_patterns,
            "total_occurrences": total_occurrences,
            "robust_patterns": robust_patterns,
            "pending_outcomes": len(pending),
            "learned_weights": weights,
            "top_patterns": sorted(
                [
                    {
                        "pattern_id": s.pattern_id,
                        "count": s.count,
                        "win_rate": s.win_rate,
                        "avg_profit_pct": s.avg_profit_pct,
                        "signal_strength": s.signal_strength,
                    }
                    for s in all_stats
                ],
                key=lambda x: x["count"],
                reverse=True,
            )[:20],
        }

    # ------------------------------------------------------------------
    # Learning Report & Milestone Tracking
    # ------------------------------------------------------------------
    def learning_report(self) -> dict:
        """Generate a learning summary report showing pattern evolution."""
        all_stats = self.db.get_all_pattern_stats()
        weights = self.db.get_learned_weight()
        pending = self.db.get_pending_patterns()

        return {
            "total_patterns": len(all_stats),
            "total_occurrences": sum(s.count for s in all_stats),
            "robust_patterns": sum(1 for s in all_stats if s.is_robust),
            "pending_outcomes": len(pending),
            "learned_weights": weights,
            "top_patterns": sorted(
                [
                    {
                        "pattern_id": s.pattern_id,
                        "count": s.count,
                        "wins": s.wins,
                        "losses": s.losses,
                        "win_rate": s.win_rate,
                        "avg_profit_pct": s.avg_profit_pct,
                        "signal_strength": s.signal_strength,
                        "is_robust": s.is_robust,
                    }
                    for s in all_stats
                ],
                key=lambda x: x["count"],
                reverse=True,
            )[:20],
            "weights_report": {
                "sentiment_weight": weights.get("sentiment_mult", 1.0),
                "rsi_weight": weights.get("rsi_mult", 1.0),
                "ema_weight": weights.get("ema_mult", 1.0),
                "most_predictive": max(
                    [
                        (k, abs(weights.get(k, 1.0) - 1.0))
                        for k in ("sentiment_mult", "rsi_mult", "ema_mult")
                    ],
                    key=lambda x: x[1],
                )[0].replace("_mult", "") if len(weights) > 2 else "insufficient_data",
            },
        }

    def milestone_summary(self) -> dict:
        """Return profit milestone summary toward $10K target."""
        return self.db.get_milestone_summary()

    def record_trade_pattern_and_track(
        self,
        symbol: str,
        sentiment_score: float,
        conviction_score: float,
        rsi_value: Optional[float],
        ema_short: float,
        ema_long: float,
        entry_price: float,
        quantity: int,
        side: str,
        prev_ema_short: Optional[float] = None,
        prev_ema_long: Optional[float] = None,
        tier: Optional[str] = None,
    ) -> Tuple[int, str]:
        """
        Record a trade entry as a pattern, track the position,
        and return (record_id, pattern_hash).
        """
        rid = self.record_pattern(
            symbol=symbol,
            sentiment_score=sentiment_score,
            conviction_score=conviction_score,
            rsi_value=rsi_value,
            ema_short=ema_short,
            ema_long=ema_long,
            entry_price=entry_price,
            prev_ema_short=prev_ema_short,
            prev_ema_long=prev_ema_long,
            tier=tier,
        )

        # Get pattern hash from the record
        conn = self.db._connect()
        row = conn.execute(
            "SELECT pattern_hash FROM pattern_memory WHERE id = ?", (rid,)
        ).fetchone()
        pattern_hash = row["pattern_hash"] if row else ""

        # Track as active position
        self.db.add_active_position(
            record_id=rid, symbol=symbol, entry_price=entry_price,
            quantity=quantity, side=side, pattern_hash=pattern_hash,
            conviction=conviction_score,
        )

        return rid, pattern_hash

    def close_tracked_position(
        self, record_id: int, current_price: float,
        hours_open: Optional[float] = None,
    ) -> dict:
        """
        Close a tracked position, record the outcome in pattern memory,
        log a P&L milestone, and remove from active positions.
        Returns the result dict with profit info.
        """
        conn = self.db._connect()
        pos = conn.execute(
            "SELECT * FROM active_positions WHERE record_id = ?", (record_id,)
        ).fetchone()
        if not pos:
            return {"error": f"No active position for record_id {record_id}"}

        # Claim the position with the DELETE itself, before any P&L is
        # recorded. `_check_active_positions` runs on BOTH the 15-second fast
        # monitor and the pipeline cycle, so two threads can reach here for
        # the same position: both used to SELECT it, both compute P&L, both
        # record an outcome and log a milestone. Measured with 8 concurrent
        # closes of one position: 8 "successful" closes and 8 P&L milestones
        # for a single trade -- inflating exactly the statistics the
        # forward-test gate reads to decide whether real money is justified.
        #
        # DELETE is atomic and rowcount reports whether THIS caller removed
        # the row, so precisely one proceeds. Doing it in the database rather
        # than with a lock also holds across processes, not just threads.
        claimed = conn.execute(
            "DELETE FROM active_positions WHERE record_id = ?", (record_id,))
        conn.commit()
        if claimed.rowcount == 0:
            return {"error": "position %s already being closed by another "
                             "worker" % record_id}

        symbol = pos["symbol"]
        entry_price = pos["entry_price"]
        quantity = pos["quantity"]
        side = pos["side"]

        if hours_open is None:
            hours_open = round((time.time() - pos["entry_time"]) / 3600, 2)

        # Calculate P&L
        if side == "buy":
            profit_pct = (current_price - entry_price) / entry_price * 100.0
        else:  # sell (short)
            profit_pct = (entry_price - current_price) / entry_price * 100.0

        dollar_pnl = round(profit_pct / 100.0 * entry_price * quantity, 2)
        outcome = "win" if profit_pct > 0 else "loss"

        # Record outcome in pattern_memory, using the SAME side used for the
        # P&L above so the learned result can never disagree with the reported
        # result.
        self.record_outcome(record_id, current_price, hours_open, side=side)

        # Log P&L milestone
        self.db.record_milestone(
            milestone_type="trade",
            value=dollar_pnl,
            symbol=symbol,
            note=f"{outcome.upper()}: {side.upper()} {symbol} "
                 f"qty={quantity} @ ${entry_price:.2f}→${current_price:.2f} "
                 f"({profit_pct:+.2f}%)",
        )

        # Already removed above: the DELETE is what claimed this close, so the
        # row is gone before any outcome was written. Deleting again here
        # would be harmless but misleading about where the claim happens.

        logger.info(
            "Position closed [%s]: %s qty=%d %.2f→%.2f (P&L=$%.2f, %.2f%%)",
            symbol, side.upper(), quantity, entry_price, current_price,
            dollar_pnl, profit_pct,
        )

        return {
            "success": True,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "exit_price": current_price,
            "profit_pct": round(profit_pct, 2),
            "dollar_pnl": dollar_pnl,
            "outcome": outcome,
            "hours_open": hours_open,
            "record_id": record_id,
        }

    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Versioned Model Retraining
    # ------------------------------------------------------------------
    FITS_DIR = Path(os.environ.get("DATA_DIR", "/home/team/shared/data")) / "patterns_fits"
    MAX_VERSIONS = 10

    def _ensure_fits_dir(self):
        self.FITS_DIR.mkdir(parents=True, exist_ok=True)

    def _get_next_version(self):
        self._ensure_fits_dir()
        existing = [int(f.stem.replace("v", "")) for f in self.FITS_DIR.glob("v*.json") if f.stem.startswith("v")]
        return max(existing) + 1 if existing else 1

    def list_fits(self):
        import json
        self._ensure_fits_dir()
        versions = []
        for f in sorted(self.FITS_DIR.glob("v*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                versions.append({
                    "version": f.stem,
                    "timestamp": data.get("timestamp", 0),
                    "datetime_utc": data.get("datetime_utc", "unknown"),
                    "patterns": data.get("patterns", 0),
                    "occurrences": data.get("occurrences", 0),
                    "robust_patterns": data.get("robust_patterns", 0),
                    "overall_win_rate": data.get("overall_win_rate", 0),
                    "promoted": data.get("promoted", False),
                    "file_size": f.stat().st_size,
                })
            except Exception:
                pass
        current_live = None
        live_link = self.FITS_DIR / "current_live.txt"
        if live_link.exists():
            try:
                current_live = live_link.read_text().strip()
            except Exception:
                pass
        return {"versions": versions, "current_live": current_live, "total_versions": len(versions), "fit_dir": str(self.FITS_DIR)}

    def export_fit(self, version=None, promote=False, note=""):
        import json
        from datetime import datetime
        self._ensure_fits_dir()
        version = version or self._get_next_version()
        version_str = f"v{version}"
        all_stats = self.db.get_all_pattern_stats()
        weights = self.db.get_learned_weight()
        pending = self.db.get_pending_patterns()
        patterns_data = []
        for s in all_stats:
            patterns_data.append({
                "pattern_id": s.pattern_id, "count": s.count, "wins": s.wins,
                "losses": s.losses, "win_rate": s.win_rate,
                "avg_profit_pct": s.avg_profit_pct, "signal_strength": s.signal_strength,
                "is_robust": s.is_robust,
            })
        total_occurrences = sum(s.count for s in all_stats)
        robust_count = sum(1 for s in all_stats if s.is_robust)
        overall_wr = round(
            sum(s.win_rate * s.count for s in all_stats if s.count > 0) / max(total_occurrences, 1), 4
        ) if total_occurrences > 0 else 0.0
        fit_data = {
            "version": version, "version_str": version_str,
            "timestamp": time.time(), "datetime_utc": datetime.utcnow().isoformat(),
            "patterns": len(patterns_data), "occurrences": total_occurrences,
            "robust_patterns": robust_count, "overall_win_rate": overall_wr,
            "learned_weights": weights, "pending_outcomes": len(pending),
            "pattern_stats": patterns_data, "promoted": promote, "note": note,
        }
        fit_path = self.FITS_DIR / f"{version_str}.json"
        with open(fit_path, "w") as f:
            json.dump(fit_data, f, indent=2, default=str)
        if promote:
            live_link = self.FITS_DIR / "current_live.txt"
            live_link.write_text(version_str)
            logger.info("PROMOTED fit %s as current live version", version_str)
        all_fits = sorted(self.FITS_DIR.glob("v*.json"), key=lambda f: f.stat().st_mtime)
        while len(all_fits) > self.MAX_VERSIONS:
            oldest = all_fits.pop(0)
            try:
                oldest.unlink()
                logger.info("Removed oldest fit %s (max %d versions)", oldest.name, self.MAX_VERSIONS)
            except Exception as e:
                logger.warning("Failed to remove old fit %s: %s", oldest.name, e)
        logger.info("Exported fit %s: %d patterns, %d occurrences, %.2f%% win rate%s",
                    version_str, len(patterns_data), total_occurrences,
                    overall_wr * 100, " (PROMOTED)" if promote else "")
        return {"version": version, "version_str": version_str, "path": str(fit_path),
                "patterns": len(patterns_data), "occurrences": total_occurrences,
                "overall_win_rate": overall_wr, "promoted": promote}

    def evaluate_fit_on_unseen_data(self, fit_version, lookback_days=20):
        import json
        fit_path = self.FITS_DIR / f"v{fit_version}.json"
        if not fit_path.exists():
            return {"error": f"Fit v{fit_version} not found", "can_promote": False}
        try:
            with open(fit_path) as f:
                candidate = json.load(f)
        except Exception as e:
            return {"error": f"Failed to load fit: {e}", "can_promote": False}
        current_live = None
        live_link = self.FITS_DIR / "current_live.txt"
        if live_link.exists():
            try:
                live_v_str = live_link.read_text().strip()
                live_path = self.FITS_DIR / f"{live_v_str}.json"
                if live_path.exists():
                    with open(live_path) as f:
                        current_live = json.load(f)
            except Exception:
                pass
        candidate_ts = candidate.get("timestamp", 0)
        conn = self.db._connect()
        recent_trades = conn.execute(
            """SELECT symbol, conviction_score, rsi_value, entry_price,
                      exit_price, profit_pct, outcome, timestamp
               FROM pattern_memory
               WHERE timestamp > ? AND exit_price IS NOT NULL
               ORDER BY timestamp DESC LIMIT 100""",
            (candidate_ts - 86400 * lookback_days,),
        ).fetchall()
        if len(recent_trades) < 5:
            return {"error": f"Insufficient unseen data (need >=5, found {len(recent_trades)})",
                    "can_promote": False, "recent_trades_found": len(recent_trades)}
        candidate_wins = sum(1 for t in recent_trades if t["outcome"] == "win")
        candidate_accuracy = candidate_wins / len(recent_trades)
        candidate_wr = candidate.get("overall_win_rate", 0)
        live_accuracy = current_live.get("overall_win_rate", 0) if current_live else 0
        can_promote = (candidate_wr >= live_accuracy * 0.95 and candidate_accuracy >= 0.4) if current_live else (len(recent_trades) >= 5 and candidate_wr > 0)
        result = {"candidate_version": f"v{fit_version}", "candidate_win_rate": candidate_wr,
                  "candidate_unseen_accuracy": round(candidate_accuracy, 4),
                  "live_version": live_link.read_text().strip() if live_link.exists() else None,
                  "live_win_rate": live_accuracy, "unseen_trades_evaluated": len(recent_trades),
                  "candidate_wins": candidate_wins, "can_promote": can_promote,
                  "promotion_reason": (
                      f"Candidate WR ({candidate_wr:.2%}) {'>= ' if can_promote else '< '}"
                      f"live ({live_accuracy:.2%}) with {candidate_accuracy:.1%} accuracy on {len(recent_trades)} trades")}
        logger.info("Fit evaluation: v%d wr=%.2f%% unseen=%.2f%% live=%.2f%% -> %s",
                    fit_version, candidate_wr*100, candidate_accuracy*100, live_accuracy*100,
                    "CAN PROMOTE" if can_promote else "DO NOT PROMOTE")
        return result

    def retrain(self, auto_promote=True, note=""):
        export = self.export_fit(promote=False, note=note)
        evaluation = self.evaluate_fit_on_unseen_data(export["version"])
        result = {"export": export, "evaluation": evaluation, "promoted": False, "note": note}
        if auto_promote and evaluation.get("can_promote"):
            promoted_export = self.export_fit(version=export["version"], promote=True,
                                              note=f"Auto-promoted: {evaluation.get('promotion_reason', '')}")
            result["promoted"] = True
            result["export"] = promoted_export
            logger.info("RETRAIN: Fit v%d promoted (wr=%.2f%%, unseen=%.2f%%)",
                        export["version"], evaluation.get("candidate_win_rate",0)*100,
                        evaluation.get("candidate_unseen_accuracy",0)*100)
        elif not auto_promote:
            logger.info("RETRAIN: Fit v%d exported but not promoted (auto_promote=False)", export["version"])
        else:
            logger.warning("RETRAIN: Fit v%d NOT promoted - %s",
                           export["version"], evaluation.get("promotion_reason", "gate failed"))
        return result

    def rollback(self, version):
        version_str = f"v{version}"
        fit_path = self.FITS_DIR / f"{version_str}.json"
        if not fit_path.exists():
            return {"error": f"Version {version_str} not found", "success": False}
        live_link = self.FITS_DIR / "current_live.txt"
        old_live = live_link.read_text().strip() if live_link.exists() else "none"
        live_link.write_text(version_str)
        logger.info("ROLLBACK: %s -> %s", old_live, version_str)
        return {"success": True, "previous_live": old_live, "current_live": version_str, "version": version}

    def get_current_fit(self):
        import json
        live_link = self.FITS_DIR / "current_live.txt"
        if not live_link.exists():
            return {"status": "no_live_fit", "message": "No fit has been promoted yet. Run retrain() to create one."}
        try:
            version_str = live_link.read_text().strip()
            fit_path = self.FITS_DIR / f"{version_str}.json"
            if fit_path.exists():
                with open(fit_path) as f:
                    data = json.load(f)
                return {"status": "ok", "version": version_str, "data": data}
            return {"status": "missing", "version": version_str, "message": f"Fit file {fit_path} not found."}
        except Exception as e:
            return {"error": str(e)}


    # Historical daily-bar backfill for pattern memory
    # ------------------------------------------------------------------
    def seed_from_daily_bars(self) -> dict:
        """
        Backfill pattern_memory from historical daily_bars so the evaluator
        has real sample counts instead of 0-2.

        For each bar (with ~60 bar warmup), computes RSI and EMA cross,
        records the pattern, and uses the next bar's close as the outcome.
        """
        symbols = ["SPY", "QQQ", "IWM"]
        total_recorded = 0
        total_outcomes = 0

        for symbol in symbols:
            bars = self.db.get_recent_daily_bars(symbol, limit=9999)
            if len(bars) < 61:
                logger.warning(
                    "seed_from_daily_bars: %s only has %d bars (need >=61)",
                    symbol, len(bars),
                )
                continue

            closes = [b["close"] for b in bars]

            for i in range(60, len(bars) - 1):
                # Compute indicators using lookback
                price_window = closes[:i + 1]
                rsi_val = compute_rsi(price_window, period=14)
                ema_short = compute_ema(price_window, period=20)
                ema_long = compute_ema(price_window, period=50)
                prev_short = compute_ema(price_window[:-1], period=20)
                prev_long = compute_ema(price_window[:-1], period=50)

                if rsi_val is None or ema_short is None or ema_long is None:
                    continue

                entry_price = bars[i]["close"]
                exit_price = bars[i + 1]["close"]
                profit_pct = round((exit_price - entry_price) / entry_price * 100, 4)

                # Record pattern with neutral sentiment (no historical news)
                record_id = self.record_pattern(
                    symbol=symbol,
                    sentiment_score=0.0,
                    conviction_score=0.0,
                    rsi_value=rsi_val,
                    ema_short=ema_short,
                    ema_long=ema_long,
                    entry_price=entry_price,
                    prev_ema_short=prev_short,
                    prev_ema_long=prev_long,
                    data_source="seed",
                )
                total_recorded += 1

                # Record outcome
                outcome = "win" if profit_pct > 0 else "loss"
                self.db.update_outcome(
                    record_id, exit_price=exit_price,
                    hours_later=24,
                    profit_pct=abs(profit_pct),
                    outcome=outcome,
                )
                total_outcomes += 1

        logger.info(
            "seed_from_daily_bars: %d patterns recorded, %d outcomes "
            "(%d/%d symbols with >=61 bars)",
            total_recorded, total_outcomes,
            sum(1 for s in symbols if len(
                self.db.get_recent_daily_bars(s, limit=9999)
            ) >= 61),
            len(symbols),
        )
        return {"recorded": total_recorded, "outcomes": total_outcomes}

    # Mock data for testing
    # ------------------------------------------------------------------
    def seed_mock_data(self) -> None:
        """Seed the database with synthetic pattern data for testing."""
        mock_patterns = [
            # (sentiment, conviction, rsi, ema_short, ema_long, entry, outcome_price, hours, symbol)
            ("bullish", 0.72, 65, 510.5, 505.2, 508.0, 515.0, 4, "SPY"),
            ("bullish", 0.55, 42, 452.0, 448.3, 450.0, 458.5, 6, "SPY"),
            ("bullish", 0.68, 35, 498.0, 495.1, 496.5, 502.0, 3, "SPY"),
            ("neutral", 0.12, 55, 488.0, 486.5, 487.0, 486.0, 5, "SPY"),
            ("bearish", -0.65, 72, 520.0, 518.5, 519.0, 511.0, 4, "SPY"),
            ("bearish", -0.70, 78, 505.0, 503.2, 504.0, 496.0, 3, "SPY"),
            ("bearish", -0.45, 45, 480.0, 482.0, 481.0, 478.0, 6, "SPY"),
            ("bullish", 0.80, 38, 462.0, 458.0, 460.0, 470.0, 8, "QQQ"),
            ("bullish", 0.60, 55, 485.0, 480.0, 482.0, 490.0, 4, "QQQ"),
            ("bearish", -0.55, 75, 530.0, 525.0, 528.0, 518.0, 5, "QQQ"),
            ("neutral", 0.05, 50, 500.0, 498.0, 499.0, 500.5, 4, "QQQ"),
            ("bullish", 0.50, 45, 410.0, 408.0, 409.0, 415.0, 6, "IWM"),
            ("bearish", -0.60, 68, 395.0, 398.0, 396.0, 388.0, 5, "IWM"),
        ]

        for zone, conv, rsi, short, long, entry, ex_price, hrs, sym in mock_patterns:
            ema_dir, _ = detect_ema_cross(short, long)
            sig = PatternSignature(
                symbol=sym, sentiment_zone=zone, rsi_zone=classify_rsi_zone(rsi),
                ema_cross=ema_dir,
            )
            rid = self.db.insert_pattern(
                pattern_hash=sig.hash_id,
                symbol=sym,
                sentiment_zone=zone,
                rsi_zone=classify_rsi_zone(rsi),
                ema_cross=ema_dir,
                sentiment_score=conv,
                rsi_value=float(rsi),
                conviction_score=conv,
                entry_price=entry,
            )
            pct = (ex_price - entry) / entry * 100.0
            outcome = "win" if pct > 0 else "loss"
            self.db.update_outcome(rid, ex_price, hrs, round(pct, 2), outcome)

        self._retrain_weights()
        logger.debug("Seeded %d mock patterns", len(mock_patterns))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    import sys

    is_json = "--json" in sys.argv
    log_level = logging.WARNING if is_json else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s %(message)s")

    engine = PatternEngine()

    if "--seed" in sys.argv:
        engine.seed_mock_data()
        if not is_json:
            print("Mock data seeded.")

    if "--summary" in sys.argv or "--seed" in sys.argv:
        s = engine.summary()
        if is_json:
            print(json.dumps(s))
            sys.exit(0)
        print("\n=== Pattern Engine Summary ===")
        print(f"  Total patterns:       {s['total_patterns']}")
        print(f"  Total occurrences:    {s['total_occurrences']}")
        print(f"  Robust patterns (10+): {s['robust_patterns']}")
        print(f"  Pending outcomes:     {s['pending_outcomes']}")
        print(f"\n  Learned Weights:")
        print(f"    Sentiment: {s['learned_weights'].get('sentiment_mult', 1.0):.4f}")
        print(f"    RSI:       {s['learned_weights'].get('rsi_mult', 1.0):.4f}")
        print(f"    EMA:       {s['learned_weights'].get('ema_mult', 1.0):.4f}")
        print(f"\n  Top Patterns (by count):")
        for p in s["top_patterns"][:10]:
            print(
                f"    {p['pattern_id']:20s} | count={p['count']:3d} | "
                f"win_rate={p['win_rate']:.0%} | signal={p['signal_strength']:+.3f}"
            )

    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        # Evaluate a symbol from CLI
        symbol = sys.argv[1]
        sent = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
        rsi = float(sys.argv[3]) if len(sys.argv) > 3 else 50.0
        ema_s = float(sys.argv[4]) if len(sys.argv) > 4 else 100.0
        ema_l = float(sys.argv[5]) if len(sys.argv) > 5 else 99.0

        signal = engine.evaluate(
            symbol=symbol,
            sentiment_score=sent,
            conviction_score=sent,
            rsi_value=rsi,
            ema_short=ema_s,
            ema_long=ema_l,
        )
        print(f"\n=== Evaluation: {symbol} ===")
        print(f"  Action:     {signal.action.upper()}")
        print(f"  Conviction: {signal.conviction:+.4f}")
        print(f"  Reason:     {signal.reason}")
