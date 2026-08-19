# Opening Failed-Auction / Video-Hypothesis Findings

Date: 2026-08-18

Status: discovery-only result. No production, Tier 3, learning, paper-order,
or live-order authorization.

## Question tested

Does an opening break at a pre-known prior-RTH, overnight, or five-minute
opening-range extreme become a better reversal after the video-inspired
sequence of:

1. high aggressive effort with weak price progress (absorption),
2. opposite signed trade dominance and OFI,
3. a reclaim of the level, and
4. a failed retest?

The protocol was frozen first in
`docs/OPENING_FAILED_AUCTION_VIDEO_TEST_SPEC_20260818.md`. This strategy is
separate from the immediate 09:30 two-minute gap fade.

## Data and integrity

- NQ MBP-1-derived one-second features, aggregated to non-overlapping five-
  second decision buckets.
- 66 files from 2026-05-13 through 2026-08-17; 64 sessions had valid,
  roll-clean point-in-time levels.
- Search interval: 09:30-10:30 ET.
- High levels permit only upward breakout attempts; low levels permit only
  downward breakout attempts.
- Entry follows the completed decision bucket and crosses the estimated BBO.
- Reported net values below include the estimated BBO crossing and one extra
  tick of slippage per side; commissions are not yet subtracted.
- This period is contaminated discovery data because other order-flow outcomes
  from it were inspected previously. It cannot provide untouched proof.

The deterministic report was reproduced byte for byte:

`backend/data/opening_research/opening_failed_auction_discovery_2026.json`

SHA-256:
`c9dbaeced2e5dfadbf1f315a0f2b2e246517969638b4ce474fdcb32e9a133ae5`

## Results

| Nested state | Events | 2-minute usable | Wins | Net mean points | Net median points | Session-cluster 95% CI |
|---|---:|---:|---:|---:|---:|---:|
| Bare level break | 201 | 187 | 86 (46.0%) | -1.39 | -5.54 | [-7.51, +4.48] |
| Failed break + reclaim | 141 | 128 | 53 (41.4%) | -5.32 | -10.17 | [-12.23, +1.59] |
| Absorption | 43 | 39 | 16 (41.0%) | -8.48 | -8.70 | [-22.80, +3.99] |
| Absorption + shift + reclaim | 24 | 22 | 11 (50.0%) | -3.22 | -2.57 | [-20.69, +15.01] |
| Full sequence + failed retest | 0 | 0 | 0 | n/a | n/a | n/a |

Neither absorption nor the dominance/reclaim confirmation improved the frozen
two-minute primary outcome over the bare-break baseline. No valid full
failed-retest sequence occurred in the discovery sample, so its expectancy is
unmeasured rather than zero.

Five- and fifteen-minute secondary results were unstable, sparse, or negative.
No secondary horizon provides a basis to replace the frozen primary.

## Decision

**Do not promote this exact rule and do not buy a large historical MBP-1
validation package for it yet.** It fails the discovery go/no-go screen on
frequency, net expectancy, incremental value, and uncertainty.

This does not show that all auction or footprint methods are false. It shows
that this operational definition, at these point-in-time levels, did not turn
the video's narrative into measurable edge on the available discovery data.

## What remains potentially testable

The video relies heavily on exact volume-profile location (VAH/VAL/POC) and
price-level footprint behavior. Those were not fabricated from OHLC bars and
are not present in this Phase 1 contract. A materially different Phase 2 would
need point-in-time trade-at-price profiles and exact raw MBP event sequences.
It should start with a small, cost-quoted pilot and its own frozen rules. It
must not be described as a continuation of a passing Phase 1 result.

The already validated cross-instrument immediate gap-fade remains the stronger
research direction. Its unresolved question is executable cost/forward
stability, not whether this failed-auction filter rescues it.

## Verification

- 15 focused failed-auction/order-flow tests passed.
- 45 opening-research tests passed together.
- Two report generations were byte-identical.
- No production files, coordinator state, broker state, learning database, or
  bot process were changed.
- No additional Databento purchase was made for this test.
