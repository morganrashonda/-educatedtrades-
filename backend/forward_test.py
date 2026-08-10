"""Forward-test harness: is this bot ready for real money, and do we know yet?

Backtests tell you how a strategy would have done on data you already have.
Forward testing tells you how it does on data it has never seen -- which is
the only evidence that survives contact with reality.

Two jobs, and the second matters more than the first:

  1. Measure post-fix paper performance honestly (compounded, direction-aware).
  2. Say whether the sample is large enough to mean anything at all.

The second is where most people go wrong. A bot with 12 trades and a 67% win
rate feels like a working strategy; its true win rate could be anywhere from
39% to 86%. This module refuses to call that ready, and shows the arithmetic
rather than asserting it.

Nothing here promises profitability. It reports what happened and how much
confidence that supports.
"""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Readiness thresholds. Deliberately conservative and explicitly configurable:
# the point of a gate is that it is agreed BEFORE the results are in.
# ---------------------------------------------------------------------------
MIN_TRADES = int(os.environ.get("FORWARD_MIN_TRADES", "30"))
MIN_TRADING_DAYS = int(os.environ.get("FORWARD_MIN_DAYS", "20"))
MAX_ACCEPTABLE_DRAWDOWN_PCT = float(os.environ.get("FORWARD_MAX_DD_PCT", "10.0"))
#: Require the LOWER bound of the win-rate interval to clear breakeven, not the
#: point estimate. A point estimate above 50% on a small sample is noise.
REQUIRED_WIN_RATE_FLOOR = float(os.environ.get("FORWARD_WIN_FLOOR", "0.50"))

#: Trades recorded before this timestamp were produced by the pre-fix code
#: (direction-blind P&L on shorts, fabricated fills). They are not evidence.
QUARANTINE_BEFORE = os.environ.get("FORWARD_QUARANTINE_BEFORE", "")


def _quarantine_cutoff() -> Optional[float]:
    if not QUARANTINE_BEFORE:
        return None
    try:
        text = QUARANTINE_BEFORE.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple:
    """95% Wilson score interval for a proportion.

    Preferred over the textbook normal approximation because it stays sane at
    small n and near 0 or 1 -- exactly the regime a new bot lives in.
    """
    if trials <= 0:
        return (0.0, 1.0)
    phat = successes / trials
    denom = 1.0 + z * z / trials
    centre = phat + z * z / (2 * trials)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * trials)) / trials)
    low = (centre - margin) / denom
    high = (centre + margin) / denom
    return (max(0.0, low), min(1.0, high))


def trades_needed_for_confidence(observed_rate: float, floor: float = 0.5,
                                 z: float = 1.96, cap: int = 5000) -> Optional[int]:
    """Roughly how many trades before the interval could clear `floor`.

    Answers "how much longer must I paper trade?" instead of leaving the
    operator to guess. Returns None if the observed rate never clears it.
    """
    if observed_rate <= floor:
        return None
    n = 10
    while n <= cap:
        low, _ = wilson_interval(int(round(observed_rate * n)), n, z)
        if low > floor:
            return n
        n += 10
    return None


def max_drawdown_pct(returns_pct: List[float]) -> float:
    """Peak-to-trough decline of the COMPOUNDED equity curve."""
    equity, peak, worst = 1.0, 1.0, 0.0
    for r in returns_pct:
        equity *= (1.0 + r / 100.0)
        if equity <= 0:
            return 100.0
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak * 100.0)
    return worst


def compounded_return_pct(returns_pct: List[float]) -> float:
    equity = 1.0
    for r in returns_pct:
        equity *= (1.0 + r / 100.0)
        if equity <= 0:
            return -100.0
    return (equity - 1.0) * 100.0


# ---------------------------------------------------------------------------

@dataclass
class ForwardTrade:
    symbol: str
    side: str
    profit_pct: float
    outcome: str
    timestamp: float
    tier: Optional[str] = None


