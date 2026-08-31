# SPY/QQQ/IWM relative-value divergence — findings, 2026-08-28

Status: complete research test, frozen protocol. Result: **rejected at the
discovery stage** — every combination in the grid failed, decisively, not
marginally. No production, Tier 3, learning, or order behavior changed.

## What was tested

Per [docs/BASKET_DIVERGENCE_RESEARCH_SPEC.md](BASKET_DIVERGENCE_RESEARCH_SPEC.md):
whether a symbol whose daily RSI diverges sharply from its two SPY/QQQ/IWM
peers subsequently converges back toward them. Data: 2,677 Alpaca daily bars
per symbol, 2016-01-04 through 2026-08-26. Chronological 60/20/20 split by
day count: 1,597 discovery days, then validation, then confirmation.

## Discovery-stage grid result

30 (threshold × horizon) combinations were evaluated on the discovery slice
alone. **Every single one produced a negative mean spread return.** This
ruled out proceeding to validation/confirmation entirely — the spec commits
in advance to only freezing and carrying forward a candidate that clears a
positive bootstrap lower bound on discovery; none did, so nothing was frozen
and validation/confirmation were never run.

The least-bad combination (threshold=15 RSI points, horizon=1 day): 619
observations, mean −0.0104%, win rate 49.3%, bootstrap 95% CI
[−0.071%, +0.054%] — statistically indistinguishable from zero, the closest
thing to a null result in the grid.

The pattern across the full grid is not noise-shaped, though. It is
monotonic and consistent:

| Threshold (RSI pts) | horizon=1 mean % | horizon=5 mean % | horizon=10 mean % |
|---:|---:|---:|---:|
| 5.0  | −0.028 | −0.074 | −0.162 |
| 7.5  | −0.020 | −0.079 | −0.170 |
| 10.0 | −0.031 | −0.108 | −0.213 |
| 12.5 | −0.013 | −0.134 | −0.287 |
| 15.0 | −0.010 | −0.141 | −0.362 |
| 20.0 | −0.042 | −0.155 | −0.384 |

Every cell is negative. The effect gets **worse**, not better, as the
horizon lengthens, and the 10-day column's bootstrap intervals sit entirely
below zero at every threshold from 7.5 upward (e.g. threshold=20,
horizon=10: mean −0.384%, win rate 43.1%, bootstrap 95% CI
[−0.645%, −0.107%] — a confident, not marginal, negative result).

## Interpretation

The hypothesized *direction* was wrong. A basket member that diverges from
SPY/QQQ/IWM's other two does not converge back over the following 1–10
trading days — if anything, the divergence tends to persist or widen,
consistent with relative-strength/momentum continuation rather than
mean-reversion at this horizon and this granularity (daily bars). This is a
stronger, more informative outcome than "no evidence either way": the data
did not shrug, it consistently pointed the opposite way.

This does **not** get reported as a discovered momentum edge. The grid above
was generated and inspected before any conclusion was drawn, so simply
flipping every sign and calling the mirror-image rule validated would be
exactly the kind of post-hoc, look-then-decide reasoning this research
program's own falsification tests (e.g. `opening_direction_falsification.py`)
exist to catch. A momentum-continuation version of this hypothesis is
plausible enough to be worth testing — but only as its own frozen spec,
scored against data this analysis has not already seen, not as a same-data
sign-flip of a rejected result.

## A note on mechanics, not conclusions

While building this test, `patterns.compute_rsi` (the real production
function, reused here deliberately for consistency) was confirmed to return
exactly 100 whenever the trailing average loss is zero — which a
genuinely flat, non-moving price series satisfies just as much as a rising
one. This only surfaced while writing a synthetic test fixture (a literally
flat "peer" series pinned to RSI 100, indistinguishable from a spiking
"leader"); it does not appear to be reachable with real market data, which
essentially never holds a price perfectly constant for 15 consecutive bars.
Noted for completeness, not flagged as a defect to fix.

## Verification

- New regression tests for this module: 5/5 passed
  (`backend/tests/test_basket_divergence.py`), covering no-lookahead RSI
  construction, the laggard/leader sign convention in both directions, the
  Wilson-bound helper, and threshold filtering.
- Full backend safety/integrity suite: 858/858 passed (unaffected — this
  module is standalone and imports nothing production-facing).

## Next gate

None proposed for this specific hypothesis — it is rejected, not deferred.
The one legitimate follow-on is a **separately specified** momentum/
continuation test, frozen before touching any of the data this analysis
already looked at (e.g. a genuinely held-out period going forward, or an
intraday timeframe never examined here), so its own discovery stage is not
contaminated by having already seen this grid.
