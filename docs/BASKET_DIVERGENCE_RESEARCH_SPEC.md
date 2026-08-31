# SPY/QQQ/IWM relative-value divergence — research spec

Status: frozen before validation/confirmation were touched. Research only.
No production, Tier 3, learning, or order-path imports or authorization.

## Purpose

The README states SPY, QQQ and IWM correlate at roughly 0.9 and calls
trading all three "closer to one observation than to three" — framed purely
as a concentration **risk**. This is the first test in this repository of
whether that same near-redundancy carries a usable **signal**: when one of
the three baskets' daily RSI diverges sharply from its two peers, does it
subsequently converge back toward them (classic relative-value /
statistical-arbitrage mean reversion), independent of which way the market
moves that day overall?

This is mechanistically unrelated to every prior hypothesis in
`backend/research/` — all of those are NQ/QQQ opening-bell microstructure
studies. This is a cross-sectional, market-neutral question about the actual
six-symbol production universe, using only Alpaca daily bars already paid
for via the existing account. No new data purchase.

## Data

- Source: Alpaca historical daily bars, SPY/QQQ/IWM, 2016-01-04 through
  2026-08-26 (2,677 bars per symbol; 1,597/532/532-ish day discovery/
  validation/confirmation split by count, see findings for exact figures).
- Indicators: `patterns.compute_rsi` (the real production implementation),
  period 14, computed with no lookahead — each day's RSI uses only prices up
  to and including that day.

## Rule

For each day and each of the three symbols:

1. `relative_rsi = symbol_rsi - mean(the other two symbols' RSI, same day)`.
2. If `|relative_rsi|` clears a frozen threshold, the symbol is a **laggard**
   (relative_rsi very negative) or a **leader** (relative_rsi very positive).
3. `spread_return = symbol's forward return over N days − mean(the other two
   symbols' forward return over the same N days)` for a laggard (long the
   spread); the mirrored sign for a leader (short the spread).
4. A positive `spread_return` means the hypothesis called the direction of
   convergence correctly.

## Discovery-only parameter search

Grid: divergence threshold ∈ {5, 7.5, 10, 12.5, 15, 20} RSI points; horizon
∈ {1, 2, 3, 5, 10} trading days. Selection criterion: best mean spread return
(after a conservative 2bps cost stress) among combinations with a positive
day-block bootstrap 95% lower bound and at least 30 discovery-slice
observations. Whatever the search selects is frozen and applied unchanged to
validation and confirmation — neither of those slices is ever used to pick
or adjust a parameter.

## Confirmation gates (if a discovery candidate exists)

The combined validation+confirmation (out-of-sample) sample is `PASS` only
if all are true: ≥30 observations; positive gross mean; positive mean even
under the worst (10bps) cost scenario; day-block bootstrap 95% lower bound
above zero; both the validation block and the confirmation block
individually gross-positive; same-days random-direction one-sided
probability below 0.05; positive mean after deleting the single best
observation. Fewer than 30 out-of-sample observations is
`INSUFFICIENT_EVIDENCE`, not failure. If discovery itself produces no
candidate meeting its own bar, the result is `NO_DISCOVERY_CANDIDATE` and
validation/confirmation are never run at all — there is nothing frozen to
apply to them.

## Interpretation

A pass would justify measuring genuine intraday/executable pricing before
any promotion; it would not itself authorize a trade. A rejection at the
discovery stage — the actual outcome, see the paired findings doc — means
the hypothesized *direction* (divergence → convergence) is wrong, which is a
different and stronger conclusion than "no evidence either way." No result
from this study changes the bot, its patterns, or its order path.
