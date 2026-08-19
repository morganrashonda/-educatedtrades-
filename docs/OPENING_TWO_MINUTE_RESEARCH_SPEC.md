# Two-minute cash-open prediction study

Status: pre-registered, research-only. This study cannot place orders, update
learning tables, or alter production configuration.

## Question

At 09:29:00 America/New_York, can information already observable in NQ and QQQ
predict the direction and economically tradable size of the NQ move from the
09:30 bar open through the 09:31 bar close (the first two cash-session minutes)?

The study distinguishes three claims:

1. **Association:** a feature and the later move co-vary.
2. **Prediction:** a rule formed without future data remains useful in later
   chronological periods.
3. **Causation:** a measured market mechanism, such as Nasdaq opening-auction
   imbalance, transmits price pressure into NQ. Price bars alone cannot prove
   this third claim.

## Timestamp and outcome contract

- Decision timestamp: 09:29:00 ET.
- Latest permitted one-minute bar: 09:28 ET, whose close is known at 09:29.
- Entry proxy: NQ 09:30 bar open.
- Exit proxy: NQ 09:31 bar close.
- Primary outcome: signed NQ points over that interval.
- Directional tie: an exactly zero move is neither a win nor a loss.
- One observation per cash session; no overlapping seconds are treated as
  independent samples.

The minute-bar entry is a feasibility proxy, not an executable fill claim.
Any surviving candidate requires event-level replay at the 09:30 boundary.

## Frozen predictors

All predictors end at 09:28 ET:

- NQ and QQQ returns over the last 5 and 30 minutes;
- NQ and QQQ return from the prior regular-session close;
- NQ and QQQ location within their observed overnight/premarket range;
- NQ and QQQ overnight/premarket range as a percentage of prior close;
- NQ-minus-QQQ divergence for the 5-minute and overnight returns;
- prior NQ regular-session return and range;
- QQQ premarket volume relative to the median of the preceding 20 sessions.

No candlestick name is an input. Bodies, wicks, and named patterns may be
reported later only if they add information beyond the frozen continuous
features in a separately pre-registered test.

## Frozen deterministic rules

1. NQ 5-minute continuation.
2. QQQ 5-minute continuation applied to NQ.
3. NQ/QQQ 5-minute agreement; abstain on disagreement.
4. NQ overnight continuation.
5. NQ/QQQ overnight agreement; abstain on disagreement.
6. Overnight-extreme breakout: continue only when NQ is in the outer 20% of
   its overnight range and the 5-minute move points farther outward.
7. Overnight-extreme rejection: fade only when NQ is in the outer 20% and the
   5-minute move points back inward.

All seven are a disclosed hypothesis family. A favorable isolated rule is not
accepted without multiplicity-aware interpretation and later-period stability.

## Chronological evaluation

Sessions are sorted and divided into three contiguous blocks of equal size:
discovery, validation, and retrospective confirmation. Because related opening
research has already examined portions of these dates, the final block is not
called an untouched holdout. Only future shadow sessions can provide a truly
untouched confirmation.

A fixed, L2-regularized linear probability score is also fit on the discovery
block using the frozen predictors. Its feature scaling and coefficients are
then frozen for validation and retrospective confirmation. It is diagnostic;
it does not replace the deterministic rules or authorize execution.

## Economic and statistical reporting

For every rule and period, report:

- sessions, trades, abstentions, direction accuracy, and Wilson interval;
- gross mean/median NQ points, profit factor, maximum drawdown, and worst trade;
- day-bootstrap 95% interval for mean points;
- net results under 0.5, 1.0, 2.0, and 3.0 NQ-point round-trip all-in costs;
- dollars per trade and total dollars at $20/point for NQ and $2/point for MNQ;
- calendar-block stability, with the most recent 12 months shown separately.

The cost deduction is applied once per round trip. Broker-specific commissions
must be added to slippage/spread to select the applicable all-in scenario.

## Acceptance gate

A candidate is not economically credible unless validation and retrospective
confirmation both have:

- positive mean after at least 1.0 NQ point of all-in round-trip cost;
- bootstrap lower bound above zero, or a future sample plan explicitly labels
  the result provisional;
- profit factor above 1.10;
- no dependence on one month or one extreme session;
- at least 100 independent sessions for an always-on rule, or 50 trades for an
  abstaining rule;
- the same direction of effect in the most recent 12 months.

No result from this study enables Tier 3, learning, paper execution, or live
execution. Promotion requires event-level replay, future shadow testing,
independent review, and explicit owner authorization.
