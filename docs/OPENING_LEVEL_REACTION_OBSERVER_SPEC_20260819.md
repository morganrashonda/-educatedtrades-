# Opening Level-Reaction Observer

Status: frozen measurement-only research specification. This document does
not authorize production integration, Tier 3 execution, learning writes,
paper orders, or live orders.

Frozen: 2026-08-19 before evaluating the generalized classifier's outcomes.

## Objective

Measure whether an attempted break of a known NQ level is accepted, fails, or
remains unresolved. The observer joins four independently recorded layers:

1. map: levels known before the attempted break;
2. confirmation: price response, aggressive trades, and MBP-1 order flow;
3. invalidation: acceptance back through the level or continuation beyond it;
4. context: gap, prior range, and explicitly timestamped headline tags.

The central continuous diagnostic is directional price progress per unit of
aggressive effort. High effort with weak or opposite progress is recorded as
possible absorption; it is not automatically treated as a reversal.

## Evidence and clock

- Instrument: Databento `NQ.v.0`, retaining the actual instrument ID.
- Level source: point-in-time intraday NQ OHLC bars at their observed source
  cadence, except the first-minute
  opening range, which is derived from one-second MBP states only after that
  minute has completed.
- Reaction source: one-second BBO, trades, and MBP-1 rows in the audited
  research SQLite store.
- Timezone: `America/New_York`; no fixed UTC offset.
- Initial observation interval: the available 09:28-09:36 ET evidence window.
- Tick: 0.25 NQ points.

Every feature interval is half-open and ends strictly before its decision
timestamp. A one-second bucket can start an attempted break only after that
bucket has ended. Entry is the first observed BBO no more than two seconds
after the decision. Missing evidence is excluded or classified unresolved;
nothing is imputed.

## Point-in-time levels

The initial map contains:

- previous regular-session high and low;
- overnight high and low, 18:00 through 09:29:59 ET;
- 09:00-09:29:59 premarket high and low;
- previous completed regular-session week high and low;
- first-minute opening-range high and low, eligible only at 09:31:00 ET.

Pre-open levels are eligible at the cash open. Opening-range levels are kept
separate and cannot leak into earlier decisions. Levels of the same eligibility
class within four ticks are clustered. Instrument-roll mismatches exclude the
affected prior-period levels rather than translating them.

The map also records, without using future data: cash-open gap from the prior
RTH close, mean prior-20-session range, gap/range ratio, and prior five-session
close return.

## Attempt and decision clocks

An upward attempt starts when a one-second close first crosses at least one
tick above a high-type level. A downward attempt is symmetric for a low-type
level. The break becomes observable at the end of that second.

The observer evaluates fixed decisions 5, 10, 30, and 60 seconds after the
observable break. A level has a 300-second attempt cooldown. Nearby eligible
levels that describe the same crossing are one event cluster.

## Classification

All signs are expressed in attempted-break direction `s`.

At each decision, calculate from post-break rows strictly before the cutoff:

- distance from the level;
- signed aggressive-trade imbalance;
- depth-normalized OFI;
- directional price progress;
- directional aggressive volume;
- progress per aggressive contract;
- bid/ask refill ratios and directional refill balance;
- queue imbalance, microprice displacement, spread, and depth.

Classifications use signs and one-tick location only; no outcome-tuned
magnitude threshold is introduced:

- `accepted_break`: price is at least one tick beyond the level, aggressive
  trade imbalance agrees with `s`, and depth-normalized OFI agrees with `s`;
- `failed_break`: price is at least one tick back inside, aggressive trade
  imbalance opposes `s`, and depth-normalized OFI opposes `s`;
- `unresolved`: every other state, including missing evidence.

Mechanism tags are descriptive:

- `accepted_flow` for an accepted break;
- `opposite_dominance` for a failed break;
- `absorption_divergence` when aggressive trades still support the attempted
  break but price is no longer beyond the level and refill pressure opposes it;
- `mixed_or_missing` otherwise.

## Headline context

An optional JSONL sidecar may provide `event_id`, `published_at`, `scope`,
`sentiment`, `significance`, `source`, and `symbols`. Only events published no
later than the decision are attached. The observer does not infer sentiment,
materiality, index weight, or affected direction. Without a sidecar, headline
context is explicitly `DATA_GATED`, never neutral or zero.

## Outcomes

Every classified decision records both hypothetical directions so selection
cannot hide the alternative:

- continuation side `s`;
- reversal side `-s`.

For 30, 60, 120, 180, and 300 seconds where evidence exists, record:

- observed executable BBO entry and exit;
- crossing P&L in points;
- one additional tick per side stress;
- MFE and MAE using executable liquidation quotes;
- timestamps of MFE and MAE;
- time spent outside versus inside the level.

`accepted_break` maps to continuation only for descriptive strategy summaries;
`failed_break` maps to reversal. `unresolved` never maps to a trade.

## Required tests

1. weekly and opening-range levels are unavailable before their eligibility;
2. future order flow cannot change an earlier classification;
3. the exact decision timestamp is excluded from features;
4. upper and lower attempts are symmetric;
5. accepted, failed, absorption-divergence, and unresolved states are distinct;
6. entry never precedes decision and missing quotes are not fabricated;
7. MFE/MAE use executable sides, not midpoint fantasy fills;
8. future headlines are excluded;
9. no broker, production, learning, or Tier 3 module is imported;
10. SQLite and input files are opened read-only for observation.

## Interpretation boundary

The current historical MBP inventory has already been inspected in related
research. Results from it are discovery and implementation validation only.
No classifier can advance from this work directly to execution. A frozen
shadow-forward sample is required, with all attempts and abstentions retained.
