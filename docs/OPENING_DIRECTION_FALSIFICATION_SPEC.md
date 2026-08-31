# Opening direction-versus-volatility falsification

Status: frozen retrospective falsification plan. Research only. This document
does not authorize production signals, Tier 3, learning writes, or orders.

## Question

Does the sign of the NQ overnight gap contain information about the 09:30 open
through 09:31 close return, or did prior research merely select sessions with
large direction-agnostic opening movement?

The existing 1.00% absolute-gap universe and discovery-fitted normalized-gap
threshold are inherited unchanged. No threshold is selected by this study.

## Frozen comparisons

On identical candidate days report:

1. fade the overnight gap;
2. continue in the overnight-gap direction;
3. always long and always short;
4. independent random direction applied to each observed two-minute return;
5. gap-direction permutation within calendar-quarter and prior-volatility
   tercile blocks;
6. absolute two-minute movement, which does not receive a trading direction.

Randomization tests use 50,000 deterministic draws and one-sided exceedance
probabilities for the observed fade mean. They are retrospective diagnostics,
not untouched p-values.

## Volatility control

Each candidate is matched without replacement to the nearest prior-only
20-session volatility observation in the same calendar year, restricted to an
ordinary session whose absolute overnight gap is below 1.00%. Compare absolute
two-minute movement with a paired day bootstrap. This comparison measures
whether candidate mornings simply move more; it does not create a control
trading strategy.

## Path and execution diagnostics

- Compute fade-direction MFE and MAE across the 09:30 and 09:31 minute bars.
- Minute bars cannot determine intrabar event order or guarantee fills.
- Where exact one-second BBO data overlap, cross the opening and exit spreads.
- Exact overlap still excludes commissions, latency and additional slippage.
- Report 0-, 1-, 2- and 3-point cost scenarios and results without the best
  and worst observations.

No stop or target is optimized. The inherited target remains a forced exit at
the end of 09:31. A later study may define an executable stop/target only after
direction survives this gate.

## Decision rule

Reject the directional hypothesis if the high-normalized-gap fade does not
beat same-day random direction and blocked direction permutation, or if its
apparent profit is explained by a few observations or erased by plausible
costs. Survival means only that a mechanism-qualified, delayed-entry study is
worth specifying; it is not an edge or deployment decision.
