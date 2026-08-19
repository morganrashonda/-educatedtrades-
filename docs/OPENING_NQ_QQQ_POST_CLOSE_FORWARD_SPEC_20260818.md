# NQ Signal / QQQ Execution Post-Close Forward Specification

Status: frozen, research-only observation. This document does not authorize
Tier 3, learning writes, paper orders, live orders, or changes to the running
coordinator.

Frozen: 2026-08-18. The all-session baseline and qualifying-session order-flow
diagnostics were added on 2026-08-18, still before the first eligible cash
session. The first eligible cash session is 2026-08-19.

## Purpose

Measure the already frozen two-minute NQ opening-gap fade on future sessions,
using QQQ SIP quotes as the executable vehicle. Earlier NQ and QQQ history has
already been inspected and is not untouched confirmation. A session retrieved
after its close still counts as forward evidence only when its date is on or
after 2026-08-19, because the complete rule was frozen before that session.

## Fixed signal

- Prior reference: the immediately preceding cash session reported by Alpaca's
  market calendar, using that session's NQ 15:59 ET one-minute close. The
  observer may cross weekends and full-market holidays, but it may not skip an
  early or unscheduled closure to use an older close. If the immediately
  preceding session did not close at 16:00 ET, the candidate is refused because
  the frozen 15:59 reference does not exist.
- Decision value: the candidate session's completed 09:28 ET NQ one-minute
  close.
- Both bars must resolve to the same actual futures instrument ID. A roll
  mismatch refuses the session.
- `gap_pct = (decision_close / prior_close - 1) * 100`.
- Trigger only when `abs(gap_pct) > 1.278097837`. Equality is no signal.
- Positive gap means short QQQ; negative gap means long QQQ.
- No candle shape, QQQ price, news, macro label, order flow, or later price may
  alter the trigger or direction.

## Fixed QQQ quote contract

- Source: Alpaca historical stock quotes, symbol QQQ, `feed=sip`.
- Marks: 09:30:01, 09:30:06, 09:30:11, and 09:32:01 ET.
- At each mark use the first valid quote at or after the mark and no more than
  two seconds late. Invalid rows may be skipped only in timestamp order inside
  that same window.
- A quote must have positive finite prices and sizes with `bid < ask`.
- Long entry crosses the ask and exits at the bid. Short entry crosses the bid
  and exits at the ask.
- The primary result subtracts an additional $0.02 per share round trip after
  the observed spread crossing. The 5- and 10-second entries are diagnostics.
- Missing, malformed, locked, crossed, or late quotes refuse the signal session.
  No trade, midpoint, or bar substitution is permitted.

## All-session QQQ null baseline

The same four QQQ SIP marks are collected for every valid cash session after
the forward boundary, including days where the NQ gap does not trigger. This
baseline cannot create or change a signal.

For every session report:

- executable long return: entry ask to exit bid;
- executable short return: entry bid to exit ask;
- both gross returns and returns after the same additional $0.02 per-share
  primary slippage used by the qualifying signal;
- entry and exit spreads;
- entry-to-exit midpoint change; and
- the same five- and ten-second delayed executable returns in both directions.

A non-signal day with missing QQQ marks is `REFUSED_QQQ_SOURCE`, not silently
counted as `NO_SIGNAL`. The baseline answers whether the selected large-gap
days outperform ordinary openings and exposes how much unconditional movement
and spread crossing explain the result. Baseline results must not be searched
for a replacement direction or threshold during this frozen run.

## Qualifying-session NQ order-flow diagnostics

Only sessions that satisfy the unchanged NQ gap trigger request Databento
`GLBX.MDP3`, `NQ.v.0`, `mbp-1` data from 09:29:55 through 09:32:06 ET. The
actual instrument ID must match the 09:28 signal bar. Raw data is compressed
locally with its uncompressed SHA-256 provenance hash.

The existing validated one-second MBP-1 extractor produces three fixed,
non-overlapping-lookahead summaries beginning at 09:30:01 ET:

- first 10 seconds: `[09:30:01, 09:30:11)`;
- first 30 seconds: `[09:30:01, 09:30:31)`; and
- primary horizon: `[09:30:01, 09:32:01)`.

Each summary records event/trade counts, aggressive buy and sell volume,
signed trade imbalance, OFI, mean depth, depth-normalized OFI, queue imbalance,
spread, microprice displacement, gap-direction aggressive effort, price
progress per aggressive contract, gap extension, reversal from the extreme,
and the same quantities signed toward the frozen fade direction.

These are explanatory measurements only. There is no absorption threshold,
order-flow filter, score, confirmation requirement, or veto. Missing or
over-budget order flow is retained as a diagnostic refusal while the QQQ
primary result remains complete and unchanged.

## Collection boundary

- The collector accepts no session before 2026-08-19.
- A same-day collection cannot start before 16:20 ET, safely after the SIP
  historical-data delay.
- The session date is explicit. The collector does not automatically sweep old
  dates and cannot import pre-freeze history.
- Historical requests are minimized to one Alpaca calendar lookup, two exact NQ
  one-minute marks, four two-second QQQ quote windows, and, only for a
  qualifying signal, one 131-second NQ MBP-1 window.
- Every Databento request receives a cost preflight. One run has a hard $0.01
  bar-data cap and a ten-request ceiling. A qualifying session's optional
  MBP-1 diagnostic has a separate hard $0.50 estimate cap and a 64 MiB
  uncompressed download cap. Before downloading or decompressing MBP-1, the
  collector must also be able to preserve at least 256 MiB of free disk after
  the worst permitted working file. Exceeding any diagnostic cap cannot
  suppress or alter the primary QQQ outcome.
- Every attempted eligible session ends as `NO_SIGNAL`, `COMPLETE`, or an
  explicit `REFUSED_*` state. Source failures are retained and may be retried;
  a completed or no-signal result is immutable.

## Durability and isolation

- Evidence is stored in a dedicated SQLite database outside `patterns.db`, the
  order ledger, and all production state.
- The event ledger is append-only. A materialized session row may move from a
  documented refusal to a terminal result, but the failed attempt remains in
  the ledger.
- Identical retries are idempotent. Conflicting terminal results are rejected.
- Credentials and raw authorization headers are never persisted.
- The module has no production, broker, order, account, position, learning, or
  Tier 3 import or execution path.

## Review boundary

Report all post-freeze sessions, including no-signal and refused days. Initial
review remains 30 completed qualifying signals; stronger review remains 60.
Any paper trial requires a separate specification, independent review, and
explicit owner authorization. No result produced by this observer can enable
execution automatically.
