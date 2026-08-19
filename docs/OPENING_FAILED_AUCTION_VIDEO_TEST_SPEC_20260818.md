# Opening Failed-Auction / Absorption Test

Status: pre-registered research only. This document does not authorize a
production signal, Tier 3 execution, learning writes, paper orders, or live
orders.

Frozen: 2026-08-18, before evaluating this sequence's historical outcomes.

## Question

Does the video-inspired sequence—location, aggressive effort without price
progress, opposite-side dominance, reclaim, and failed retest—identify an
opening reversal with positive executable expectancy beyond a bare failed
break?

This is separate from the immediate 09:30 two-minute gap-fade study. The
failed-auction signal cannot enter at 09:30 unless every required confirmation
has already occurred. Its clock starts when the final confirmation becomes
observable.

## Evidence hierarchy

1. The video supplies a mechanism hypothesis, not proof.
2. May-August 2026 NQ MBP-1 features are discovery data. Prior order-flow
   outcomes from this period have been inspected, so no result from it can be
   called untouched confirmation.
3. Thresholds operationalized on discovery data are frozen before loading an
   older validation sample.
4. A later chronological sample, never used for threshold or rule selection,
   is required for confirmation.

The previously frozen raw-OFI continuation rule was rejected and is not a
candidate in this study.

## Instrument and time

- Primary instrument: NQ continuous research series from Databento GLBX.MDP3,
  with actual instrument ID retained and roll-transition sessions excluded.
- Event data: MBP-1 with trade events and contemporaneous top-of-book state.
- Session timezone: America/New_York, using exchange-calendar dates rather
  than fixed UTC offsets.
- Search interval: 09:30:00 through 10:30:00 ET.
- Decision buckets: non-overlapping five-second intervals.
- Price tick: 0.25 NQ points.

QQQ is not pooled with NQ. A later QQQ test is separate corroboration.

## Levels known at the time

Phase 1 uses only levels that can be reconstructed without future opening
information:

- prior regular-session high and low;
- overnight high and low measured from 18:00 ET through 09:29:59 ET.

The five-minute opening-range high/low becomes eligible only after 09:35:00
ET. Exact VAH, VAL, and POC are deferred until point-in-time profile data are
available; they must not be approximated and labelled as volume-profile
levels.

If two levels are within four ticks, they form one level cluster. The cluster
is one opportunity, preventing duplicate signals from the same auction.

## Frozen event state machine

All signs below are expressed in the direction of the attempted break:
`s=+1` for an upper-level break and `s=-1` for a lower-level break.

### S0: first break

The first BBO midprice at least one tick beyond a known level starts an attempt.
The attempt expires after 180 seconds. A new attempt at the same level cannot
start for 300 seconds after expiry or signal completion.

### S1: absorption candidate

Within 30 seconds of S0, evaluate rolling ten-second windows. Absorption needs
both high directional effort and weak directional result:

- directional aggressive trade volume is at or above its expanding,
  same-time-of-day 75th percentile computed only from earlier discovery days;
- directional price progress per aggressive contract is at or below its
  expanding, same-time-of-day 25th percentile; and
- total aggressive volume is nonzero and the BBO remains valid.

For an upper break, directional aggressive volume is buy volume and progress
is the positive change in midprice. For a lower break, they are sell volume and
negative midprice change. Adverse progress is clipped to zero before dividing
by volume. Quantile estimates require at least 20 earlier observations; events
without a valid baseline are `INSUFFICIENT_BASELINE`, not imputed.

This relative definition avoids choosing a flattering fixed contract count
from future data and adapts to time-of-day liquidity.

### S2: opposite dominance and reclaim

Within 60 seconds after S1, one five-second bucket must satisfy all of:

- signed aggressive trade imbalance points opposite the attempted break and
  has absolute magnitude at least 0.20;
- depth-normalized OFI points opposite the attempted break;
- the decision-bucket close midprice is at least one tick back inside the
  broken level.

The end of this bucket is the earliest `absorption + shift + reclaim` decision
timestamp.

### S3: failed retest

