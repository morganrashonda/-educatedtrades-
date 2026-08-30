# SPY/QQQ/IWM relative-strength continuation — findings, 2026-08-28

Status: complete research test, frozen protocol. Result:
**INSUFFICIENT_EVIDENCE** — directionally promising, statistically not yet
distinguishable from noise on this sample. Not a pass. Not the same kind of
clean rejection as the paired reversion test. No production, Tier 3,
learning, or order behavior changed.

## What was tested

Per [docs/BASKET_MOMENTUM_CONTINUATION_RESEARCH_SPEC.md](BASKET_MOMENTUM_CONTINUATION_RESEARCH_SPEC.md):
the mirror image of the rejected reversion hypothesis, scored only on the
calendar slice reversion's own discovery stage never touched — the ~40% of
the 2016–2026 daily history after 2022-05-26 (1,066 days), which reversion
reserved for validation/confirmation but never actually ran, since its
discovery stage never froze a candidate.

Within that slice: 639 days for this hypothesis's own discovery, then a
fresh 20/20 validation/confirmation split of the remainder.

## Discovery-stage grid result

Unlike reversion (30/30 grid cells negative), this grid is mixed, with the
best results at wider thresholds and longer horizons:

| Threshold (RSI pts) | horizon | n | win rate | mean % | bootstrap 95% CI | profit factor |
|---:|---:|---:|---:|---:|---|---:|
| 12.5 | 10 | 275 | 55.3% | +0.215 | [−0.196, +0.619] | 1.23 |
| 15.0 | 10 | 199 | 56.8% | +0.205 | [−0.252, +0.653] | 1.22 |
| 15.0 | 5  | 199 | 52.8% | +0.185 | [−0.184, +0.554] | 1.26 |
| 15.0 | 3  | 199 | 54.3% | +0.172 | [−0.108, +0.459] | 1.33 |

Every one of these point estimates is positive, with win rates above 50%
and profit factors above 1.2 — a real contrast with reversion, where every
cell was negative. But **none clear the pre-registered bar**: the day-block
bootstrap 95% lower bound is negative in every single case, including the
best-looking cells above. At tighter thresholds (5.0, 7.5) with much larger
sample sizes (n>600), the point estimates are close to flat and slightly
negative, consistent with reversion's own weakest (near-zero) results at
those same thresholds.

Because no cell cleared the discovery bar, **no candidate was frozen, and
validation/confirmation were never run** — same protocol reversion followed,
applied honestly to this hypothesis too.

## Why "insufficient," not "rejected"

The wider-threshold cells (12.5–20 RSI points) that look best are exactly
the ones with the fewest observations — as few as 101–275 — because a large
divergence between two of three highly correlated ETFs is a genuinely rare
event. The bootstrap correctly reports that a sample this size cannot yet
rule out zero, even though the point estimate is consistently positive
across every horizon at those thresholds. That is a real distinction from
reversion, where increasing the sample size (tighter thresholds, more
observations) made the negative result *more* confident, not less.

## Interpretation

This does not get reported as a discovered edge, and it does not get
reported as dead the way reversion was. The honest statement is: relative-
strength continuation among SPY/QQQ/IWM shows a consistent positive
direction at wider divergence thresholds and multi-day horizons, but the
event is rare enough that ~4.5 years of daily data (639 discovery days) is
not enough to separate it from chance. More calendar time, or a design that
generates more observations per unit time (e.g. an intraday RSI rather than
daily, or extending the basket beyond three symbols), would be needed before
this could reach a validation stage at all.

## Verification

- New regression tests for the momentum path: 2/2 passed, on top of the
  existing 5 (7/7 total in
  `backend/tests/test_basket_divergence.py`) — covering that momentum mode
  is the exact negation of reversion mode (no other behavior change) and
  that the unseen-slice boundary used here is provably the exact date
  reversion's own discovery stage stopped at, with no overlap.
- Full backend safety/integrity suite: 858/858 passed.
- End-to-end suite: 33/33 passed.

## Next gate

Not a research dead end, unlike reversion — but not ready for a validation
run either. The defensible next step, if this is worth pursuing further, is
increasing statistical power before spending a validation/confirmation
slice on it: either accumulate more calendar time naturally, or redesign the
divergence measurement at a higher frequency (intraday) so wide-divergence
events occur often enough to bootstrap a confident interval. Re-running the
existing daily-bar version on more data without a power increase would just
reproduce the same "promising but inconclusive" result.
