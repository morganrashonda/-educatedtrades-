# NQ opening microstructure findings — 2026-08-19

## Decision

There is a repeatable **research signal worth freezing for shadow-forward observation**, but there is not yet a production edge and nothing in this work authorizes Tier 3 or orders.

The candidate is:

1. Measure signed buyer- versus seller-initiated NQ volume from 09:28:00 through 09:29:59 ET.
2. Use its sign as the proposed direction.
3. Wait until 09:30:05 ET.
4. Abstain unless depth-normalized MBP-1 order-flow imbalance observed from 09:30:00 through 09:30:04 points in the same direction.
5. For research measurement only, mark entry at the observed executable BBO side at 09:30:05 and mark exit two minutes later.

This exact rule was discovered retrospectively among several gates. It must therefore be frozen unchanged and tested on future sessions before it can be considered for Main 5.

## Evidence inventory

- Instrument: Databento `NQ.v.0`, continuous front contract.
- Evidence span: 2021-08-17 through 2026-08-14.
- Complete cash-open BBO and trades sessions: 1,254.
- Outcome-independent MBP-1 sample: 454 sessions selected by fixed date-hash ordering, not by returns or volatility.
- MBP sessions by year: 80, 80, 71, 71, 76, 76 for 2021–2026.
- Compact MBP rows: 217,780; ledger-derived row total agrees exactly.
- Timing-safe feature rows: 4,086.
- Unavailable 09:36 marks: 454 excluded, never imputed.
- Source hashes: present and structurally valid for all completed MBP requests.
- SQLite integrity: `ok`.
- Total Databento ledger cost across the research collection: $67.656995, below the $78 ceiling.
- One paid MBP 504 response remains in the audit trail as failed evidence and is excluded.

Every predictive feature ends strictly before its decision timestamp. Entry and exit P&L use the executable BBO side rather than midpoint fills. Fixed additional cost stresses are 0.25, 0.75, 1.25, and 2.25 NQ points per measured trade.

## Frozen walk-forward results

The full regularized MBP model did **not** establish an edge at the cash open over the next two minutes:

- 272 chronological out-of-sample observations.
- AUC 0.538; accuracy 51.5%.
- Mean net +0.53 points at 0.25-point cost, but −1.47 points at 2.25-point cost.
- Gross 95% session-bootstrap interval: −3.15 to +4.85 points.

Depth-normalized OFI alone also failed:

- Accuracy 48.9%.
- Mean net −0.23 points at 0.25-point cost.
- Gross 95% interval: −3.92 to +3.93 points.

The simple pre-open signed aggressive-flow direction was the only frozen baseline with a clearly positive unadjusted gross interval:

- 272 chronological out-of-sample observations.
- Accuracy 56.6%.
- Mean net +4.81 points at 0.25-point cost and +2.81 at 2.25-point cost.
- Gross 95% interval: +1.13 to +8.87 points.

This is not pristine discovery OOS because related opening research had already inspected overlapping history. It is evidence of a candidate, not proof of edge.

## Stability sensitivity

Across the full 454-session MBP inventory, after excluding two exactly flat targets and three zero-flow sessions, the pre-open flow rule had:

- 449 observations; 98.9% coverage.
- 55.7% accuracy.
- Mean net +3.22 points at 0.25-point cost and +1.22 at 2.25-point cost.
- Positive mean at 0.25-point cost in all six calendar-year buckets.
- Equal-year mean +3.20 points at 0.25-point cost.

However, 2026 supplied 745.25 of the 1,559.5 aggregate gross points. Excluding 2026 reduced the mean to +1.93 points at 0.25-point cost, and the gross 95% interval became −0.30 to +4.82. The effect is therefore materially strengthened by the newest regime.

Pre-open price agreement and refill-pressure agreement improved the retrospective mean, but each lost in 2023. They do not provide a reliable acceptance test by themselves.

## Five-second confirmation candidate

Requiring the first five seconds of depth-normalized MBP OFI to agree with the pre-open flow direction produced:

- 225 observations; 49.6% session coverage.
- 60.0% accuracy.
- Mean net +3.38 points at 0.25-point cost and +1.38 at 2.25-point cost.
- Positive 0.25-point-cost mean in five of six year buckets.
- Mean +2.02 points at 0.25-point cost after excluding 2026.
- Gross 95% interval +0.05 to +7.14 points.

That interval is only barely positive before the fixed cost is subtracted; the net interval crosses zero even at the lowest cost. The gate also lost in 2023 and was selected from multiple exploratory gates. This is why it should be shadow-forward tested, not traded.

Waiting longer did not improve the evidence consistently. By 60 seconds, the ungated pre-open signal was only +0.82 points at the lowest cost and negative under the harsh cost stress. The useful information appears concentrated in the first seconds and first two minutes, consistent with the original opening hypothesis.

## What can safely be said

1. Pre-open aggressive trade imbalance contains directional information about the next two NQ minutes in this sample.
2. The relationship is association, not demonstrated causation.
3. A five-second MBP OFI agreement gate is the best mechanism candidate found here for distinguishing flow that continues from flow that may be absorbed.
4. The complex model, queue imbalance, microprice, refill ratios, and OFI alone did not demonstrate stable standalone edge.
5. The result is sensitive to regime, costs, and multiple testing. It is not ready for autonomous execution, Tier 3, enabled learning, or real money.

## Required next step

Freeze the five-second rule above and run a measurement-only observer after each cash session. Do not tune thresholds during the forward window. Record every eligible and rejected session, executable BBO entry/exit, raw pre-open flow, first-five-second OFI, slippage, and reason for abstention.

The first review should occur after at least 40 eligible signals, with a stronger decision gate at 80–100 eligible signals. Promotion requires positive net expectancy under the predeclared cost model, a confidence interval that does not rely on one month or regime, and no safety or data-quality failures. A failed forward result retires or redesigns the hypothesis; it must not be rescued by post-hoc threshold changes.

## Verification

- Focused research tests: 19 passed.
- Repository safety/integrity suite: 837 passed.
- End-to-end suite: 33 passed.
- Remaining pytest suite: 218 passed.
- Total: 1,088 passed, 0 failed.
- Research modules compile under the production-upgrade Python 3.12 environment.
- Optional Ruff lint could not run because Ruff is not installed in that environment.
- Scope scan found no broker, order submission, production mode, learning database, or Tier 3 imports in the research modules.

Main 5 production/runtime code and state were not changed.
