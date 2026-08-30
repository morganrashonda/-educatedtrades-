# Accepted-Break Shadow-Forward Specification

Status: frozen, research-only observation. This document authorizes no Tier 3
integration, learning writes, paper orders, live orders, account access,
position access, or changes to the running coordinator.

Frozen: 2026-08-19 after inspecting historical sessions through 2026-08-14.
The first eligible untouched session is 2026-08-20.

## Purpose and selection disclosure

Test whether an accepted NQ level break contains repeatable short-horizon
directional information on sessions not used to select it. The candidate was
selected from a historical grid. Its apparent historical result is therefore
discovery evidence, not an unbiased performance estimate.

The forward observer measures a prediction. It is not yet an executable
multi-signal strategy: overlapping accepted levels remain separate correlated
observations and no equity curve, position stacking, or capital return is
claimed.

## Frozen map

For each eligible NQ cash opening, reconstruct only information known at the
relevant eligibility time:

- prior NQ 09:30-15:59 ET high and low;
- overnight NQ high and low from 18:00 through 09:29:59 ET;
- 09:00-09:29:59 ET premarket high and low;
- prior completed ISO week's NQ 09:30-15:59 ET high and low;
- first 60 seconds' opening-range high and low, eligible at 09:31:00 ET.

The source is Databento `GLBX.MDP3`, continuous symbol `NQ.v.0`, while every
row retains its actual instrument ID. A level is excluded on an instrument
mismatch. Levels with the same eligibility time and within four NQ ticks are
clustered exactly as in the frozen historical observer.

The OHLCV map request begins 35 calendar days before the candidate session.
This preserves enough ordinary cash sessions to calculate the frozen prior-20
session context without affecting the candidate classification.

## Frozen attempt and candidate

- Tick size: 0.25 NQ points.
- Evidence window: 09:28:00-09:36:00 ET.
- Upward attempt: a completed one-second MBP state crosses from below to at
  least one tick above an eligible high-type level.
- Downward attempt: exact directional mirror for a low-type level.
- The break becomes observable only when that one-second bucket ends.
- Decision: exactly 30 seconds after the break becomes observable.
- Features: half-open interval from the observable break through, but not
  including, the decision timestamp.
- The measurement schema records, without changing the frozen candidate rule:
  trade count and buy/sell volume; price progress, range, distance extremes,
  and realized movement; directional effort and progress per contract; signed
  trade imbalance; OFI and depth-normalized OFI; additions/removals and their
  imbalances; top-of-book same-side and opposing-side refill proxies; queue,
  spread, depth, and microprice levels plus their start/end changes; outside
  fraction; and the exclusive feature cutoff timestamp.
- These are top-of-book measurements only. Refill fields are explicitly labeled
  proxies and are not interpreted as hidden-liquidity or order-identity proof.
- `accepted_break`: price remains at least one tick beyond the level,
  aggressive-trade imbalance agrees with the attempted direction, and
  depth-normalized OFI agrees with it.
- Every failed, unresolved, missing-evidence, and no-attempt state is retained
  as an abstention. No threshold may be added during collection.

The frozen predicted direction for an accepted break is continuation. Failed
break reversal is not a candidate in this run.

## Frozen outcome and costs

- Entry: first valid NQ BBO at or after the 30-second decision and no more than
  two seconds late.
- Exit: first valid NQ BBO at or after 30 seconds following entry decision and
  no more than two seconds late.
- Longs enter at ask and liquidate at bid; shorts enter at bid and liquidate at
  ask.
- Primary net result includes the observed spread crossing plus one additional
  NQ tick at entry and one at exit: 0.50 NQ points total stress.
- MFE, MAE, and elapsed inside/outside/boundary dwell are retained.
- Missing or late quotes produce an explicit outcome refusal. Midpoints or
  bars may not substitute for executable quotes.

## Forward boundary and daily source gate

- No session before 2026-08-20 may enter the ledger.
- Same-day collection may not begin before 16:20 ET.
- A weekday wake-up is not proof of a cash session. The Alpaca calendar is
  consulted only to identify whether the U.S. cash market held a session; it
  supplies no signal or outcome input.
- Databento OHLCV map evidence, MBP-1 evidence, and BBO-1s evidence must all be
  complete. Partial source success is a retained `REFUSED_*` attempt and may be
  explicitly retried; it is never a no-signal day.
- Complete sessions are immutable and idempotent.

## Paid-data and disk boundaries

Each run estimates every request before retrieval. The fixed request set is one
bounded OHLCV map window and the 09:28-09:36 ET MBP-1 and BBO-1s windows.

- maximum three paid Databento requests per run;
- maximum cumulative estimate: $1.00 per session;
- maximum streamed response: 256 MiB per request;
- minimum free disk after worst-case processing: 1 GiB;
- compact event-level derived evidence, map inputs, and source SHA-256 values
  are retained; credentials and authorization headers are never persisted.

Exceeding any bound refuses the session without inventing evidence.

## Ledger and review

Evidence is stored in a dedicated SQLite database under the research data
directory. The attempt ledger is append-only. The materialized session row may
advance from a refusal to `COMPLETE`, but a complete result cannot change.

The status report must show:

- every eligible date and source refusal;
- sessions with no attempts and sessions with attempts but no accepted break;
- every accepted event, abstention, level family, side, and timestamp;
- observation-weighted and session-clustered primary net results;
- yearly/monthly ordering as the sample grows;
- contract hash and exact source provenance.

Initial review requires 60 complete cash sessions; stronger review requires
120. These counts do not create automatic promotion. Any threshold change,
level-family selection, side selection, position-overlap rule, paper trial, or
Tier 3 integration requires a new frozen specification, independent review,
and explicit owner authorization.

## Required tests

1. pre-boundary and pre-16:20 collection are refused;
2. exact-decision and future evidence cannot alter classification;
3. only accepted breaks become candidates;
4. every other attempt and no-attempt session is retained;
5. missing/late entry or exit quotes are never fabricated;
6. retries are idempotent and complete sessions are immutable;
7. the event ledger rejects update and delete;
8. cost, request-count, response-size, and free-disk bounds fail closed;
9. source and contract hashes are deterministic;
10. the module has no production, broker, order, account, position, learning,
    or Tier 3 dependency.
