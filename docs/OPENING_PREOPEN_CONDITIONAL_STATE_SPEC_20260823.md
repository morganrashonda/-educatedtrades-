# Main 5 pre-open conditional-state shadow observer

## Purpose

This is a separate, measurement-only observer for a specific hypothesis:
large pre-open aggressive trade-flow imbalance, when aligned with pre-open
price direction, may improve the next two-minute continuation markout.

It does not place orders, change production state, write learning records,
enable Tier 3, or provide a signal to the trading coordinator.

## Frozen contract

- Dataset: `GLBX.MDP3`, `NQ.v.0`, continuous mapping.
- Eligible sessions: cash sessions on or after 2026-08-24.
- Evidence window: 09:00:00–09:30:00 ET, exactly 1,800 contiguous one-second
  MBP-1 states for one instrument ID.
- Entry: first valid BBO at or after 09:30:00 ET and no more than two seconds
  late.
- Outcome: first valid BBO at or after 09:32:00 ET and no more than two seconds
  late.
- Candidate state: flow sign equals pre-open price-return sign and absolute
  signed trade-flow score is at least 0.10.
- Baseline: same executable two-minute markout for every nonzero flow-sign
  session, regardless of alignment or threshold.
- Cost stress: subtract 2.25 NQ points from every executable outcome.
- Maximum: two data requests, $2.00 estimated Databento cost, 256 MiB response
  cap, and a one-GiB free-space reserve.

The candidate and baseline are descriptive measurements. No result is promoted
to a strategy from this ledger. Review gates are 20 complete sessions for data
quality, 60 for the first performance review, and 120 for stronger
confirmation.

## Why this state is worth testing

The exploratory historical split found a promising but uncertain conditional
state: high-magnitude pre-open flow aligned with pre-open price direction. It
was not accepted as proof because the held-out sample was small and the
cluster bootstrap interval crossed zero. This observer tests the state forward
on unseen sessions with executable bid/ask marks and a fixed adverse-cost
stress, while retaining the all-flow baseline as the falsification control.

## Integrity rules

- Missing seconds, multiple instrument IDs, late/missing executable quotes, or
  invalid source data cause abstention rather than imputation.
- Session records are immutable once complete; source events are append-only.
- The observer is isolated from `backend/main.py`, broker adapters, order
  submission, position state, learning, and Tier 3.
- `--check` is local and read-only; it never contacts Alpaca or Databento.