Within 120 seconds after S2, price must return to within two ticks of the level,
must not extend beyond the S0/S1 extreme by more than one tick, and must then
close at least two ticks back inside the level in a later five-second bucket.
The end of that later bucket is the full-sequence decision timestamp.

If no retest occurs, the full sequence does not signal. That no-retest state is
reported separately rather than silently treated as a failure or success.

## Nested comparisons

Every first-break attempt is classified without changing definitions:

1. bare break;
2. failed break/reclaim without order-flow qualification;
3. absorption only (S1);
4. absorption + opposite dominance + reclaim (S1-S2);
5. full failed auction (S1-S3).

The primary question is whether each added confirmation improves *net mean
expectancy* over the preceding nested group and over matched bare-break
controls. Win rate alone cannot establish an edge.

## Execution and outcomes

No fill may precede its decision timestamp.

- Reversal direction is `-s`.
- Entry is the first observable ask after the decision for a long and the first
  observable bid after the decision for a short.
- Time exits cross the opposite side of the BBO at 2, 5, and 15 minutes after
  entry. The frozen primary horizon is 2 minutes; 5 and 15 minutes measure
  decay and path, not replacement primaries.
- Commission is reported explicitly and crossing cost comes from observed BBO.
- Slippage stress is 0, 1, 2, and 4 additional ticks per side.
- MFE and MAE use executable-side prices where available.
- A structural stop is one tick beyond the failed-auction extreme. Report the
  first stop or 1R target, the first stop or 2R target, and ambiguous ordering.
  Do not discard stopped trades that later reverse.

POC and opposite-value targets are not part of Phase 1 because exact
point-in-time profiles are not yet in the data contract.

## Selection and controls

- Discovery: the already inspected May-August 2026 feature period. It may be
  used to debug extraction and verify event frequency, never to pass a gate.
- Validation and confirmation dates are selected before their MBP-1 outcomes
  are loaded, using only calendar date, pre-open gap, prior volatility, and
  level availability.
- Controls are matched on date block, absolute gap bucket, prior 20-day
  volatility bucket, level type, break side, and five-minute time bucket.
- One event cluster per level attempt is the unit of analysis. Confidence
  intervals resample whole sessions.
- Report large-gap and ordinary-gap strata separately; neither may be hidden
  by pooling.

## Falsification and robustness

The implementation must include:

1. timestamp audit proving all features precede entry;
2. future-flow placebo that must not create a stronger result;
3. within-session block permutation of order-flow features;
4. matched bare-break controls;
5. session-cluster bootstrap confidence intervals;
6. leave-one-session-out and leave-one-week-out stability;
7. delayed entry by 5, 10, and 30 seconds;
8. the frozen slippage grid;
9. false-discovery correction across nested definitions, sides, level types,
   gap strata, and secondary horizons;
10. separate results by month, side, level type, and event-time bucket.

## Minimum evidence and gates

The full-sequence rule cannot pass unless the untouched sample contains at
least 30 full-sequence events across at least 20 independent sessions. A result
with fewer observations is `INSUFFICIENT_EVIDENCE`, regardless of profitability.

For both validation and later untouched confirmation, all are required:

- positive mean and median net expectancy at the two-minute primary horizon
  after observed crossing, commission, and one extra tick per side;
- session-bootstrap 95% interval for mean net expectancy above zero;
- positive incremental expectancy versus matched failed-break controls;
- no dependence on one day or week;
- no sign reversal under a ten-second delayed entry;
- future-flow placebo and block permutation do not explain the result; and
- results remain directionally consistent across at least two calendar blocks.

Failure of any gate keeps the strategy out of Tier 3. Passing this research
gate would authorize only a separate shadow-forward test, not trading.

## Acquisition boundary

Before any Databento purchase, request an exact metadata cost estimate for the
preselected date windows. Start with the smallest untouched block capable of
testing event frequency. Do not purchase multi-year full-session MBP-1 when
targeted 09:20-10:35 ET windows can answer the question. Pause for owner review
if the quote is unexpectedly large.
