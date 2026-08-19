# Untouched ES confirmation of the NQ opening-gap direction

Status: frozen before requesting or loading ES outcomes. Research only. This
document does not authorize production signals, Tier 3, learning, or orders.

## Purpose

Test whether the previously observed NQ two-minute gap-fade direction transfers
to a different equity-index future without changing the rule. ES has not been
loaded, inspected, or analyzed anywhere in the current workspace.

This is a direction-transfer test, not a claim that ES and NQ are identical or
that an ES bar result proves executable NQ profit.

## Data contract

- Dataset: Databento `GLBX.MDP3`.
- Symbol: `ES.v.0`, volume-based continuous front contract.
- Input symbology: `continuous`.
- Schema: `ohlcv-1m`.
- Requested period: 2023-08-17 through 2026-08-17 inclusive.
- Preserve actual `instrument_id` on every bar.
- Exclude a session when the 09:28 contract differs from the prior 15:59
  contract. Never bridge a continuous-contract roll as an overnight gap.
- Decision information ends with the 09:28 ET bar.
- Outcome is 09:30 one-minute open through 09:31 one-minute close.

## Inherited rule

For each roll-clean session:

1. calculate the 09:28 ES displacement from the prior 15:59 cash-session
   close;
2. require absolute displacement of at least 1.00%;
3. calculate close-to-close volatility from only the previous 20 valid
   sessions;
4. divide absolute displacement by that volatility;
5. inherit the NQ threshold `1.170437` unchanged;
6. trade direction is opposite the overnight displacement;
7. force the measurement exit at the end of 09:31.

No ES threshold, macro filter, day filter, stop, target, absorption feature, or
alternative horizon may be selected after seeing ES outcomes.

## Primary confirmation gates

The primary high-normalized-gap sample is `PASS` only if all are true:

- at least 30 observations;
- positive gross mean;
- day-bootstrap 95% lower bound above zero;
- same-day random-direction one-sided probability below 0.05;
- positive mean after a conservative one ES-point cost;
- positive mean after deleting the best observation;
- positive gross mean in both chronological halves.

Fewer than 30 observations is `INSUFFICIENT_EVIDENCE`, not failure. Any other
miss is `FAIL`. The broader absolute-gap-at-least-1% sample is secondary and
cannot override the primary decision.

Random direction and blocked calendar-quarter/prior-volatility permutation use
the identical ES days and realized two-minute return magnitudes. The analysis
uses 50,000 deterministic resamples.

## Interpretation

A pass would justify acquiring broader exact NQ quote/order-flow coverage and
specifying a delayed-entry acceptance/rejection test. It would not establish a
tradable edge because OHLCV opening prices are not guaranteed executable BBO
fills. A fail stops promotion of the current gap-direction hypothesis. No
result from this study directly changes the bot.
