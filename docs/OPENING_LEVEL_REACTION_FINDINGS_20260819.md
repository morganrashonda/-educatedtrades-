# Opening Level-Reaction Findings

Status: discovery measurement only. No production integration, learning,
Tier 3 execution, paper orders, or live orders are authorized by this report.

## What was tested

The frozen observer specification was evaluated against 454 complete NQ MBP-1
sessions from 2021-2026. It reconstructed levels point in time from 3,503,782
historical intraday bars and observed 1,424 attempted breaks across 453 sessions.
The dominant source-bar cadence was 60 seconds (3,487,122 adjacent rows).

Levels included prior RTH, overnight, 09:00-09:29 ET premarket, prior completed
RTH week, and the first 60 seconds of the cash session. Decisions were fixed at
5, 10, 30, and 60 seconds after a break became observable. Both continuation
and reversal were measured at 30, 60, 120, 180, and 300 seconds using executable
BBO sides plus one additional tick of stress at entry and exit.

Headline context remained `DATA_GATED`; no untimestamped or synthetic news was
treated as evidence.

## Primary result

The generalized rule is rejected as a current trading strategy. Every populated
decision/horizon combination had negative average stressed P&L. The least-bad
combination was:

- decision: 30 seconds after the observed break;
- outcome: 30 seconds after the decision;
- classified observations with outcomes: 782 across 396 sessions;
- observation-weighted stressed mean: -0.36 NQ points;
- equal-session-weighted stressed mean: -0.76 points;
- deterministic session-cluster bootstrap 95% interval: [-1.80, +0.40];
- stressed win rate: 52.05%.

For the same observations, random direction averaged -1.17 points. The
classifier improved direction selection by +0.81 points, but that improvement
was not large or stable enough to clear executable costs.

## Accepted and failed breaks are not equivalent

At the 30-second decision and 30-second outcome:

| State | n | Stressed mean | Win rate | Equal-session mean | Session-cluster 95% interval |
| --- | ---: | ---: | ---: | ---: | ---: |
| Accepted break / continuation | 455 | +0.18 | 56.70% | -0.66 | [-1.91, +0.64] |
| Failed break / reversal | 327 | -1.11 | 45.57% | -1.29 | [-3.22, +0.40] |

The accepted-break observation-weighted mean is positive, but it becomes
negative when each session receives equal weight. That is evidence of
within-session concentration, not a stable edge. Failed-break reversal is the
clearly weaker half and should not be promoted as a strategy.

## Stability checks

The least-bad aggregate was not stable by year:

| Year | n | Stressed mean | Win rate |
| --- | ---: | ---: | ---: |
| 2021 | 144 | -0.11 | 48.61% |
| 2022 | 129 | -2.34 | 44.19% |
| 2023 | 143 | -0.46 | 53.85% |
| 2024 | 118 | +1.95 | 60.17% |
| 2025 | 130 | +0.40 | 55.38% |
| 2026 | 118 | -1.50 | 50.85% |

Downward attempts were stronger than upward attempts in this one diagnostic,
but both side-specific confidence intervals crossed zero. Level-family splits
also produced attractive-looking pockets, including opening-range lows and
overnight-high confluence. Those are post-hoc findings from a searched grid and
are not edge claims.

## Integrity findings from three reviews

The implementation reviews caught and corrected these issues before the final
report:

1. prior-week instrument IDs were not propagated into weekly aggregates,
   incorrectly excluding all weekly levels;
2. a decision could initially classify a partial evidence window;
3. progress per aggressive contract initially clipped adverse progress to zero;
4. dwell was initially reported as observation counts rather than elapsed time;
5. the source filename implied five-minute bars while the dominant observed
   cadence is one minute, so the specification now describes observed cadence.

Regression coverage includes exact-decision poison rows, future rows, future
headlines, upper/lower symmetry, missing start/end evidence, executable BBO
fills, MFE/MAE, elapsed dwell, weekly and opening-range eligibility, read-only
SQLite, and forbidden production dependencies.

## Safe conclusion and next research direction

The evidence supports one narrow statement: confirmation flow may contain weak
short-horizon directional information, while the tested failed-break reversal
rule does not. It does not support deploying either rule.

The next defensible step is an untouched shadow-forward test, not another
historical threshold search. Freeze an accepted-break continuation candidate
before collecting new sessions, retain every attempt and abstention, and test
whether its session-equal net result remains positive after the same costs.
Order-flow, replenishment, microprice, gap context, and timestamped headlines
should remain measured explanatory fields rather than outcome-tuned gates.

Any level-family or side-specific candidate must be registered separately and
pay a multiple-testing penalty. No candidate should enter Tier 3 until it passes
an untouched forward sample and an explicit implementation/safety review.

## Verification

- New observer tests: 15/15 passed.
- All opening-research tests: 143/143 passed.
- Legacy safety and integrity suite: 837/837 passed.
- End-to-end suite: 33/33 passed.
- Remaining backend pytest collection: 233/233 passed.
- Full backend gate: 1,103 passing checks, 0 failures.

The detailed 34 MB discovery artifact is stored outside the repository at
`/Users/shaym/Documents/Educated Trades/opening_level_reaction_discovery_5y.json`.
