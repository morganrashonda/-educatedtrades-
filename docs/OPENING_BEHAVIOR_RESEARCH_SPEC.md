# Opening-behavior research specification

Status: research-only specification. It does not place orders, write learning
outcomes, or change the production strategy.

## Objective

Measure whether information available before or during the U.S. cash open
changes the conditional distribution of the next move for **NQ futures** and
**QQQ separately**. The target is behavior, not a chart pattern selected after
the fact:

> Given the opening state at time `t`, what does price tend to do over the next
> 5, 15, 30, 60, and 120 minutes, after realistic costs?

No result is a Tier 3 strategy until it survives the promotion gates below.

## Data contract

The study requires one-minute bars for the most recent complete year available
at the frozen cutoff. The cutoff, provider, feed, and download checksum are
recorded in a manifest. Raw vendor data is not committed to Git.

### QQQ

- instrument: QQQ shares;
- consolidated SIP feed where available;
- regular session: 09:30–16:00 America/New_York;
- extended-hours data retained separately, never silently mixed into RTH;
- corporate-action treatment recorded and applied consistently.

### NQ

- instrument: the actual front-month NQ contract, with contract month retained;
- continuous series may be used for research features only and must document
  the rollover rule;
- CME Globex session retained separately from the U.S. cash-open window;
- prices in index points, with $20 per point and 0.25-point ticks;
- commissions, exchange fees, spread, slippage, and rollover treatment stored
  as explicit assumptions.

### Required quality checks

The loader must report, per instrument and day:

- missing bars and duplicate timestamps;
- timezone conversion and session boundaries;
- non-monotonic timestamps;
- zero/negative/invalid prices;
- impossible OHLC relationships;
- contract changes for NQ;
- whether the day is a normal, half, or holiday session.

An incomplete or invalid day is excluded from a result table and counted in a
data-quality report. It is never silently filled with synthetic prices.

## Daily opening record

One record is created for each valid trading day. Fields are computed in
America/New_York for the cash open and retain UTC timestamps for auditability.

### Pre-open fields

- prior regular-session close;
- current cash-session open;
- gap in points and percent;
- overnight high, low, range, return, and direction;
- prior-day high, low, range, and midpoint;
- distance from the open to each reference level;
- prior 5-, 20-, and 60-session realized volatility;
- scheduled-event flag, if an authorized calendar is supplied.

### Opening-window fields

For each of 1, 5, 15, and 30 minutes:

- opening high, low, range, and midpoint;
- range normalized by prior volatility and overnight range;
- open-to-window-close return;
- body size, upper wick, and lower wick;
- volume and relative volume versus the same window over prior sessions;
- first break direction and break timestamp;
- whether price held outside or re-entered the range.

No feature may use a bar whose close occurs after the decision timestamp.

## Behavior labels

Labels are forward outcomes, not inputs. They are calculated only after the
opening record is frozen.

For each horizon of 5, 15, 30, 60, and 120 minutes, record:

- forward return from the decision price;
- maximum favorable excursion;
- maximum adverse excursion;
- first barrier reached: target, stop, neither, or ambiguous;
- time to each barrier;
- whether the opening range was reclaimed or held;
- whether the day later made a trend day or range day classification.

When one bar touches both a stop and target, the result is ambiguous at that
resolution and is not awarded to either side. A higher-resolution dataset is
required to resolve it; the harness must not assume an intrabar order.

## Pre-registered pattern families

Each family is evaluated independently for NQ and QQQ, long and short.

1. **Opening continuation:** close beyond an opening extreme and hold outside
   the range for the confirmation rule.
2. **Failed breakout:** break an opening extreme, re-enter the range, and
   confirm in the opposite direction.
3. **Gap continuation:** opening gap and first impulse agree, with no early
   gap-reclaim confirmation.
4. **Gap fill:** opening gap is followed by a reclaim of the prior close.
5. **Extreme reversal:** price tests an overnight or prior-day extreme,
   rejects it, and confirms through the last local swing.
6. **Cross-market agreement/divergence:** NQ and QQQ opening states agree or
   disagree. This is a feature comparison, not pooled P&L.

The confirmation rule, target, stop, and time limit are parameters recorded
before the evaluation split. A pattern cannot be redefined after seeing its
results.

## Statistics

Every pattern report includes:

- observations and independent trading days;
- mean net trade expectancy;
- median net trade;
- win rate and Wilson interval;
- average winner and average loser;
- profit factor;
- standard deviation and downside deviation;
- maximum drawdown and consecutive losses;
- favorable/adverse excursion;
- cost and slippage sensitivity;
- outlier influence and leave-one-day-out stability.

Mean expectancy is essential but insufficient. A positive mean driven by one
large day does not qualify as evidence if the median, uncertainty interval, or
cost-stress result fails.

## Validation design

The initial one-year study uses a chronological split, frozen before analysis:

- discovery: first six months;
- validation: following three months;
- untouched confirmation: final three months.

The discovery period may identify a candidate, but it may not be used to tune
the confirmation period. After any material rule change, the candidate returns
to discovery and the validation/confirmation clock restarts.

The report must also include rolling walk-forward results and day/block
bootstrap intervals so clustered trades from one market event do not appear to
be independent evidence.

## Tier 3 promotion gates

A candidate remains research-only unless all are true:

- the data-quality report has no unresolved critical defects;
- the rule is deterministic and timestamp-auditable;
- validation and untouched confirmation both have sufficient observations;
- mean net expectancy is positive and not dependent on one outlier day;
- median result and cost-stress result remain acceptable;
- drawdown and consecutive-loss limits fit the declared risk budget;
- the effect is not erased by reasonable spread, slippage, or delayed entry;
- NQ and QQQ conclusions are reported separately;
- no look-ahead, synthetic data, or post-result feature selection is present.

Promotion order is: research report → shadow signals → paper-only Tier 3 →
independent review → explicit owner authorization. Learning and live execution
remain disabled throughout research and shadow phases.

## Current implementation boundary

Main 5 currently has daily equity research and production ETF execution. It
does not yet have the NQ futures data/rollover/execution adapter required to
trade NQ. The first implementation packet should therefore be a read-only
research harness with provider-neutral normalized input, not a production
signal or order path.
