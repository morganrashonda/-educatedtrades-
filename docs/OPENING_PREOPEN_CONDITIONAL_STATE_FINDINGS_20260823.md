# Exploratory findings: pre-open flow-alignment state

## Status

This is an exploratory historical result, not a production signal and not a
claim of a proven edge. The forward observer in
`opening_preopen_conditional_forward.py` is the authoritative next test.

## State tested

For each session, use only information available before 09:30 ET:

1. Calculate signed aggressive trade-flow imbalance from the pre-open MBP
   observations.
2. Calculate the pre-open price return.
3. Require the flow sign and price-return sign to agree.
4. Require absolute flow score to be at least 0.10.
5. Measure the directionally appropriate executable two-minute markout from
   the opening ask/bid to the 09:32 bid/ask.

The all-nonzero-flow direction is retained as the baseline. The conditional
state is only the aligned, high-magnitude subset.

## Exploratory result

On the available five-year opening dataset, using a chronological 70/30 split
and a threshold determined from the training portion only:

- Training-derived flow threshold: approximately 0.1048.
- Held-out candidate observations: 33.
- Held-out directional accuracy: 66.7%.
- Mean held-out outcome after the 2.25-point stress: approximately +14.35 NQ
  points.
- A simple session bootstrap interval for the held-out mean was approximately
  -0.85 to +29.19 points, crossing zero.

Rolling chronological checks were directionally positive but small:

| Training fraction | Test window | n | Accuracy | Mean stressed outcome |
| --- | --- | ---: | ---: | ---: |
| 50% | 2023-12-05 to 2025-02-27 | 19 | 52.6% | +2.39 |
| 60% | 2024-08-23 to 2025-10-06 | 17 | 64.7% | +14.84 |
| 70% | 2025-03-05 to 2026-03-25 | 15 | 60.0% | +12.77 |

## What can safely be said

The state is a worthwhile pre-registered candidate for forward observation:
it is specific, available before the decision, executable-marked, and has a
natural baseline. The evidence is not yet sufficient to call it repeatable or
to trade it. The sample is small, the bootstrap interval crosses zero, and
the large point averages may partly reflect high-volatility mornings.

The forward ledger must therefore report:

- candidate and all-flow baseline side by side;
- equal-signal and observation-weighted means;
- win rate and stressed executable markouts;
- session-cluster uncertainty;
- missing-data and no-signal counts;
- calendar-month and regime context when available later.

No threshold tuning, promotion, Tier 3 connection, or order execution is
authorized by this note.
