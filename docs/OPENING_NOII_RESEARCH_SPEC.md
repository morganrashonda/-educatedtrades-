# QQQ opening-auction imbalance mechanism test

Status: pre-registered after the price-only large-gap candidate was identified,
but before the purchased NOII outcomes were joined to NQ returns. Research only.

## Question

Does QQQ Nasdaq Opening Cross imbalance information available before 09:30 ET
explain or improve the NQ two-minute large-overnight-gap fade?

This is a mechanism test. The base candidate remains:

- observe at 09:29 ET;
- require absolute NQ displacement from the prior cash close of at least 1.00%;
- direction is opposite that displacement;
- NQ entry proxy is the 09:30 bar open;
- exit proxy is the 09:31 bar close.

## Raw-data contract

- dataset: Databento `XNAS.ITCH`;
- schema: `imbalance`, normalized from Nasdaq TotalView-ITCH NOII;
- symbol: QQQ only;
- auction type: `O` only;
- timestamp: `ts_recv`, so exchange-to-capture arrival is respected;
- range: 2023-08-17 through 2026-08-17;
- raw file is immutable after record-count and schema validation.

Closing (`C`) and extended (`A`) auctions are excluded. Missing opening snapshots
are reported and never filled.

## Frozen snapshots

For each cash session, select the latest received opening message at or before:

1. 09:29:00 ET (`snapshot_2900`);
2. 09:29:50 ET (`snapshot_2950`).

Both must be at or after 09:28:00 ET. The second timestamp is a separate
decision design, not a replacement for the first.

## Frozen variables

For each snapshot:

- signed imbalance ratio: bid imbalance is positive, ask imbalance negative,
  no imbalance zero; quantity divided by `max(paired_qty, 1)`;
- signed near-price displacement in basis points:
  `(cont_book_clr_price / ref_price - 1) * 10,000`;
- log paired shares and log total imbalance shares;
- fade-aligned imbalance: signed ratio multiplied by the frozen fade direction;
- fade-aligned near displacement: near-price displacement multiplied by fade
  direction.

Changes are snapshot 09:29:50 minus snapshot 09:29:00. No 09:30 cross price,
statistics record, or later message is permitted as an input.

## Frozen deterministic comparisons

Each is compared with the unchanged 1.00% base fade:

1. **Imbalance support:** trade only when fade-aligned imbalance is positive.
2. **Near-price support:** trade only when fade-aligned near displacement is
   positive.
3. **Dual support:** both quantities are positive.
4. **No opposition:** neither quantity is negative.

The four comparisons are run independently at 09:29:00 and 09:29:50. Zero is
not silently treated as positive.

## Diagnostic model

A fixed L2-regularized directional score is fit only on the first chronological
third of large-gap sessions using:

- fade-aligned imbalance and near-price displacement at both snapshots;
- their changes;
- log paired and imbalance quantities;
- absolute overnight displacement.

Scaling and coefficients are frozen before evaluation on the middle and final
thirds. The model is diagnostic and cannot authorize execution.

## Acceptance standard

NOII is useful only if at least one frozen comparison:

- improves mean net NQ points versus the base fade in both later chronological
  blocks after a 1.0-point all-in cost;
- does not obtain improvement solely by deleting losing sessions while leaving
  fewer than 20 trades per later block;
- has the same directional relationship in both later blocks;
- remains positive after deleting the single best trade in each block;
- improves or preserves the event-level result where overlapping data exist.

Because the base candidate and the NOII study were conceived using historical
data, even a pass remains provisional and requires future shadow confirmation.
