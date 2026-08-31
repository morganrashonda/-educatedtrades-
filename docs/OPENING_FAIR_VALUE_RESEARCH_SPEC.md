# NQ/QQQ opening fair-value and gap-provenance study

Status: pre-registered before joining the new fair-value/path variables to the
two-minute outcomes. Research only; no orders, learning writes, or production
configuration changes.

## Primary question

Among roll-clean sessions where NQ is at least 1.00% from the prior cash close
at 09:29 ET, can we distinguish an information-confirmed gap from a temporary
futures-versus-cash dislocation before the 09:30 cash open?

Decision time is 09:29:00 ET. The latest NQ/QQQ minute bar permitted is 09:28.
The Nasdaq NOII input is the latest QQQ opening message whose capture-received
timestamp is at or before 09:29:00. The unchanged outcome is NQ 09:30 bar open
through 09:31 bar close.

## Rolling cash-implied NQ fair value

For each session `t`:

1. `qqq_indicative_return_t = qqq_near_clearing_price_0929 /
   qqq_prior_cash_close - 1`.
2. On only the previous 60 valid sessions, fit OLS:
   `nq_open_return = alpha + beta * qqq_indicative_return`.
3. Require at least 40 prior observations. Never include the current session in
   its own fit.
4. `expected_nq_open_return_t = alpha_t + beta_t *
   qqq_indicative_return_t`.
5. `fair_value_residual_t = nq_0928_return_from_prior_close -
   expected_nq_open_return_t`.

A positive residual means NQ is above the cash-implied opening relationship; a
negative residual means it is below. The frozen residual direction is opposite
the residual sign.

No official 09:30 opening price, later NOII message, current-session outcome,
or full-sample coefficient is allowed as an input.

## Gap-formation variables

NQ bars are divided in America/New_York time:

- prior cash close through 01:59;
- 02:00 through the 08:29 pre-announcement bar;
- the 08:30 one-minute bar;
- 08:31 through 08:59;
- 09:00 through 09:28.

Record:

- signed return for each segment;
- late confirmation: total-gap direction multiplied by the 09:00–09:28
  return; negative means late rejection;
- 08:30 contribution divided by absolute total displacement;
- path efficiency: absolute total displacement divided by cumulative absolute
  one-minute movement;
- largest non-overlapping five-minute move divided by absolute total
  displacement;
- minutes since the last overnight high for positive gaps or low for negative
  gaps.

Missing required bars exclude the session. Segments are not silently filled.

## Volatility normalization

Compute the standard deviation of NQ close-to-close cash-session returns from
only the previous 20 valid sessions. `normalized_gap` is absolute overnight
displacement divided by that prior-only volatility. Require 20 observations.

## Frozen comparisons

Within the unchanged absolute-gap-at-least-1.00% universe:

1. `base_gap_fade`: opposite the overnight displacement.
2. `fair_value_direction`: opposite the fair-value residual.
3. `fair_value_selective`: fair-value direction only when absolute residual is
   at least the median absolute residual in the discovery block.
4. `late_rejection_fade`: base fade only when late confirmation is negative.
5. `residual_and_rejection`: base fade only when late confirmation is negative
   and fair-value direction agrees with the base fade.
6. `high_normalized_gap_fade`: base fade only when normalized gap is at least
   the discovery-block median.

The two discovery medians are the only fitted thresholds and are frozen before
validation and retrospective confirmation.

## Validation and economics

After all availability filters, divide eligible sessions into three contiguous
equal blocks: discovery, validation, retrospective confirmation. Report trades,
win rate and Wilson interval, gross mean/median points, profit factor, maximum
drawdown, day-bootstrap mean interval, mean after 1.0-point all-in cost, and mean
after deleting the best trade.

A comparison is useful only if it improves on the base in both later blocks,
has at least 20 trades in each later block, remains positive after one-point
cost and best-trade deletion, and preserves direction in event-level overlap.

## Secondary macro attribution

Scheduled 08:30 release labels are added only after the primary results are
frozen. They may explain heterogeneity but cannot redefine the primary rule in
this study. Sources must be official BLS, BEA, Census, or Federal Reserve
release calendars; missing labels remain unknown rather than assumed absent.

Any survivor remains a shadow-only candidate because related historical data
has already been inspected. Future sessions are the only untouched sample.
