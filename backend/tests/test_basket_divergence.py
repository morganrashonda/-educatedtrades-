"""Regression tests for backend/research/basket_divergence.py.

Run standalone: python3 backend/tests/test_basket_divergence.py
(no pytest required, matching this repo's other research test files.)
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATA_DIR", "/tmp")

import research.basket_divergence as bd  # noqa: E402

D0 = date(2020, 1, 1)


def _flat_bars(days=40, shock_day=25, shock_symbol="QQQ", drop=0.85,
                recover_day=28, recover_mult=None):
    """SPY/IWM oscillate with tiny alternating noise around 100 (never net
    trending); one symbol drops then (optionally) recovers on top of that,
    so the resulting laggard/leader labels are unambiguous.

    A perfectly dead-flat series is deliberately avoided: patterns.py's real
    compute_rsi() returns 100 whenever avg_loss==0, which a zero-volatility
    series satisfies just as much as a genuinely rising one -- so dead-flat
    "peers" would peg at the same RSI ceiling as a spiking "leader" and mask
    exactly the case this test needs to exercise.

    build_daily_table() drops the first RSI_PERIOD-ish days (not enough
    history yet), so `rows[i]` is NOT the i-th calendar day -- tests must
    look events up by date (D0 + timedelta(days=shock_day)), never by row
    index.
    """
    if recover_mult is None:
        recover_mult = (1 / drop) * 1.02  # recovers plus a bit
    bars = {"SPY": [], "QQQ": [], "IWM": []}
    px = {"SPY": 100.0, "QQQ": 100.0, "IWM": 100.0}
    for i in range(days):
        d = D0 + timedelta(days=i)
        noise = 1.003 if i % 2 == 0 else (1 / 1.003)
        for s in px:
            px[s] *= noise
        if i == shock_day:
            px[shock_symbol] *= drop
        if recover_day is not None and i == recover_day:
            px[shock_symbol] *= recover_mult
        for s in bars:
            bars[s].append(bd.Bar(d, px[s], px[s], px[s], px[s], 1000.0))
    return bars


class TestBasketDivergence(unittest.TestCase):
    def test_no_lookahead_in_rsi_construction(self):
        """The shock day's RSI must not change if only FUTURE prices change."""
        shock_date = D0 + timedelta(days=25)
        bars_a = _flat_bars(days=35, shock_day=25, recover_day=None)
        bars_b = _flat_bars(days=35, shock_day=25, recover_day=None)
        for s in bars_b:
            for bar in bars_b[s]:
                if bar.d > shock_date:
                    bar.c *= 1.5
        rows_a = {r["date"]: r for r in bd.build_daily_table(bars_a)}
        rows_b = {r["date"]: r for r in bd.build_daily_table(bars_b)}
        self.assertIn(shock_date, rows_a)
        self.assertEqual(rows_a[shock_date]["rsi"]["QQQ"],
                          rows_b[shock_date]["rsi"]["QQQ"],
                          "RSI on the shock day changed when only FUTURE "
                          "prices were altered -- that would be lookahead.")

    def test_laggard_that_catches_up_scores_positive(self):
        """A symbol that crashes then recovers relative to flat peers must
        be labeled 'laggard' with a positive forward spread_return -- this
        is the entire sign convention the discovery-grid result depends on."""
        shock_date = D0 + timedelta(days=25)
        bars = _flat_bars(days=40, shock_day=25, drop=0.85, recover_day=28)
        rows = bd.build_daily_table(bars)
        obs = bd.relative_signals(rows, threshold=5.0, horizon=3)
        matches = [o for o in obs if o["date"] == shock_date and o["symbol"] == "QQQ"]
        self.assertEqual(len(matches), 1, f"no match on {shock_date}; got dates {sorted(set(o['date'] for o in obs))}")
        self.assertEqual(matches[0]["side"], "laggard")
        self.assertGreater(matches[0]["spread_return_pct"], 0,
                            "a laggard that recovers relative to its peers "
                            "must score a positive spread return")

    def test_leader_that_gives_back_gains_scores_positive(self):
        """Mirror case: a symbol that spikes then gives the spike back must
        be labeled 'leader' with a positive spread_return (short-the-spread
        pays off), not negative."""
        shock_date = D0 + timedelta(days=25)
        bars = _flat_bars(days=40, shock_day=25, drop=1.20,
                           recover_day=28, recover_mult=(1 / 1.20) * 0.98)
        rows = bd.build_daily_table(bars)
        obs = bd.relative_signals(rows, threshold=5.0, horizon=3)
        matches = [o for o in obs if o["date"] == shock_date and o["symbol"] == "QQQ"]
        self.assertEqual(len(matches), 1, f"no match on {shock_date}; got dates {sorted(set(o['date'] for o in obs))}")
        self.assertEqual(matches[0]["side"], "leader")
        self.assertGreater(matches[0]["spread_return_pct"], 0)

    def test_wilson_bounds_matches_production_shape(self):
        lo, hi = bd._wilson_bounds(50, 100)
        self.assertTrue(0.0 <= lo < 0.5 < hi <= 1.0)

    def test_threshold_filters_out_small_divergences(self):
        bars = _flat_bars(days=10, shock_day=999, recover_day=None)
        rows = bd.build_daily_table(bars)
        obs = bd.relative_signals(rows, threshold=5.0, horizon=1)
        self.assertEqual(obs, [], "flat, identical baskets must never diverge")

    def test_momentum_is_the_exact_negation_of_reversion(self):
        """momentum=True must never do anything except flip the sign -- same
        observations, same dates, same sides, only spread_return negated."""
        bars = _flat_bars(days=40, shock_day=25, drop=0.85, recover_day=28)
        rows = bd.build_daily_table(bars)
        reversion = bd.relative_signals(rows, threshold=5.0, horizon=3, momentum=False)
        momentum = bd.relative_signals(rows, threshold=5.0, horizon=3, momentum=True)
        self.assertEqual(len(reversion), len(momentum))
        self.assertGreater(len(reversion), 0)
        for r, m in zip(reversion, momentum):
            self.assertEqual(r["date"], m["date"])
            self.assertEqual(r["symbol"], m["symbol"])
            self.assertEqual(r["side"], m["side"])
            self.assertAlmostEqual(r["spread_return_pct"], -m["spread_return_pct"], places=9)

    def test_run_momentum_unseen_slice_excludes_reversion_discovery(self):
        """run_momentum()'s data slice must start strictly after the exact
        date reversion's own discovery slice ends -- no overlap, so momentum's
        discovery stage never reuses a day reversion already scored."""
        bars = {s: [] for s in bd.SYMBOLS}
        px = {s: 100.0 for s in bd.SYMBOLS}
        for i in range(200):
            d = D0 + timedelta(days=i)
            noise = 1.004 if i % 2 == 0 else (1 / 1.004)
            for s in px:
                px[s] *= noise
            if i % 17 == 0:
                px["QQQ"] *= 0.9
            for s in bars:
                bars[s].append(bd.Bar(d, px[s], px[s], px[s], px[s], 1000.0))

        all_rows = bd.build_daily_table(bars)
        expected_cutoff = int(len(all_rows) * bd.DISCOVERY_FRAC)
        expected_first_unseen_date = all_rows[expected_cutoff]["date"]

        orig_load = bd.load_alpaca
        bd.load_alpaca = lambda symbol, start, end: [
            b for b in bars[symbol]
        ]
        try:
            result = bd.run_momentum()
        finally:
            bd.load_alpaca = orig_load

        self.assertEqual(result["data"]["reversion_discovery_cutoff_date"],
                          str(expected_first_unseen_date))
        self.assertEqual(result["data"]["unseen_slice_days"],
                          len(all_rows) - expected_cutoff)


if __name__ == "__main__":
    unittest.main(verbosity=2)
