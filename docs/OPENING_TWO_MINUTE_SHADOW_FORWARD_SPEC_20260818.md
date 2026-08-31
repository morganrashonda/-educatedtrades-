# NQ Two-Minute Gap-Fade Shadow-Forward Specification

Status: frozen research-only shadow observation. This document does not
authorize Tier 3, learning writes, paper orders, live orders, or modification
of the running coordinator.

Frozen: 2026-08-18 before any future session is recorded.

## Candidate

At 09:29:00 America/New_York:

1. read the prior regular-session 15:59 NQ close;
2. read the current session's 09:28 NQ close, which is complete at 09:29;
3. require the same actual futures instrument ID at both observations;
4. calculate `gap_pct = (09:28 close / prior 15:59 close - 1) * 100`;
5. signal only when `abs(gap_pct) > 1.278097837`;
6. fade the displacement: sell after a positive gap and buy after a negative
   gap;
7. observe an executable entry quote at 09:30:00 ET and an executable exit
   quote at 09:32:00 ET.

The threshold is the previously documented provisional threshold. It is strict
greater-than, not greater-than-or-equal. It cannot be changed during this
shadow run.

## Executable-price contract

- Long entry crosses the first valid ask at or after 09:30:00 ET.
- Short entry crosses the first valid bid at or after 09:30:00 ET.
- Long exit crosses the first valid bid at or after 09:32:00 ET.
- Short exit crosses the first valid ask at or after 09:32:00 ET.
- Quotes more than five seconds after either boundary are late and refused.
- Bid and ask must be finite, positive, and strictly ordered.
- Gross signed points already include the observed spread crossing.
- Additional round-trip slippage is reported at 0, 0.5, 1, 2, and 3 NQ
  points. Commission is reported separately and must not be hidden in a
  favorable scenario.

## Forward integrity

`live` observations count toward the forward gate only when:

- the decision is durably recorded from 09:29:00 through 09:29:59.999 ET;
- all source timestamps match the intended session and bars;
- the instrument IDs match across the overnight boundary;
- the entry and exit quotes meet the boundary/latency contract; and
- every stage is written before the next stage is accepted.

`historical_replay` observations are allowed for software verification but are
permanently ineligible for forward evidence.

Every cash session must end in one of these auditable states:

- `NO_SIGNAL`;
- `SIGNAL_AWAITING_ENTRY`;
- `SIGNAL_OPEN`;
- `COMPLETE`;
- `REFUSED_DECISION`, `REFUSED_ENTRY`, or `REFUSED_EXIT`.

Missing data, roll transitions, late quotes, invalid quotes, and source errors
are refusals, not silently dropped days.

## Durability and isolation

- Store data in a dedicated SQLite database outside `patterns.db` and outside
  the execution ledger.
- Keep an append-only event table plus a materialized session record.
- Identical retries are idempotent; conflicting retries are rejected.
- One session date is one independent observation.
- The module must not import `main`, `trading`, `execution_safety`, `patterns`,
  or any broker SDK.
- The schema contains no order ID, order quantity, account, or broker-submit
  field.

## Review gates

Initial review: 30 eligible completed future signals.

Stronger decision gate: 60 eligible completed future signals. Promotion to a
separately authorized paper trial requires all of:

- positive mean and median after observed crossing plus one NQ point of
  additional round-trip cost;
- session-bootstrap 95% lower bound above zero at that cost;
- profit factor above 1.10;
- no dependence on one session or one calendar month;
- acceptable 5- and 10-second delayed-entry diagnostics collected separately;
- complete refusal/no-signal accounting; and
- independent review followed by explicit owner authorization.

No number produced by this store enables execution automatically.
