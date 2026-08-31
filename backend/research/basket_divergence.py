"""SPY/QQQ/IWM relative-value divergence — research only.

Hypothesis, never before tested anywhere in this repository: the README
itself flags that SPY, QQQ and IWM correlate at roughly 0.9 and calls trading
all three "closer to one observation than to three" -- stated purely as a
concentration RISK. This module asks whether that same near-redundancy can be
turned into a SIGNAL instead: when one of the three baskets' daily RSI
diverges sharply from its two peers, does it subsequently converge back
(classic relative-value / statistical-arbitrage mean reversion), independent
of which way the overall market moves that day?

This is mechanistically unrelated to every previous opening-bell hypothesis
in backend/research/ (all of which were NQ/QQQ microstructure-at-the-open
studies). It is a cross-sectional, market-neutral question about the actual
six-symbol production universe, using only free daily bars already paid for
via the existing Alpaca account -- no new data purchase.

No production, Tier 3, learning, or order-path imports. Writes a report only.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import patterns as P  # reuse the REAL, production RSI implementation

SYMBOLS = ["SPY", "QQQ", "IWM"]
RSI_PERIOD = 14
MIN_HISTORY = RSI_PERIOD + 5

# Chronological split -- frozen before any threshold search runs on anything
# but the discovery slice.
DISCOVERY_FRAC = 0.60
VALIDATION_FRAC = 0.20
# remainder is confirmation

# Discovery-only grid. Whatever this search picks is frozen and applied
# unchanged to validation and confirmation -- neither is ever used to pick
# or adjust a parameter.
DIVERGENCE_THRESHOLDS = [5.0, 7.5, 10.0, 12.5, 15.0, 20.0]
HORIZONS_DAYS = [1, 2, 3, 5, 10]

# Cost stress: SPY/QQQ/IWM are among the most liquid ETFs traded, so a wide
# assumed round-trip cost is deliberately conservative, not realistic.
COST_SCENARIOS_BPS = [0.0, 2.0, 5.0, 10.0]

BOOTSTRAP_RESAMPLES = 5000
BOOTSTRAP_SEED = 20260828


@dataclass
class Bar:
    d: date
    o: float
    h: float
    l: float
    c: float
    v: float


def load_alpaca(symbol: str, start: datetime, end: datetime) -> List[Bar]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key = os.environ.get("APCA_API_KEY_ID", "")
    secret = os.environ.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("No Alpaca credentials in environment")
    client = StockHistoricalDataClient(key, secret)
    req = StockBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame.Day,
                            start=start, end=end)
    resp = client.get_stock_bars(req)
    data = resp.data.get(symbol, [])
    return [Bar(b.timestamp.date(), float(b.open), float(b.high), float(b.low),
                float(b.close), float(b.volume)) for b in data]


def build_daily_table(bars_by_symbol: dict) -> list:
    """One row per date common to all three symbols: close, rsi, fwd returns."""
    common_dates = sorted(set.intersection(
        *[{b.d for b in bars} for bars in bars_by_symbol.values()]))
    closes = {s: {b.d: b.c for b in bars} for s, bars in bars_by_symbol.items()}

    rows = []
    price_history = {s: [] for s in SYMBOLS}
    for d in common_dates:
        rsis = {}
        ok = True
        for s in SYMBOLS:
            price_history[s].append(closes[s][d])
            r = P.compute_rsi(price_history[s], RSI_PERIOD)
            if r is None:
                ok = False
            rsis[s] = r
        if not ok:
            continue
        rows.append({"date": d, "close": {s: closes[s][d] for s in SYMBOLS},
                     "rsi": rsis})

    # Forward returns per symbol per horizon, computed AFTER the loop so day
    # t's row can look ahead into the table (not into compute_rsi, which only
    # ever saw price_history[:t+1]) -- this is the label, not the feature.
    n = len(rows)
    for i, row in enumerate(rows):
        row["fwd_return_pct"] = {}
        for s in SYMBOLS:
            row["fwd_return_pct"][s] = {}
            for h in HORIZONS_DAYS:
                if i + h < n:
                    c0 = row["close"][s]
                    c1 = rows[i + h]["close"][s]
                    row["fwd_return_pct"][s][h] = (c1 / c0 - 1.0) * 100.0
                else:
                    row["fwd_return_pct"][s][h] = None
    return rows


def relative_signals(rows: list, threshold: float, horizon: int,
                      momentum: bool = False) -> list:
    """One observation per (day, divergent symbol) that crosses `threshold`.

    relative_rsi = symbol_rsi - mean(peer RSIs, same day)
    trade_return = symbol's forward return over `horizon` days MINUS the
    average forward return of the other two symbols over the same horizon --
    i.e. long the laggard / short the peer basket, market-neutral by
    construction, not a directional market bet.

    momentum=True flips the sign convention: rewards the divergent symbol
    CONTINUING to diverge (laggard keeps lagging, leader keeps leading)
    instead of converging back. Used only by run_momentum() against a data
    slice the mean-reversion version of this test never scored -- see
    docs/BASKET_MOMENTUM_CONTINUATION_RESEARCH_SPEC.md for why that
    separation matters.
    """
    obs = []
    for row in rows:
        for s in SYMBOLS:
            peers = [p for p in SYMBOLS if p != s]
            peer_rsi = float(np.mean([row["rsi"][p] for p in peers]))
            rel_rsi = row["rsi"][s] - peer_rsi
            if abs(rel_rsi) < threshold:
                continue
            fwd_s = row["fwd_return_pct"][s].get(horizon)
            fwd_peers = [row["fwd_return_pct"][p].get(horizon) for p in peers]
            if fwd_s is None or any(v is None for v in fwd_peers):
                continue
            peer_fwd = float(np.mean(fwd_peers))
            # Laggard (rel_rsi < 0, relatively oversold) is predicted to
            # catch up, i.e. fwd_s > peer_fwd. Leader (rel_rsi > 0) is
            # predicted to give back relative strength, i.e. fwd_s < peer_fwd.
            # In both cases a positive spread_return means the hypothesis
            # called the direction correctly. momentum=True predicts the
            # opposite: laggard keeps lagging, leader keeps leading.
            reversion_return = (fwd_s - peer_fwd) if rel_rsi < 0 else (peer_fwd - fwd_s)
            spread_return = -reversion_return if momentum else reversion_return
            obs.append({
                "date": row["date"], "symbol": s, "rel_rsi": rel_rsi,
                "side": "laggard" if rel_rsi < 0 else "leader",
                "spread_return_pct": spread_return,
            })
    return obs


def _wilson_bounds(wins: int, trials: int, z: float = 1.96) -> tuple:
    if trials <= 0:
        return (0.0, 1.0)
    phat = wins / trials
    denom = 1.0 + z * z / trials
    centre = phat + z * z / (2 * trials)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * trials)) / trials)
    return (max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom))


def _day_block_bootstrap(obs: list, seed: int) -> tuple:
    """Block by calendar date (not by observation) so same-day, correlated
    laggard/leader events across symbols are resampled together."""
    by_day = {}
    for o in obs:
        by_day.setdefault(o["date"], []).append(o["spread_return_pct"])
    days = list(by_day.values())
    if len(days) < 5:
        return (None, None)
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        sample_days = [days[i] for i in rng.integers(0, len(days), len(days))]
        flat = [v for d in sample_days for v in d]
        if flat:
            means.append(float(np.mean(flat)))
    if not means:
        return (None, None)
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    return (lo, hi)


def summarize(obs: list, cost_bps: float = 0.0) -> dict:
    if not obs:
        return {"n": 0}
    rets = [o["spread_return_pct"] - cost_bps / 100.0 for o in obs]
    wins = sum(1 for r in rets if r > 0)
    wr_lo, wr_hi = _wilson_bounds(wins, len(rets))
    boot_lo, boot_hi = _day_block_bootstrap(
        [{"date": o["date"], "spread_return_pct": r} for o, r in zip(obs, rets)],
        BOOTSTRAP_SEED)
    gains = [r for r in rets if r > 0]
    losses = [-r for r in rets if r < 0]
    pf = (sum(gains) / sum(losses)) if losses else (float("inf") if gains else 0.0)
    rets_sorted = sorted(rets)
    without_best = rets_sorted[:-1] if len(rets_sorted) > 1 else []
    return {
        "n": len(rets),
        "distinct_days": len({o["date"] for o in obs}),
        "win_rate": round(wins / len(rets), 4),
        "win_rate_wilson95": [round(wr_lo, 4), round(wr_hi, 4)],
        "mean_pct": round(float(np.mean(rets)), 4),
        "median_pct": round(float(np.median(rets)), 4),
        "total_pct": round(float(np.sum(rets)), 4),
        "bootstrap_mean_ci95": (
            [round(boot_lo, 4), round(boot_hi, 4)]
            if boot_lo is not None else None
        ),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "mean_without_best_pct": (
            round(float(np.mean(without_best)), 4) if without_best else None
        ),
    }


def random_direction_falsification(obs: list, seed: int = 99001) -> dict:
    """Same days/magnitudes, random long/short assignment of the spread."""
    rng = np.random.default_rng(seed)
    abs_moves = [abs(o["spread_return_pct"]) for o in obs]
    n_pos = sum(1 for o in obs if o["spread_return_pct"] > 0)
    trials = 20000
    null_means = []
    beat_count = 0
    real_mean = float(np.mean([o["spread_return_pct"] for o in obs])) if obs else 0.0
    for _ in range(trials):
        signs = rng.choice([-1.0, 1.0], size=len(abs_moves))
        sample = [m * s for m, s in zip(abs_moves, signs)]
        sm = float(np.mean(sample)) if sample else 0.0
        null_means.append(sm)
        if sm >= real_mean:
            beat_count += 1
    p = beat_count / trials
    return {
        "real_mean_pct": round(real_mean, 4),
        "null_mean_pct": round(float(np.mean(null_means)), 4) if null_means else None,
        "null_ci95_pct": (
            [round(sorted(null_means)[int(0.025 * trials)], 4),
             round(sorted(null_means)[int(0.975 * trials)], 4)]
            if null_means else None
        ),
        "one_sided_p_value": round(p, 5),
        "trials": trials,
    }


def _evaluate_hypothesis(rows: list, hypothesis_text: str, momentum: bool = False) -> dict:
    """Shared discovery/validation/confirmation pipeline for a given row set.

    Used identically by run() (reversion, full history) and run_momentum()
    (continuation, restricted to a data slice reversion never scored) so the
    two hypotheses are held to exactly the same gates.
    """
    n = len(rows)
    d_end = int(n * DISCOVERY_FRAC)
    v_end = d_end + int(n * VALIDATION_FRAC)
    discovery_rows = rows[:d_end]
    validation_rows = rows[d_end:v_end]
    confirmation_rows = rows[v_end:]

    # ---- Discovery: grid search, frozen afterwards ----
    best = None
    grid_results = []
    for thresh in DIVERGENCE_THRESHOLDS:
        for hz in HORIZONS_DAYS:
            obs = relative_signals(discovery_rows, thresh, hz, momentum=momentum)
            if len(obs) < 30:
                continue
            summ = summarize(obs, cost_bps=2.0)
            grid_results.append({"threshold": thresh, "horizon": hz, **summ})
            score = summ["mean_pct"] if summ.get("bootstrap_mean_ci95") and \
                summ["bootstrap_mean_ci95"][0] is not None and \
                summ["bootstrap_mean_ci95"][0] > 0 else None
            if score is not None and (best is None or score > best[0]):
                best = (score, thresh, hz)

    if best is None:
        return {
            "status": "NO_DISCOVERY_CANDIDATE",
            "reason": "no (threshold, horizon) combination cleared a positive "
                      "bootstrap lower bound on the discovery slice with "
                      "n>=30 after a 2bps cost stress",
            "grid_results": grid_results,
            "discovery_days": len(discovery_rows),
        }

    _, frozen_threshold, frozen_horizon = best

    def eval_block(block_rows, label):
        obs = relative_signals(block_rows, frozen_threshold, frozen_horizon, momentum=momentum)
        return {
            "label": label,
            "n_days": len(block_rows),
            "date_range": [str(block_rows[0]["date"]), str(block_rows[-1]["date"])]
            if block_rows else None,
            "gross": summarize(obs, cost_bps=0.0),
            "cost_scenarios": {
                f"{c}bps": summarize(obs, cost_bps=c) for c in COST_SCENARIOS_BPS
            },
            "random_direction_falsification": random_direction_falsification(obs),
            "n_obs": len(obs),
            "_obs": obs,
        }

    validation = eval_block(validation_rows, "validation")
    confirmation = eval_block(confirmation_rows, "confirmation")
    combined_obs = validation["_obs"] + confirmation["_obs"]
    combined = {
        "label": "validation+confirmation (out-of-sample)",
        "n_obs": len(combined_obs),
        "gross": summarize(combined_obs, cost_bps=0.0),
        "cost_scenarios": {
            f"{c}bps": summarize(combined_obs, cost_bps=c) for c in COST_SCENARIOS_BPS
        },
        "random_direction_falsification": random_direction_falsification(combined_obs),
    }
    del validation["_obs"]
    del confirmation["_obs"]

    # ---- Decision, mirroring the gate style used elsewhere in this program ----
    checks = {}
    checks["min_30_oos_observations"] = combined["n_obs"] >= 30
    checks["positive_oos_gross_mean"] = combined["gross"].get("mean_pct", -1) > 0
    max_cost = combined["cost_scenarios"].get(f"{max(COST_SCENARIOS_BPS)}bps", {})
    checks["positive_after_worst_cost_scenario"] = (
        isinstance(max_cost.get("mean_pct"), (int, float)) and max_cost["mean_pct"] > 0
    )
    boot = combined["gross"].get("bootstrap_mean_ci95")
    checks["oos_bootstrap_lower_above_zero"] = bool(boot and boot[0] is not None and boot[0] > 0)
    checks["both_oos_blocks_gross_positive"] = (
        validation["gross"].get("mean_pct", -1) > 0
        and confirmation["gross"].get("mean_pct", -1) > 0
    )
    checks["random_direction_p_below_0_05"] = (
        combined["random_direction_falsification"]["one_sided_p_value"] < 0.05
    )
    checks["positive_without_best_trade"] = (
        combined["gross"].get("mean_without_best_pct") is not None
        and combined["gross"]["mean_without_best_pct"] > 0
    )
    status = "PASS" if all(checks.values()) else (
        "INSUFFICIENT_EVIDENCE" if not checks["min_30_oos_observations"] else "FAIL"
    )

    return {
        "status": status,
        "hypothesis": hypothesis_text,
        "frozen_rule": {
            "divergence_threshold_rsi_points": frozen_threshold,
            "horizon_trading_days": frozen_horizon,
            "selection_note": "chosen on the discovery slice only, by best "
                               "positive day-block-bootstrap lower bound "
                               "after a 2bps cost stress; never re-selected "
                               "or adjusted afterward",
        },
        "discovery": {
            "days": len(discovery_rows),
            "date_range": [str(discovery_rows[0]["date"]), str(discovery_rows[-1]["date"])],
            "grid_results": grid_results,
        },
        "validation": validation,
        "confirmation": confirmation,
        "combined_out_of_sample": combined,
        "decision": {"checks": checks, "status": status},
    }


def run() -> dict:
    start = datetime(2016, 1, 1)
    end = datetime(2026, 8, 27)
    bars_by_symbol = {s: load_alpaca(s, start, end) for s in SYMBOLS}
    rows = build_daily_table(bars_by_symbol)

    result = _evaluate_hypothesis(
        rows,
        hypothesis_text=(
            "relative RSI divergence among SPY/QQQ/IWM predicts convergence "
            "of the divergent symbol's return toward its two peers "
            "(market-neutral spread), independent of overall market direction"
        ),
        momentum=False,
    )
    result["data"] = {
        "symbols": SYMBOLS,
        "source": "Alpaca historical daily bars",
        "total_common_days": len(rows),
    }
    return result


def run_momentum() -> dict:
    """Continuation (mirror-image) hypothesis, restricted to the calendar
    slice run()'s reversion test reserved for validation/confirmation but
    never actually scored (its discovery stage never froze a candidate).
    Never touches the reversion discovery slice, so this hypothesis's own
    discovery stage is not contaminated by having already seen those results.
    """
    start = datetime(2016, 1, 1)
    end = datetime(2026, 8, 27)
    bars_by_symbol = {s: load_alpaca(s, start, end) for s in SYMBOLS}
    all_rows = build_daily_table(bars_by_symbol)

    # Exactly the boundary run() computes, so this is precisely the slice
    # the reversion test never scored -- not an approximation of it.
    reversion_d_end = int(len(all_rows) * DISCOVERY_FRAC)
    unseen_rows = all_rows[reversion_d_end:]

    result = _evaluate_hypothesis(
        unseen_rows,
        hypothesis_text=(
            "relative RSI divergence among SPY/QQQ/IWM predicts CONTINUED "
            "divergence (laggard keeps lagging, leader keeps leading) rather "
            "than convergence -- the mirror image of the rejected reversion "
            "hypothesis in run(), tested only on the calendar slice that "
            "test never scored in either direction"
        ),
        momentum=True,
    )
    result["data"] = {
        "symbols": SYMBOLS,
        "source": "Alpaca historical daily bars",
        "total_common_days_full_history": len(all_rows),
        "reversion_discovery_cutoff_date": str(all_rows[reversion_d_end]["date"]),
        "unseen_slice_days": len(unseen_rows),
        "note": "unseen_rows = all_rows after the exact date reversion's "
                "discovery slice ended; reversion's discovery grid was never "
                "computed on this slice, only on all_rows[:reversion_d_end]",
    }
    return result


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--momentum", action="store_true",
                         help="run the continuation hypothesis instead of "
                              "the (already rejected) reversion one")
    args = parser.parse_args()
    report = run_momentum() if args.momentum else run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps({"status": report.get("status"),
                       "decision": report.get("decision"),
                       "output": str(args.output)}, indent=2, default=str))


if __name__ == "__main__":
    main()
