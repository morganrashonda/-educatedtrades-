# Pattern cold-start repair

Main 5 previously required learned pattern conviction before a trend trade
could execute, while pattern outcomes were recorded only after executed
trades. An empty pattern database therefore stayed empty indefinitely.

The repair keeps unproven signals away from normal-size execution:

1. Each symbol is classified from its own ADX. The cross-symbol average is
   dashboard telemetry only.
2. The current and previous EMA values from one closed-bar fetch define the
   pattern. This prevents a second fetch from crossing a bar boundary and
   makes real EMA crosses identifiable.
3. Raw price candidates at absolute conviction 0.20 or greater are written to
   `shadow_forward.db`, never to `patterns.db`. A shadow enters at the next
   completed bar's open, includes slippage and round-trip costs, uses a
   conservative stop-first rule when OHLC cannot reveal intrabar order, and
   charges an adverse opening gap through a stop.
4. Promotion is scoped to the exact pattern hash, side, strategy and regime.
   It requires at least 100 completed operationally eligible shadows over 20
   distinct entry days, positive net expectancy, profit factor above 1.0 and
   a positive 95% moving-block-bootstrap lower bound.
5. A promoted candidate may buy exactly one share in an Alpaca paper account.
   It is long-only, limited to two entries per day, requires a flat account,
   and still passes startup recovery, preflight, market-hours, health, kill,
   exposure, cooldown, sizing and execution-quality gates. It cannot run in
   simulation or live-money environments.
6. A broker-confirmed exploration fill is recorded in pattern memory under
   `shadow_promoted_exploration`. Normal strategy execution requires at least
   20 resolved broker-confirmed outcomes for that pattern plus the existing
   corrected-conviction threshold. An open, unresolved record is not evidence.

Read-only progress is exposed at `/api/shadow-forward` and inside
`/api/gate-status`. Per-cycle and cumulative refusal reasons distinguish “no
edge qualified” from a broken data or execution path.
