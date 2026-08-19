# Opening Order-Flow and Candle-Mechanism Research

Status: research-only. This document does not authorize production signals,
Tier 3 execution, learning writes, or live orders.

## Objective

Test whether the behavior behind an opening candle—auction pressure, liquidity,
absorption, and acceptance/rejection—contains incremental directional
information for NQ futures and QQQ shares, reported separately.

Named candle labels such as hammer or engulfing are not treated as causal
features. Candle geometry is measured as a compressed observation of the
underlying order-flow process.

## Pre-registered hypotheses

### H1: acceptance continuation

After the first opening range is defined, a close outside the range is a
continuation candidate only when the next two bars remain outside the range,
relative volume is elevated, and signed order flow agrees with the break.

### H2: failed-break reversal

A break of the opening or overnight extreme is a reversal candidate when price
closes back inside the range within the confirmation window and signed order
flow changes direction.

### H3: absorption

When aggressive volume is unusually high but price progress is unusually small
near an overnight or prior-day extreme, test whether the next move is more
likely to reject that level. The inverse is tested at support.

### H4: pressure agreement

Test whether candle direction, signed trade imbalance, book imbalance, and
microprice direction agreeing produces better conditional returns than any one
feature alone. Disagreement is a separate state, not missing data.

## Features

For each one-minute and five-minute opening bar:

- body, total range, upper wick, lower wick;
- body/range and close location `(close-low)/(high-low)`;
- range and body normalized by prior same-time volatility;
- volume surprise versus the same time-of-day history;
- signed trade volume and buy/sell volume imbalance;
- best-bid/best-ask size imbalance;
- top-of-book spread and depth;
- microprice minus midprice;
- distance to opening range, overnight extreme, and prior-day extreme;
- break, hold, re-entry, retest, and time-to-failure states.

## Data contract

### NQ

Use Databento GLBX.MDP3 with the actual contract identity retained alongside
the continuous-series research view. OHLCV is insufficient for the causal
mechanism study; the order-flow extension requires MBP-1 or MBP-10, and trades
for signed-volume validation.

### QQQ

Use consolidated SIP bars plus historical trades/quotes when authorized and
available. QQQ results remain separate from NQ results; agreement is a feature,
not pooled evidence.

## Causal-falsification design

The study cannot prove economic causality from observational data, but it can
reject weak explanations. Every candidate must pass:

1. timestamp audit: every feature precedes the label;
2. placebo lead test: future order flow must not predict the past;
3. block permutation test preserving day/session clustering;
4. matched controls by date, volatility, gap, and time of day;
5. chronological discovery, validation, and untouched confirmation;
6. cost and delayed-entry stress;
7. multiple-testing correction across all candle/order-flow hypotheses;
8. leave-one-day-out and rolling-block stability.

## Primary labels

At 5, 15, 30, 60, and 120 minutes after the decision timestamp:

- signed return;
- MFE and MAE;
- first target/stop barrier and ambiguous-bar count;
- acceptance, rejection, and re-entry outcome;
- net expectancy after spread, slippage, and commission assumptions.

## Promotion gates

No candidate is eligible for Tier 3 unless it has adequate observations in both
NQ and QQQ, positive median and mean net expectancy, a confidence interval that
does not depend on one event cluster, acceptable cost stress, and positive
untouched confirmation results. A research finding never enables learning or
execution automatically.

## Frozen MBP-1 pilot hypothesis (2026-08-18)

Exploratory work on six NQ sessions found that raw one-second OFI was almost
indistinguishable from contemporaneous five-second price momentum. The only
hypothesis retained for new, untouched sessions is therefore a mechanism-
qualified continuation rule:

- decision buckets are non-overlapping five-second windows from 13:30–13:40
  UTC for the current daylight-saving-time sample;
- direction is the sign of the five-second midprice change;
- depth-normalized OFI must agree with direction and be at least `104.18738`;
- directional refill support must be at least `0.1954`;
- decisions have a 120-second cooldown;
- the frozen primary target is the signed 120-second midprice return;
- spread, slippage, commissions, day clustering and an untouched chronological
  sample remain mandatory before any economic claim.

The six inspected sessions are discovery data, including the three sessions
initially treated as a check set. They are contaminated by subsequent feature
and horizon inspection and must never be represented as final holdout proof.

### Current evidence state

- Inspected six-session discovery set: 26 cooldown-separated signals, 17 wins,
  `+2.591 bp` mean gross signed return at 120 seconds.
- Previously uninspected prior-date robustness set (2026-07-27 through
  2026-08-07): 41 signals, 24 wins, `+1.848 bp` mean gross signed return.
- The robustness set averaged `+4.255` NQ points after a top-of-book crossing
  estimate, before commissions and additional slippage.
- A deterministic session-cluster bootstrap placed the robustness-set 95%
  interval at `[-8.410, +15.371]` points, which includes zero by a wide margin.

Therefore this is a retained research spark, not established edge. The
prior-date extension is useful falsification evidence but is not a future
chronological confirmation set. Thresholds and the 120-second target are now
frozen; future results must be reported without retuning them.

### Frozen 50-session validation outcome

The unchanged rule was subsequently run on 50 additional cash-market sessions
from 2026-05-13 through 2026-07-24. Those dates were selected before loading
their MBP-1 features by intersecting existing NQ and QQQ session dates.

- 199 cooldown-separated signals; 102 positive gross outcomes (`51.3%`).
- Mean gross signed return: `+0.193 bp`; median: `+0.205 bp`.
- Mean after the observed top-of-book crossing estimate: `-0.660` NQ points,
  before commissions and additional slippage.
- 96 of 199 signals were positive after spread; 27 of 50 sessions had positive
  mean results and 23 had negative mean results.
- Session-cluster bootstrap 95% interval after spread:
  `[-5.674, +4.505]` points.
- Monthly after-spread means deteriorated from `+1.236` points in May to
  `-0.980` in June and `-1.432` in July.

Post-hoc side separation did not establish an alternative edge. Long signals
averaged `+1.040` points after spread but had a negative median, a negative
June, and a session-bootstrap interval of `[-9.219, +6.966]`. Short signals
averaged `-2.214` points.

**Decision: reject this frozen rule as a tradable Tier 3 candidate.** It does
not clear costs, stability, or confidence gates. The result must not enable
learning or execution, and its thresholds must not be optimized on this
validation set and re-described as out-of-sample evidence.
