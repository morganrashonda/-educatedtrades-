"""Decision journal: why the bot did what it did, and what would have been better.

Statistics tell you *that* the bot lost money. They never tell you *why*. This
records the reasoning behind every decision at the moment it is made -- the
inputs, the gates, the arithmetic -- so a losing month can be diagnosed instead
of guessed at.

Four questions it is built to answer:

  * Why did it take that trade?      -> the signal inputs and every gate passed
  * Why did it refuse that trade?    -> the specific blocker, named
  * Why did that trade lose?         -> exit trigger, and the path price took
  * What could have been better?     -> excursion analysis (below)

The last one is the point. A stop-out at -2.5% looks identical in the P&L
whether the price then recovered to +5% or kept falling to -20%, but those are
opposite lessons. Tracking how far a trade ran in your favour (MFE) and against
you (MAE) before it closed separates "the stop was too tight" from "the stop
saved you", and "we exited early" from "we exited well".

Storage is append-only JSONL: one self-describing line per event, greppable,
and impossible to corrupt by a partial write.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

def default_path() -> str:
    """Where the journal lives, resolved when it is opened.

    This used to be a module constant computed at import. Anything that
    imported decision_log before DATA_DIR was settled froze the fallback --
    "." -- and then wrote the journal into whatever the working directory
    happened to be. The test suite did exactly that, appending to a
    `decisions.jsonl` in the repository root on every run, and a production
    process started from an unexpected directory would have done the same:
    the journal is the one record that explains why a trade was taken, and it
    would have been silently scattered across directories.

    Falls back to the credential-derived directory rather than ".", so a
    standalone tool or backtest that never imports main still writes the
    journal where the journal belongs.
    """
    try:
        from trading import resolve_data_dir
        base = resolve_data_dir()
    except Exception:
        base = os.environ.get("DATA_DIR", ".")
    return os.path.join(base, "decisions.jsonl")


#: Backwards-compatible module attribute. Prefer default_path(); this is a
#: snapshot and is only correct if DATA_DIR was already set at import.
DEFAULT_PATH = default_path()

ENTERED = "entered"
BLOCKED = "blocked"
EXITED = "exited"
SKIPPED = "skipped"
EXCURSION = "excursion"

# --- Execution-quality gate ----------------------------------------------
# Expressed as a share of the TARGET MOVE, not as raw percent: 0.08% is
# irrelevant against a 3% daily target and fatal against a 0.15% scalp. The
# cost case for a timeframe is always "cost is X% of what we are trying to
# capture", so the guard is stated in the same unit.
MAX_SLIPPAGE_PCT_OF_TARGET = float(
    os.environ.get("MAX_SLIPPAGE_PCT_OF_TARGET", "10"))
#: Fills considered. Rolling, because liquidity is a property of the current
#: regime -- a symbol that was fine in January and terrible in March should
#: trip in March, and one that recovers should be allowed back. An all-time
#: average only ever shrinks the universe.
SLIPPAGE_WINDOW = int(os.environ.get("SLIPPAGE_WINDOW", "20"))
#: Do not judge a symbol on one or two fills.
SLIPPAGE_MIN_FILLS = int(os.environ.get("SLIPPAGE_MIN_FILLS", "8"))
#: A single fill this many times over budget is not "wider spread than
#: expected" -- it is a halt, an illiquid moment, or size the book could not
#: absorb. That warrants an immediate stop, not a slow rolling average.
SLIPPAGE_OUTLIER_MULTIPLE = float(
    os.environ.get("SLIPPAGE_OUTLIER_MULTIPLE", "5"))


class DecisionLog:
    """Append-only journal of trading decisions and their rationale."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("DECISION_LOG_PATH") or default_path()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _append(self, payload: Dict[str, Any]) -> None:
        payload.setdefault("ts", time.time())
        payload.setdefault(
            "iso", datetime.fromtimestamp(payload["ts"], timezone.utc).isoformat())
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            line = json.dumps(payload, sort_keys=True, default=str)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
        except Exception:
            # A journal must never be able to break trading.
            pass

    # ------------------------------------------------------------------
    def entered(self, symbol: str, side: str, quantity: int, price: float,
                *, reason: str = "", inputs: Optional[dict] = None,
                gates: Optional[dict] = None, sizing: Optional[dict] = None,
                trade_id: str = "") -> None:
        """Record an entry with everything that justified it."""
        self._append({
            "event": ENTERED, "symbol": symbol, "side": side,
            "quantity": quantity, "price": price, "reason": reason,
            "inputs": inputs or {}, "gates": gates or {},
            "sizing": sizing or {}, "trade_id": trade_id,
        })

    def blocked(self, symbol: str, side: str, blocker: str,
                *, detail: str = "", inputs: Optional[dict] = None,
                would_have_been: Optional[dict] = None) -> None:
        """Record a refusal. The blocker is the whole point -- name it exactly.

        `would_have_been` lets a later review ask whether the refusals were
        costing money or saving it.
        """
        self._append({
            "event": BLOCKED, "symbol": symbol, "side": side,
            "blocker": blocker, "detail": detail, "inputs": inputs or {},
            "would_have_been": would_have_been or {},
        })

    def skipped(self, symbol: str, reason: str,
                *, inputs: Optional[dict] = None) -> None:
        """Record a signal that never became an order attempt."""
        self._append({"event": SKIPPED, "symbol": symbol, "reason": reason,
                      "inputs": inputs or {}})

    def exited(self, symbol: str, side: str, *, trigger: str,
               entry_price: float, exit_price: float, profit_pct: float,
               hold_hours: float = 0.0, quantity: int = 0,
               trade_id: str = "", detail: str = "") -> None:
        """Record an exit and why it fired."""
        self._append({
            "event": EXITED, "symbol": symbol, "side": side,
            "trigger": trigger, "entry_price": entry_price,
            "exit_price": exit_price, "profit_pct": profit_pct,
            "hold_hours": hold_hours, "quantity": quantity,
            "trade_id": trade_id, "detail": detail,
        })

    def excursion(self, symbol: str, *, trade_id: str = "",
                  entry_price: float = 0.0, best_price: float = 0.0,
                  worst_price: float = 0.0, exit_price: float = 0.0,
                  side: str = "buy") -> None:
        """Record how far the trade ran either way before it closed.

        MFE  -- maximum favourable excursion: the best it ever looked.
        MAE  -- maximum adverse excursion: the worst it ever looked.
        """
        mfe = self.pct_move(entry_price, best_price, side)
        mae = self.pct_move(entry_price, worst_price, side)
        realized = self.pct_move(entry_price, exit_price, side)
        self._append({
            "event": EXCURSION, "symbol": symbol, "trade_id": trade_id,
            "side": side, "entry_price": entry_price,
            "mfe_pct": round(mfe, 4), "mae_pct": round(mae, 4),
            "realized_pct": round(realized, 4),
            "left_on_table_pct": round(max(0.0, mfe - realized), 4),
        })

    def slippage(self, cost_assumption_pct: float = 0.03) -> Dict[str, Any]:
        """Realised slippage per symbol, in percent, against the assumption.

        This is the number the whole frequency argument rests on. Trading
        30-minute bars is only viable because cost is ~9% of the target move;
        at 25% it is not. Dollar slippage cannot answer that -- five cents is
        0.008% on SPY and 0.025% on GLD -- so it has to be measured as a
        fraction of price, per symbol, and compared with what was assumed.

        Entry slippage is signed by direction: paying ABOVE the reference on a
        buy is adverse, below is favourable. Averaging unsigned values would
        hide a systematic bias, which is exactly what you want to detect.
        """
        by_symbol: Dict[str, list] = defaultdict(list)
        for event in self.read():
            if event.get("event") != ENTERED:
                continue
            fill = event.get("price")
            reference = (event.get("inputs") or {}).get("reference_price")
            try:
                fill = float(fill)
                reference = float(reference)
            except (TypeError, ValueError):
                continue
            if fill <= 0 or reference <= 0:
                continue
            raw = (fill - reference) / reference * 100.0
            side = str(event.get("side", "buy")).lower()
            # Adverse is positive: paying up on a buy, selling down on a sell.
            by_symbol[event.get("symbol")].append(
                raw if side in ("buy", "long") else -raw)

        report: Dict[str, Any] = {"assumption_pct": cost_assumption_pct,
                                  "by_symbol": {}, "warnings": []}
        everything = []
        for symbol, values in by_symbol.items():
            everything.extend(values)
            average = sum(values) / len(values)
            ordered = sorted(values)
            worst = ordered[int(len(ordered) * 0.9)] if ordered else 0.0
            report["by_symbol"][symbol] = {
                "fills": len(values),
                "mean_pct": round(average, 5),
                "p90_pct": round(worst, 5),
                "worst_pct": round(ordered[-1], 5) if ordered else 0.0,
            }
            # One-way slippage; a round trip pays it roughly twice.
            if average * 2 > cost_assumption_pct and len(values) >= 5:
                report["warnings"].append(
                    "%s: round-trip slippage ~%.4f%% exceeds the %.4f%% "
                    "assumption — the cost case for this timeframe does not "
                    "hold for this symbol."
                    % (symbol, average * 2, cost_assumption_pct))
        if everything:
            report["overall_mean_pct"] = round(
                sum(everything) / len(everything), 5)
            report["overall_round_trip_pct"] = round(
                sum(everything) / len(everything) * 2, 5)
            report["fills"] = len(everything)
        return report

    #: Journal lines scanned by the execution gate. Only the last
    #: SLIPPAGE_WINDOW fills PER SYMBOL matter, and entries are interleaved
    #: with blocks/exits/excursions, so this is a generous bound that keeps
    #: the check O(1) in journal size.
    GATE_SCAN_LINES = 4000

    def _entry_slippage_series(self, scan: Optional[int] = None) -> Dict[str, list]:
        """Per-symbol one-way entry slippage, oldest first. Adverse positive."""
        series: Dict[str, list] = defaultdict(list)
        for event in self.read(limit=scan):
            if event.get("event") != ENTERED:
                continue
            fill = event.get("price")
            reference = (event.get("inputs") or {}).get("reference_price")
            try:
                fill = float(fill)
                reference = float(reference)
            except (TypeError, ValueError):
                continue
            if fill <= 0 or reference <= 0:
                continue
            raw = (fill - reference) / reference * 100.0
            side = str(event.get("side", "buy")).lower()
            series[event.get("symbol")].append(
                raw if side in ("buy", "long") else -raw)
        return series

    def execution_quality(self, target_move_pct: float) -> Dict[str, Any]:
        """Which symbols are currently too expensive to trade, and why.

        `target_move_pct` is the take-profit target for the configured
        timeframe -- the thing slippage is being judged against.

        Two independent triggers:
          * rolling  -- the recent average round trip exceeds the budget
          * outlier  -- one fill blew far past it, which a slow average would
                        take twenty trades to notice
        """
        budget_pct = target_move_pct * (MAX_SLIPPAGE_PCT_OF_TARGET / 100.0)
        report: Dict[str, Any] = {
            "target_move_pct": target_move_pct,
            "budget_round_trip_pct": round(budget_pct, 5),
            "window": SLIPPAGE_WINDOW,
            "by_symbol": {}, "blocked": [], "reasons": {},
        }
        for symbol, values in self._entry_slippage_series(
                scan=self.GATE_SCAN_LINES).items():
            window = values[-SLIPPAGE_WINDOW:]
            recent_round_trip = (sum(window) / len(window)) * 2 if window else 0.0
            worst_single = max(window) * 2 if window else 0.0
            entry = {
                "fills_considered": len(window),
                "fills_total": len(values),
                "rolling_round_trip_pct": round(recent_round_trip, 5),
                "worst_single_round_trip_pct": round(worst_single, 5),
                "budget_used_pct": round(
                    recent_round_trip / budget_pct * 100, 1) if budget_pct else 0.0,
            }
            reason = None
            if worst_single > budget_pct * SLIPPAGE_OUTLIER_MULTIPLE:
                reason = ("single fill cost %.4f%% round trip, over %gx the "
                          "%.4f%% budget — likely a halt, an illiquid moment, "
                          "or size the book could not absorb"
                          % (worst_single, SLIPPAGE_OUTLIER_MULTIPLE, budget_pct))
            elif (len(window) >= SLIPPAGE_MIN_FILLS
                  and recent_round_trip > budget_pct):
                reason = ("rolling round-trip slippage %.4f%% over the last %d "
                          "fills exceeds the %.4f%% budget (%.0f%% of the %.3f%% "
                          "target)"
                          % (recent_round_trip, len(window), budget_pct,
                             MAX_SLIPPAGE_PCT_OF_TARGET, target_move_pct))
            entry["blocked"] = reason is not None
            if reason:
                report["blocked"].append(symbol)
                report["reasons"][symbol] = reason
            report["by_symbol"][symbol] = entry
        return report

    def symbol_is_tradeable(self, symbol: str, target_move_pct: float) -> tuple:
        """(allowed, reason). Fails OPEN: no data is not evidence of a problem.

        Deliberately the opposite of the position gates. Those refuse when
        uncertain because the cost of a wrong trade is real; this one is a
        cost guard, and refusing to trade a symbol we have never filled would
        mean never trading it at all.
        """
        try:
            report = self.execution_quality(target_move_pct)
        except Exception:
            return True, "execution quality unavailable"
        if symbol in report["blocked"]:
            return False, report["reasons"][symbol]
        return True, "ok"

    @staticmethod
    def pct_move(entry: float, other: float, side: str) -> float:
        """Signed percent move in the direction of the position."""
        try:
            entry = float(entry)
            other = float(other)
        except (TypeError, ValueError):
            return 0.0
        if entry <= 0:
            return 0.0
        raw = (other - entry) / entry * 100.0
        return raw if str(side).lower() in ("buy", "long") else -raw

    # ------------------------------------------------------------------
    def _tail_lines(self, count: int) -> List[str]:
        """Last `count` lines, read by seeking from the END of the file.

        Reading the whole file to keep the last twenty entries is the same
        mistake as rewriting a whole JSON document to change one order: fine
        at a hundred rows, 271ms at twenty thousand. The gate that uses this
        runs on every entry attempt, so its cost must not track journal size.
        """
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size == 0:
            return []
        block = 65536
        data = b""
        with open(self.path, "rb") as handle:
            position = size
            while position > 0 and data.count(b"\n") <= count:
                step = min(block, position)
                position -= step
                handle.seek(position)
                data = handle.read(step) + data
        text = data.decode("utf-8", "replace")
        return text.splitlines()[-count:]

    def read(self, limit: Optional[int] = None) -> List[dict]:
        """Parse journal entries. `limit` reads only the tail, cheaply."""
        if not os.path.exists(self.path):
            return []
        events = []
        if limit:
            source = self._tail_lines(limit)
        else:
            with open(self.path, encoding="utf-8") as handle:
                source = handle.readlines()
        for line in source:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events[-limit:] if limit else events

    # ------------------------------------------------------------------
    def explain(self, trade_id: str) -> str:
        """Narrate one trade end to end, in the order things happened."""
        events = [e for e in self.read() if e.get("trade_id") == trade_id]
        if not events:
            return "No journal entries for trade %r." % trade_id
        lines = ["Trade %s" % trade_id, "-" * 60]
        for event in sorted(events, key=lambda e: e.get("ts", 0)):
            kind = event.get("event")
            when = event.get("iso", "")[:19]
            if kind == ENTERED:
                lines.append("%s  ENTER %s %s x%s @ %.2f"
                             % (when, event.get("side", "").upper(),
                                event.get("symbol"), event.get("quantity"),
                                event.get("price", 0)))
                if event.get("reason"):
                    lines.append("        because: %s" % event["reason"])
                for key, value in (event.get("inputs") or {}).items():
                    lines.append("        %-16s %s" % (key, value))
                for key, value in (event.get("sizing") or {}).items():
                    lines.append("        sizing.%-9s %s" % (key, value))
            elif kind == EXITED:
                lines.append("%s  EXIT  %s @ %.2f  (%s)  %+.2f%% after %.1fh"
                             % (when, event.get("symbol"),
                                event.get("exit_price", 0), event.get("trigger"),
                                event.get("profit_pct", 0),
                                event.get("hold_hours", 0)))
            elif kind == EXCURSION:
                lines.append("%s  PATH  best %+.2f%%, worst %+.2f%%, kept %+.2f%%"
                             % (when, event.get("mfe_pct", 0),
                                event.get("mae_pct", 0),
                                event.get("realized_pct", 0)))
                left = event.get("left_on_table_pct", 0)
                if left > 0.5:
                    lines.append("        left %.2f%% on the table" % left)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def postmortem(self, stop_loss_pct: float = 2.5,
                   take_profit_pct: float = 3.0) -> Dict[str, Any]:
        """Aggregate lessons. What is systematically going wrong?"""
        events = self.read()
        entries = [e for e in events if e.get("event") == ENTERED]
        exits = [e for e in events if e.get("event") == EXITED]
        blocks = [e for e in events if e.get("event") == BLOCKED]
        excursions = [e for e in events if e.get("event") == EXCURSION]

        findings: List[str] = []

        blockers = Counter(e.get("blocker", "?") for e in blocks)
        triggers = Counter(e.get("trigger", "?") for e in exits)

        # Stops that were too tight: stopped out, yet the trade had already
        # been meaningfully green at some point.
        stopped = [e for e in excursions
                   if e.get("realized_pct", 0) < 0 and e.get("mfe_pct", 0) > 0]
        premature = [e for e in stopped
                     if e.get("mfe_pct", 0) >= take_profit_pct * 0.5]
        if stopped and len(premature) / max(1, len(stopped)) > 0.4:
            findings.append(
                "%d of %d losing trades were up %.1f%%+ before reversing — the "
                "stop may be too tight, or exits too slow."
                % (len(premature), len(stopped), take_profit_pct * 0.5))

        # Winners exited well before their best price.
        winners = [e for e in excursions if e.get("realized_pct", 0) > 0]
        left = [e for e in winners if e.get("left_on_table_pct", 0) > 1.0]
        if winners and len(left) / max(1, len(winners)) > 0.5:
            avg_left = sum(e["left_on_table_pct"] for e in left) / len(left)
            findings.append(
                "%d of %d winners gave back %.2f%% on average from their best "
                "price — the target may be leaving money behind."
                % (len(left), len(winners), avg_left))

        # Trades that never went against us at all: the stop is not being used.
        never_adverse = [e for e in excursions
                         if e.get("mae_pct", 0) > -stop_loss_pct * 0.25]
        if excursions and len(never_adverse) / len(excursions) > 0.8:
            findings.append(
                "%d of %d trades barely moved against us — the stop is rarely "
                "engaged, so it is not doing much work at this width."
                % (len(never_adverse), len(excursions)))

        if blocks:
            top, count = blockers.most_common(1)[0]
            findings.append(
                "Most common refusal: %r (%d of %d blocked attempts)."
                % (top, count, len(blocks)))

        by_symbol = defaultdict(list)
        for event in exits:
            by_symbol[event.get("symbol")].append(event.get("profit_pct", 0.0))
        symbol_summary = {
            symbol: {
                "trades": len(values),
                "avg_pct": round(sum(values) / len(values), 4),
                "wins": sum(1 for v in values if v > 0),
            }
            for symbol, values in by_symbol.items()
        }

        slip = self.slippage()
        findings.extend(slip.get("warnings", []))
        if slip.get("fills"):
            findings.append(
                "Measured round-trip slippage %.4f%% across %d fills "
                "(assumed %.4f%%)."
                % (slip["overall_round_trip_pct"], slip["fills"],
                   slip["assumption_pct"]))

        return {
            "slippage": slip,
            "entries": len(entries),
            "exits": len(exits),
            "blocked": len(blocks),
            "blockers": dict(blockers),
            "exit_triggers": dict(triggers),
            "by_symbol": symbol_summary,
            "avg_mfe_pct": round(
                sum(e.get("mfe_pct", 0) for e in excursions) / len(excursions), 4)
            if excursions else 0.0,
            "avg_mae_pct": round(
                sum(e.get("mae_pct", 0) for e in excursions) / len(excursions), 4)
            if excursions else 0.0,
            "findings": findings,
        }

    # ------------------------------------------------------------------
    def render_postmortem(self, **kwargs) -> str:
        report = self.postmortem(**kwargs)
        lines = ["=" * 66, "DECISION POSTMORTEM", "=" * 66,
                 "Entries %d   Exits %d   Blocked %d"
                 % (report["entries"], report["exits"], report["blocked"])]
        if report["exit_triggers"]:
            lines.append("Exit triggers   : %s" % report["exit_triggers"])
        if report["blockers"]:
            lines.append("Refusal reasons : %s" % report["blockers"])
        if report["exits"]:
            lines.append("Avg best / worst: %+.2f%% / %+.2f%%"
                         % (report["avg_mfe_pct"], report["avg_mae_pct"]))
        if report["by_symbol"]:
            lines.append("-" * 66)
            for symbol, stats in sorted(report["by_symbol"].items()):
                lines.append("  %-6s %2d trades  %2dW  avg %+.2f%%"
                             % (symbol, stats["trades"], stats["wins"],
                                stats["avg_pct"]))
        slip = report.get("slippage", {})
        if slip.get("by_symbol"):
            lines.append("-" * 66)
            lines.append("Slippage (adverse = positive, one way):")
            for symbol, stats in sorted(slip["by_symbol"].items()):
                lines.append("  %-6s %3d fills  mean %+0.4f%%  p90 %+0.4f%%"
                             % (symbol, stats["fills"], stats["mean_pct"],
                                stats["p90_pct"]))
        lines.append("-" * 66)
        if report["findings"]:
            lines.append("What could be better:")
            for finding in report["findings"]:
                lines.append("  - %s" % finding)
        else:
            lines.append("No systematic issues detected yet "
                         "(or too few trades to tell).")
        lines.append("=" * 66)
        return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    print(DecisionLog().render_postmortem())
