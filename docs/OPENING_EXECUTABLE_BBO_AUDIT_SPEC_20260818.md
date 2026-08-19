# NQ opening-gap executable BBO audit specification

Status: frozen retrospective execution audit. This document does not authorize
Tier 3, learning writes, paper orders, live orders, or changes to the running
coordinator.

Frozen: 2026-08-18 before downloading the qualifying-session BBO outcomes.

## Question

Does the previously frozen NQ large-gap fade remain positive when its 09:30 to
09:32 observation is priced with side-correct best bid and offer snapshots,
additional slippage, and explicit commission assumptions?

This is an execution audit of a previously inspected historical signal. It is
not a new untouched edge test, even though the BBO files were not previously
examined for most qualifying sessions.

## Frozen signal

- Source population: roll-clean NQ continuous-contract cash sessions from
  2023-08-17 through 2026-08-17.
- Decision information ends with the completed 09:28 ET minute bar.
- Gap: `(09:28 close / prior regular-session 15:59 close - 1) * 100`.
- Trigger: `abs(gap) > 1.278097837%`; equality does not trigger.
- Direction: sell a positive gap and buy a negative gap.
- The prior 15:59 and current 09:28 bars must use the same instrument ID.
- No candle, NOII, fair-value, day, macro, or order-flow feature may change the
  primary direction or trigger.

## Historical executable-price contract

Databento `GLBX.MDP3` `bbo-1s` is sampled at the end of each one-second
interval. The first snapshot that contains post-boundary information is
therefore stamped one second after the nominal boundary. Using the record
stamped exactly at 09:30:00 would leak the final quote from 09:29:59.

- Entry snapshot: `ts_recv == 09:30:01 ET`.
- Five-second diagnostic: `ts_recv == 09:30:06 ET`.
- Ten-second diagnostic: `ts_recv == 09:30:11 ET`.
- Timed exit snapshot: `ts_recv == 09:32:01 ET`.
- Long entry crosses the ask; long exit crosses the bid.
- Short entry crosses the bid; short exit crosses the ask.
- Bid and ask must be finite, positive, and strictly ordered.
- All four snapshots must have one identical instrument ID.
- Missing, duplicate, mismatched, or malformed marks refuse the session rather
  than substituting a midpoint or minute bar.

The crossed spread is already embedded in the observed result. Additional
round-trip slippage is reported at 0, 0.5, 1, 2, and 3 NQ points. Commission is
reported separately at $0, $2.50, $5.00, and $7.50 round trip, with NQ and MNQ
dollar translations. No commission scenario is called "actual" until an
execution venue is selected and its real fee schedule is supplied.

## Data minimization and provenance

- Download only 09:29:55 through 09:32:06 ET for qualifying dates.
- Keep one immutable JSONL file per session and record its SHA-256 digest.
- Record HTTP failures and unavailable sessions; never silently drop them.
- Do not download the estimated $76 full-period BBO package or the estimated
  $827 full-depth MBP-1 package for this gate.
- Raw Databento files remain local and outside Git.

## Primary audit gates

The timed-exit baseline passes an `EXECUTION_AUDIT_PASS` only if all are true:

- at least 50 valid qualifying sessions;
- positive mean and median after observed spread crossing, one additional NQ
  point, and the $5 round-trip commission scenario;
- session-bootstrap 95% lower bound above zero at that same cost;
- profit factor above 1.10;
- positive mean after deleting the best session;
- positive mean in every chronological third; and
- positive mean for both five- and ten-second delayed-entry diagnostics at the
  same cost.

Failure means the historical opening-price result is not executable under this
contract. Passing permits shadow-forward collection only; it does not authorize
orders or establish a live edge.

## Stop/target development

The timed 09:32:01 exit is primary. A separate bracket experiment may use only
the first chronological third to select from this fixed Cartesian grid:

- stop distance in NQ points: `4, 8, 12, 16, 24, 32, 48`;
- target distance in NQ points: `4, 8, 12, 16, 24, 32, 48`.

At each one-second snapshot after entry, a long is marked at the executable bid
and a short at the executable ask. If the stop and target are both crossed
between sampled observations, resolve the stop first. Execute at the observed
mark, never at a more favorable trigger price.

A pair is selectable only when its development mean, median, and bootstrap
lower bound are positive after one additional point and $5 commission. Rank
eligible pairs by the bootstrap lower bound, then mean, then lower maximum
drawdown, then wider stop, then wider target. If no pair qualifies, select the
timed exit and report `NO_BRACKET_SELECTED`. Apply the selected result once to
the untouched later two thirds without reselection. Disclose all 49 trials.

Because the historical minute-bar outcomes were already inspected, later-third
performance is retrospective validation, not pristine confirmation.

## Pre-registered mechanism attribution

The 08:30 ET one-minute move is descriptive and cannot filter the primary
audit. Label a session `information_created` when the absolute 08:30 bar move
is at least 10% of the absolute total gap; otherwise label it `pre_existing`.
Report count, win rate, mean, median, and bootstrap interval for both. A bucket
with fewer than 15 sessions is `INSUFFICIENT_MECHANISM_SAMPLE`.

## Publication boundary

Research code, tests, this specification, and compact aggregate reports may be
published in a separate research PR. Raw market data, API keys, local databases,
and virtual environments must not be committed.
