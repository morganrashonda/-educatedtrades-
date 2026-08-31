# Opening direction-versus-volatility findings

Date: 2026-08-18

Status: retrospective research result. No production signal, Tier 3 change,
learning write, order, commit, or push is authorized by this finding.

## Decision

The historical gap-fade direction survives the narrow direction-versus-
volatility falsification gate. It does **not** establish an executable edge.

On the identical high-normalized-gap sessions, randomizing the trading side
while preserving every observed two-minute return magnitude did not reproduce
the fade mean. The result therefore cannot be dismissed solely as selecting
high-volatility mornings. However, the candidate was found through prior
search, the randomization probabilities are not search-adjusted, and exact BBO
execution overlap is too small and outlier-dependent for a trading claim.

## Frozen data

- 702 roll-clean NQ/QQQ sessions: 2023-08-17 through 2026-08-17.
- 109 inherited absolute-gap-at-least-1.00% sessions.
- 49 inherited high-normalized-gap sessions using the unchanged discovery
  threshold `1.170437...`.
- 109/109 candidate sessions had complete 09:30 and 09:31 NQ minute bars.
- Reconstructed prior-only volatility matched the original value exactly for
  all 109 sessions; maximum absolute difference was `0.0`.
- Exact one-second BBO overlap: 21/109 and only 7/49.

## Direction-versus-magnitude results

| Sample | Trades | Fade wins | Fade mean | 95% day bootstrap | Mean after 1 point | Mean without best |
|---|---:|---:|---:|---:|---:|---:|
| Absolute gap >=1% | 109 | 64 (58.7%) | +11.12 | +3.82 to +18.56 | +10.12 | +10.03 |
| High normalized gap | 49 | 34 (69.4%) | +23.47 | +12.27 to +35.01 | +22.47 | +21.27 |

For the 49-session sample:

- mean absolute two-minute movement was 35.82 points;
- the fade mean was 65.5% of that direction-agnostic magnitude;
- independent random side on the same 49 returns had a 95% null interval of
  -13.12 to +13.17 points;
- the observed +23.47 fade mean had a retrospective one-sided randomization
  probability of `0.00018`;
- calendar-quarter/prior-volatility-blocked direction permutation had a null
  interval of -3.39 to +18.96 and probability `0.00146`.

The blocked result is less secure than the raw number suggests: only 8 of 23
blocks contained both gap directions and could meaningfully permute direction.
Neither probability corrects for the broader indicator and variant search that
preceded this test. They are falsification diagnostics, not confirmatory
p-values.

Always-long and always-short controls crossed zero. On the 49 days, always
long averaged +4.79 points and always short -4.79, compared with +23.47 for
the gap fade.

## Volatility-matched ordinary sessions

The 49 candidates were matched without replacement to ordinary sessions in
the same calendar year with the nearest prior-only 20-session volatility:

- candidate prior volatility: 1.257%;
- control prior volatility: 1.241%;
- candidate mean absolute two-minute move: 35.82 points;
- control mean absolute two-minute move: 29.13 points;
- paired difference: +6.69 points;
- paired bootstrap interval: -3.89 to +17.36 points.

The interval crosses zero. Candidate mornings were descriptively larger, but
the matched comparison does not establish that they have greater absolute
movement. More importantly, same-day side randomization already preserves the
candidate days' exact realized magnitudes and still fails to explain the
observed direction result.

## Path risk

Across the 49 high-normalized-gap minute-bar paths:

- mean MFE in the fade direction: 42.60 points;
- median MFE: 33.25 points;
- mean MAE: 29.09 points;
- median MAE: 23.25 points.

The favorable path is real historically, but so is substantial adverse
movement. Minute bars cannot determine whether the high or low occurred first,
and no stop or target was optimized. These are path measurements, not a
backtest of an executable risk policy.

## Exact BBO overlap

The 49-session candidate had only seven sessions with exact one-second BBO
entry and exit coverage:

- 4/7 positive after crossing the observed opening and exit spreads;
- +14.16 points at exact mid;
- mean estimated top-of-book crossing cost: 1.56 points;
- +12.60 points after crossing;
- only +2.57 points after crossing and deleting the best session.

This subset excludes commissions, latency and additional slippage. Seven
observations, four wins, and the best-trade sensitivity are insufficient for
an execution claim.

## Chronological description

The inherited 49 sessions remained positive in each chronological third:

| Block | Trades | Wins | Gross mean | Bootstrap interval |
|---|---:|---:|---:|---:|
| Discovery | 16 | 12 | +15.75 | +3.91 to +28.31 |
| Validation | 16 | 11 | +35.44 | +10.92 to +59.98 |
| Retrospective confirmation | 17 | 11 | +19.47 | +0.51 to +39.22 |

These labels describe chronology only. Prior exploratory work means none of
the combined 49 sessions should now be advertised as a pristine holdout.

## What the result permits

This result permits specifying the next research gate: a mechanism-qualified,
delayed-entry acceptance/rejection test with executable timestamps. It does
not permit implementation in Tier 3 or autonomous trading.

Before any edge claim, the frozen direction must survive at least one genuinely
untouched test (such as ES or an uninspected NQ period), and historical NQ
candidate sessions need substantially broader exact quote/order-flow coverage.
Any absorption confirmation must enter only after it is observed; it cannot
receive the 09:30 opening price retroactively.