@dataclass
class ForwardReport:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    trading_days: int = 0
    win_rate: float = 0.0
    win_rate_low: float = 0.0
    win_rate_high: float = 1.0
    expectancy_pct: float = 0.0
    compounded_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_consecutive_losses: int = 0
    quarantined: int = 0
    ready: bool = False
    blockers: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class ForwardTest:
    """Evaluates paper-trading results and gates the move to real money."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            data_dir = os.environ.get("DATA_DIR", ".")
            db_path = Path(os.environ.get("DB_PATH",
                                          os.path.join(data_dir, "patterns.db")))
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    def load_trades(self) -> tuple:
        """Return (usable_trades, quarantined_count)."""
        if not self.db_path.exists():
            return [], 0
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT symbol, profit_pct, outcome, timestamp, tier "
                "FROM pattern_memory "
                "WHERE data_source = 'live' AND outcome IN ('win','loss') "
                "ORDER BY timestamp ASC"
            ).fetchall()
        except sqlite3.Error:
            return [], 0
        finally:
            conn.close()

        cutoff = _quarantine_cutoff()
        usable, quarantined = [], 0
        for row in rows:
            ts = float(row["timestamp"] or 0.0)
            if cutoff is not None and ts < cutoff:
                quarantined += 1
                continue
            usable.append(ForwardTrade(
                symbol=row["symbol"], side="",
                profit_pct=float(row["profit_pct"] or 0.0),
                outcome=row["outcome"], timestamp=ts,
                tier=row["tier"] if "tier" in row.keys() else None,
            ))
        return usable, quarantined

    # ------------------------------------------------------------------
    def evaluate(self) -> ForwardReport:
        trades, quarantined = self.load_trades()
        report = ForwardReport(quarantined=quarantined)

        if quarantined:
            report.notes.append(
                "%d pre-fix trades excluded: they were recorded with "
                "direction-blind P&L and are not evidence." % quarantined
            )

        if not trades:
            report.blockers.append("no completed paper trades yet")
            return report

        returns = [t.profit_pct for t in trades]
        report.trades = len(trades)
        report.wins = sum(1 for t in trades if t.outcome == "win")
        report.losses = report.trades - report.wins
        report.win_rate = round(report.wins / report.trades, 4)
        low, high = wilson_interval(report.wins, report.trades)
        report.win_rate_low = round(low, 4)
        report.win_rate_high = round(high, 4)
        report.expectancy_pct = round(sum(returns) / len(returns), 4)
        report.compounded_return_pct = round(compounded_return_pct(returns), 4)
        report.max_drawdown_pct = round(max_drawdown_pct(returns), 4)

        streak = worst = 0
        for t in trades:
            streak = streak + 1 if t.outcome == "loss" else 0
            worst = max(worst, streak)
        report.max_consecutive_losses = worst

        days = {datetime.fromtimestamp(t.timestamp, timezone.utc).date()
                for t in trades if t.timestamp}
        report.trading_days = len(days)

        # --- the gate --------------------------------------------------
        if report.trades < MIN_TRADES:
            report.blockers.append(
                "only %d trades; need >= %d for a meaningful sample"
                % (report.trades, MIN_TRADES))
        if report.trading_days < MIN_TRADING_DAYS:
            report.blockers.append(
                "only %d trading days; need >= %d to span varied conditions"
                % (report.trading_days, MIN_TRADING_DAYS))
        if report.expectancy_pct <= 0:
            report.blockers.append(
                "expectancy is %.4f%% per trade; a negative edge does not "
                "improve with size" % report.expectancy_pct)
        if report.win_rate_low <= REQUIRED_WIN_RATE_FLOOR:
            needed = trades_needed_for_confidence(
                report.win_rate, REQUIRED_WIN_RATE_FLOOR)
            message = (
                "win rate %.1f%% is not yet distinguishable from chance "
                "(95%% interval %.1f%%–%.1f%%)"
                % (report.win_rate * 100, low * 100, high * 100))
            if needed:
                message += "; ~%d trades at this rate would settle it" % needed
            else:
                message += "; the observed rate is at or below breakeven"
            report.blockers.append(message)
        if report.max_drawdown_pct > MAX_ACCEPTABLE_DRAWDOWN_PCT:
            report.blockers.append(
                "max drawdown %.2f%% exceeds the %.2f%% limit"
                % (report.max_drawdown_pct, MAX_ACCEPTABLE_DRAWDOWN_PCT))

        report.ready = not report.blockers
        if report.ready:
            report.notes.append(
                "Gate criteria met. This is evidence of edge, not proof of it: "
                "paper fills are optimistic (no slippage, no partial fills, no "
                "queue position), so live results are usually worse."
            )
        return report

    # ------------------------------------------------------------------
    def render(self) -> str:
        r = self.evaluate()
        lines = [
            "=" * 66,
            "FORWARD TEST — readiness for live capital",
            "=" * 66,
            "Trades            : %d  (%dW / %dL) over %d trading days"
            % (r.trades, r.wins, r.losses, r.trading_days),
            "Win rate          : %.1f%%   95%% CI %.1f%% – %.1f%%"
            % (r.win_rate * 100, r.win_rate_low * 100, r.win_rate_high * 100),
            "Expectancy        : %+.4f%% per trade" % r.expectancy_pct,
            "Compounded return : %+.2f%%" % r.compounded_return_pct,
            "Max drawdown      : %.2f%%" % r.max_drawdown_pct,
            "Worst losing run  : %d" % r.max_consecutive_losses,
        ]
        if r.quarantined:
            lines.append("Quarantined       : %d pre-fix trades" % r.quarantined)
        lines.append("-" * 66)
        lines.append("VERDICT: %s" % ("READY (gate criteria met)" if r.ready
                                      else "NOT READY"))
        for blocker in r.blockers:
            lines.append("  blocked: %s" % blocker)
        for note in r.notes:
            lines.append("  note: %s" % note)
        lines.append("=" * 66)
        return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    print(ForwardTest().render())
