"""
Portfolio-level statistics computed from the completed trades log.

Read-only: queries the pattern_memory table for completed trades (win/loss)
and computes aggregate + per-symbol + per-regime metrics.  No trading logic
or state mutations.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_FALLBACK_DATA = "/home/team/shared/data"
logger = logging.getLogger("educator.stats")



class PortfolioStats:
    """Computes portfolio-level statistics from pattern_memory."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            fallback = os.environ.get("DATA_DIR", _FALLBACK_DATA)
            db_path = Path(
                os.environ.get(
                    "DB_PATH",
                    os.path.join(fallback, "patterns.db"),
                )
            )
        self.db_path = db_path
        #: Connections are per-thread; see _connect().
        self._local = threading.local()
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """A connection owned by the calling thread.

        `GET /api/stats` and `GET /api/portfolio` run on the API thread while
        the pipeline and the position monitor write to the same database.
        A shared connection with `check_same_thread=False` is not made safe by
        that flag -- it only silences the guard. The identical arrangement in
        patterns.py lost 30% of concurrent writes when measured.

        This module only reads, so the exposure is a failed or wrong-looking
        status query rather than lost data. Same fix regardless: sharing a
        connection across threads is not something to leave in place because
        today's caller happens to be read-only.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=30000")
            except sqlite3.DatabaseError:
                pass
            self._local.conn = conn
            self._conn = conn
        return conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute(self) -> Dict[str, Any]:
        """Return the full stats dictionary suitable for /api/stats."""
        conn = self._connect()
        trades = self._fetch_completed_trades(conn)

        if not trades:
            return self._empty_response()

        result = self._aggregate(trades)
        result["by_symbol"] = self._breakdown_by_symbol(conn)
        result["by_regime"] = self._breakdown_by_regime(conn)
        return result

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_completed_trades(self, conn: sqlite3.Connection) -> List[sqlite3.Row]:
        """Return all pattern_memory rows with a final outcome."""
        cur = conn.execute(
            """
            SELECT symbol, profit_pct, outcome, sentiment_zone, rsi_zone, ema_cross,
                   entry_price, exit_price, timestamp
            FROM pattern_memory
            WHERE data_source = 'live' AND outcome IN ('win', 'loss')
            ORDER BY timestamp ASC
            """
        )
        return cur.fetchall()

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate(self, trades: List[sqlite3.Row]) -> Dict[str, Any]:
        total = len(trades)
        wins = [t for t in trades if t["outcome"] == "win"]
        losses = [t for t in trades if t["outcome"] == "loss"]
        win_count = len(wins)
        loss_count = len(losses)

        total_profit_pct = sum(t["profit_pct"] or 0.0 for t in trades)
        gross_win = sum(t["profit_pct"] or 0.0 for t in wins)
        gross_loss = sum(abs(t["profit_pct"] or 0.0) for t in losses)

        win_rate = win_count / total if total > 0 else 0.0
        avg_win = gross_win / win_count if win_count > 0 else 0.0
        avg_loss = gross_loss / loss_count if loss_count > 0 else 0.0
        profit_factor = gross_win / gross_loss if gross_loss > 0 else (
            0.0 if gross_win == 0.0 else float("inf")
        )
        expectancy = total_profit_pct / total if total > 0 else 0.0

        max_dd = self._compute_max_drawdown(trades)
        max_consec_losses = self._max_consecutive_losses(trades)

        return {
            "total_trades": total,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round(win_rate, 4),
            "avg_win_pct": round(avg_win, 4),
            "avg_loss_pct": round(avg_loss, 4),
            "profit_factor": (
                round(profit_factor, 4) if profit_factor != float("inf") else None
            ),
            "expectancy_pct": round(expectancy, 4),
            # Sum of per-trade percentages. NOT a portfolio return: it
            # ignores position size and does not compound.
            "total_net_pnl_pct": round(total_profit_pct, 4),
            # Compounded return of the trade sequence -- what the equity curve
            # actually did, assuming each trade is taken at full size.
            "compounded_return_pct": round(self._compound_return_pct(trades), 4),
            "max_drawdown_pct": round(max_dd, 4),
            "max_consecutive_losses": max_consec_losses,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    def _empty_response(self) -> Dict[str, Any]:
        return {
            "total_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "compounded_return_pct": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "profit_factor": None,
            "expectancy_pct": 0.0,
            "total_net_pnl_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "max_consecutive_losses": 0,
            "by_symbol": {},
            "by_regime": {},
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Drawdown from sequential trade P&L
    # ------------------------------------------------------------------

    def _compute_max_drawdown(self, trades: List[sqlite3.Row]) -> float:
        """
        Compute max drawdown percentage from the cumulative P&L curve.

        Starts at 100 (baseline equity), COMPOUNDS each trade's profit_pct
        sequentially, then measures the largest peak-to-trough decline.

        Compounding matters: adding percentages to an equity figure mixes
        units and understates real drawdown. Five consecutive -10% trades are
        a 40.95% drawdown compounded, but read as 50% when added -- and a +10%
        followed by a -10% is a 10% drawdown, not 9.09%. For a risk metric,
        getting this wrong in either direction is bad; understating it is
        worse.
        """
        if not trades:
            return 0.0

        equity = 100.0
        peak = equity
        max_dd = 0.0

        for t in trades:
            pnl = t["profit_pct"] or 0.0
            equity *= (1.0 + pnl / 100.0)
            if equity <= 0:
                # Total ruin; further compounding is meaningless.
                return 100.0
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

        return max_dd

    @staticmethod
    def _compound_return_pct(trades: List[sqlite3.Row]) -> float:
        """Compounded return of the trade sequence, in percent."""
        equity = 1.0
        for t in trades:
            equity *= (1.0 + (t["profit_pct"] or 0.0) / 100.0)
            if equity <= 0:
                return -100.0
        return (equity - 1.0) * 100.0

    # ------------------------------------------------------------------
    # Consecutive losses
    # ------------------------------------------------------------------

    def _max_consecutive_losses(self, trades: List[sqlite3.Row]) -> int:
        longest = 0
        current = 0
        for t in trades:
            if t["outcome"] == "loss":
                current += 1
                if current > longest:
                    longest = current
            else:
                current = 0
        return longest

    # ------------------------------------------------------------------
    # Breakdowns
    # ------------------------------------------------------------------

    def _breakdown_by_symbol(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        cur = conn.execute(
            """
            SELECT
                symbol,
                COUNT(*)                                              AS total,
                SUM(CASE WHEN outcome = 'win'  THEN 1 ELSE 0 END)     AS wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END)     AS losses,
                COALESCE(SUM(profit_pct), 0.0)                        AS net_pnl_pct,
                COALESCE(SUM(CASE WHEN outcome = 'win'  THEN profit_pct ELSE 0 END), 0.0) AS gross_win,
                COALESCE(SUM(CASE WHEN outcome = 'loss' THEN profit_pct ELSE 0 END), 0.0) AS gross_loss
            FROM pattern_memory
            WHERE data_source = 'live' AND outcome IN ('win', 'loss')
            GROUP BY symbol
            ORDER BY symbol
            """
        )
        breakdown = {}
        for row in cur.fetchall():
            total = row["total"]
            wins = row["wins"]
            losses = row["losses"]
            gross_win = abs(row["gross_win"]) if row["gross_win"] else 0.0
            gross_loss = abs(row["gross_loss"]) if row["gross_loss"] else 0.0
            breakdown[row["symbol"]] = {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / total, 4) if total > 0 else 0.0,
                "net_pnl_pct": round(row["net_pnl_pct"], 4),
                "profit_factor": (
                    round(gross_win / gross_loss, 4)
                    if gross_loss > 0 and gross_win > 0
                    else (None if gross_win == 0.0 else None)
                ),
            }
        return breakdown

    def _breakdown_by_regime(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """
        Break down win-rate and P&L by the three regime dimensions:
        sentiment_zone, rsi_zone, and ema_cross.
        """
        cur = conn.execute(
            """
            SELECT
                COALESCE(sentiment_zone, 'unknown') AS zone,
                COUNT(*)                                           AS total,
                SUM(CASE WHEN outcome = 'win'  THEN 1 ELSE 0 END)  AS wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END)  AS losses,
                COALESCE(SUM(profit_pct), 0.0)                     AS net_pnl_pct
            FROM pattern_memory
            WHERE data_source = 'live' AND outcome IN ('win', 'loss')
            GROUP BY sentiment_zone
            ORDER BY total DESC
            """
        )
        by_sentiment = {}
        for row in cur.fetchall():
            total = row["total"]
            wins = row["wins"]
            by_sentiment[row["zone"]] = {
                "total_trades": total,
                "wins": wins,
                "losses": row["losses"],
                "win_rate": round(wins / total, 4) if total > 0 else 0.0,
                "net_pnl_pct": round(row["net_pnl_pct"], 4),
            }

        cur = conn.execute(
            """
            SELECT
                COALESCE(rsi_zone, 'unknown') AS zone,
                COUNT(*)                                        AS total,
                SUM(CASE WHEN outcome = 'win'  THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
                COALESCE(SUM(profit_pct), 0.0)                   AS net_pnl_pct
            FROM pattern_memory
            WHERE data_source = 'live' AND outcome IN ('win', 'loss')
            GROUP BY rsi_zone
            ORDER BY total DESC
            """
        )
        by_rsi = {}
        for row in cur.fetchall():
            total = row["total"]
            wins = row["wins"]
            by_rsi[row["zone"]] = {
                "total_trades": total,
                "wins": wins,
                "losses": row["losses"],
                "win_rate": round(wins / total, 4) if total > 0 else 0.0,
                "net_pnl_pct": round(row["net_pnl_pct"], 4),
            }

        cur = conn.execute(
            """
            SELECT
                COALESCE(ema_cross, 'unknown') AS zone,
                COUNT(*)                                          AS total,
                SUM(CASE WHEN outcome = 'win'  THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
                COALESCE(SUM(profit_pct), 0.0)                     AS net_pnl_pct
            FROM pattern_memory
            WHERE data_source = 'live' AND outcome IN ('win', 'loss')
            GROUP BY ema_cross
            ORDER BY total DESC
            """
        )
        by_ema = {}
        for row in cur.fetchall():
            total = row["total"]
            wins = row["wins"]
            by_ema[row["zone"]] = {
                "total_trades": total,
                "wins": wins,
                "losses": row["losses"],
                "win_rate": round(wins / total, 4) if total > 0 else 0.0,
                "net_pnl_pct": round(row["net_pnl_pct"], 4),
            }

        return {
            "sentiment_zone": by_sentiment,
            "rsi_zone": by_rsi,
            "ema_cross": by_ema,
        }