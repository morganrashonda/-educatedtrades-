# NQ opening-gap executable BBO audit findings

Date: 2026-08-18

Status: `EXECUTION_AUDIT_PASS` for retrospective execution feasibility.
This does not authorize Tier 3, learning, paper orders, live orders, or a
production-code change.

## Frozen question and contract

The audit tested only the previously frozen large-gap fade:

- roll-clean NQ cash sessions from 2023-08-17 through 2026-08-17;
- absolute 09:28 ET gap strictly greater than `1.278097837%`;
- fade the gap;
- cross the BBO at 09:30:01 and 09:32:01 ET;
- report five- and ten-second delayed entries;
- require the prior 15:59 bar, 09:28 bar, and all executable BBO marks to use
  the same futures contract;
- primary costs are observed spread crossing, one additional NQ point of
  round-trip slippage, and $5 round-trip commission.

The signal trigger and direction were not changed after observing BBO outcomes.

## Data and cost integrity

- Qualifying sessions: 68.
- Valid executable sessions: 68.
- Refusals or missing exact marks: 0.
- Contract-ID mismatches: 0.
- Downloaded BBO data: 3,177,352 bytes across 68 immutable JSONL files.
- Hash mismatches between files, manifest, and report: 0.
- Estimated new Databento charge: $0.109353661533.
- Enforced caps: $0.25 and 20 MiB.
- Raw BBO data and generated JSON reports remain local under ignored `data/`.

## Primary result

After side-correct spread crossing, one additional NQ point, and $5 commission:

- Wins: 44 of 68 (`64.71%`).
- Mean: `+15.5809 NQ points` per signal.
- Median: `+12.25 NQ points`.
- Total: `+1,059.5 NQ points`.
- Session-bootstrap 95% interval for the mean: `[+5.8199, +25.4963]` points.
- Profit factor: `2.6294`.
- Maximum chronological drawdown: `104.5` points.
- Worst session: `-59.0` points.
- Best session: `+131.75` points.

The mean is about `$311.62` per one-NQ observation under that explicit cost
contract. This is a normalized historical expectancy, not an expected live
paycheck: it excludes margin constraints, order-size market impact, outage risk,
and the original signal-search bias.

Deleting the best session leaves 67 observations with a `+13.8470` point mean,
95% interval `[+4.4813, +23.2388]`, and profit factor `2.4268`.

## Stability diagnostics

Chronological-third means after primary costs:

- First 22: `+14.3864` points.
- Middle 23: `+17.6957` points.
- Last 23: `+14.6087` points.

All three means are positive. The first and last thirds' individual bootstrap
intervals cross zero, so they are directional stability checks rather than
standalone proof.

Delayed-entry diagnostics after primary costs:

- Five-second delay: mean `+14.9706`, median `+11.125`, profit factor `2.5722`.
- Ten-second delay: mean `+12.6397`, median `+12.375`, profit factor `2.2034`.

At the harsh reported stress of three additional points plus $5 commission,
the NQ result remains positive: mean `+13.5809`, median `+10.25`, bootstrap
interval `[+3.8199, +23.4963]`, profit factor `2.3202`.

## Mechanism clue

The pre-registered descriptive split supports the earlier mechanism hypothesis:

- `pre_existing`: 54 sessions, 38 wins (`70.37%`), mean `+21.1065`, median
  `+17.0`, bootstrap interval `[+9.6667, +32.2963]`, profit factor `3.6022`.
- `information_created`: 13 sessions, 6 wins (`46.15%`), mean `-3.6538`, median
  `-9.75`, bootstrap interval `[-18.4231, +13.2115]`, profit factor `0.7354`.

The second bucket is below the frozen minimum of 15 and is therefore
`INSUFFICIENT_MECHANISM_SAMPLE`. This split is descriptive and may not be used
as a production filter until it is pre-registered and confirmed on untouched
data.

## Stop/target experiment

The first chronological third selected a 32-point stop and 12-point target from
the disclosed 49-pair grid. Development performance looked strong, but the
later two thirds produced only `+2.0380` points per signal and a bootstrap
interval of `[-4.4783, +7.8424]` after primary costs.

Conclusion: the bracket did not validate convincingly. Keep the timed 09:32:01
exit as the primary research rule; do not promote the selected bracket.

## What this result does and does not establish

It establishes that the previously observed two-minute NQ fade was not merely
an OHLC or midpoint artifact. Across these 68 sessions it survived exact
side-correct BBO pricing, explicit costs, delayed entries, best-session deletion,
and chronological subdivision.

It does not establish a deployable edge. The threshold and strategy family were
found after inspecting historical minute outcomes, so the 68-session result is
retrospective and selection-biased. The BBO execution layer was new; the signal
idea was not. It also does not measure queue position, an actual broker's fill
quality, or a real fee schedule.

## Safest next gate

Run the frozen timed-exit rule in shadow-forward mode without orders and, in
parallel, test it on historical dates that were not used to discover the
threshold. Any extra Databento spend should buy untouched confirmation, not
more parameter searches on these same 68 sessions. A production/Tier-3 proposal
should wait for that independent gate.
