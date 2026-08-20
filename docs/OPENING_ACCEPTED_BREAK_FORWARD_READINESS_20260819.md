# Accepted-break forward observer readiness

Status: implementation and validation complete in the isolated research
branch. The observer is not committed, merged, deployed, or installed as a
LaunchAgent. Main 5 production remains unchanged.

## Frozen candidate

- first eligible untouched session: 2026-08-20;
- 30-second decision after an observed NQ level break;
- accepted-break continuation only;
- 30-second executable-BBO outcome;
- observed spread crossing plus 0.50 NQ points of additional round-trip
  stress;
- all failed, unresolved, missing-outcome, and no-attempt states retained;
- no overlapping-position or equity-curve claim;
- no headlines until a properly timestamped corpus exists;
- no execution, learning, or Tier 3 authorization.

Frozen contract SHA-256:
`02a71208172865892109b5f55ca1566c9f3b901e1758d85f7b672d0eb433399a`.

The measurement schema is `top_of_book_measurements_v2`. It adds timestamped,
direction-aware measurement fields for price path, trade effort, OFI,
additions/removals, refill proxies, queue, spread, depth, and microprice. It
does not change the accepted-break classification or authorize execution.

## Source and operational boundaries

The post-close observer retrieves three pre-costed Databento sources: a
35-calendar-day NQ OHLCV map, 09:28-09:36 ET MBP-1, and the same BBO-1s window.
All estimates are obtained before any paid range retrieval. The run refuses
above three requests, $1.00 estimated cost, 256 MiB per streamed response, or
when it cannot preserve 1 GiB of free disk.

The Alpaca connection is calendar-only. The implementation contains no
account, order, position, or broker method.

The proposed LaunchAgent wakes at 16:35 ET on weekdays, ten minutes after the
existing NQ/QQQ observer. The source schedule is intentionally not installed
from this unmerged worktree.

## Durability

- dedicated research SQLite database;
- append-only event table protected by update/delete refusal triggers;
- refusal-to-complete retries retain prior attempts;
- complete and no-cash-session records are immutable and idempotent;
- contract and source hashes are retained;
- source failure retains estimated cost/request metadata;
- deterministic month, level-family, side, observation-weighted,
  equal-signal-session, and session-cluster summaries;
- 60 complete sessions for initial review and 120 for stronger review;
- no automatic promotion at either count.

## Three-review findings

1. The historical observer initially needed optional decision/horizon inputs;
   a shadowed variable was caught by regression tests and corrected.
2. Forward BBO evidence now retains and checks actual instrument identity,
   rejects locked/crossed rows, and preserves paid-attempt metadata on source
   failure.
3. The map window was expanded to 35 calendar days for full prior-20-session
   context, and the final status report gained the promised deterministic
   session-cluster interval and calendar ordering.

## Exact parity check

A read-only replay of the audited 2026-08-14 session compared the historical
report with the forward evaluator. Both events matched exactly on level,
level names, side, observed break timestamp, 30-second classification, and
stressed 30-second outcome.

No paid data was requested for this parity check.

## Verification

- new observer/refactor targeted tests: 32/32 passed;
- all opening-research tests: 160/160 passed;
- legacy safety and integrity suite: 837/837 passed;
- end-to-end suite: 33/33 passed;
- remaining backend pytest collection: 250/250 passed;
- full backend gate: 1,120 passing checks, 0 failures;
- Python compilation, shell syntax, plist validation, no-network readiness,
  and forbidden-dependency scan passed.

## Remaining separate actions

1. commit and push the research branch;
2. review and merge it independently from production/runtime work;
3. deploy the merged research files to Main 5;
4. run the wrapper's no-network check from the deployed path;
5. install and load the LaunchAgent;
6. verify the first post-close record without changing the frozen contract.
