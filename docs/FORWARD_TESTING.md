# Forward-testing contract

This is the operating contract for deciding whether the strategy has evidence
of an edge. It is a measurement process, not a promise of profitability.

## Freeze the test

Run one unchanged strategy configuration in paper mode for a pre-registered
window. Do not change indicators, thresholds, symbols, sizing, or exits in
response to interim results. Record the start commit, configuration, universe,
timeframe, and data feed before the first cycle.

The minimum useful gate is **100 closed trades across multiple market
conditions**. The existing 30-trade/20-day readiness check is an early warning
indicator, not proof of an edge. Correlated ETFs do not count as independent
observations merely because they create separate trade rows.

## Record every decision

Use the decision journal and order ledger as the source of truth. A review must
be able to reconstruct, for every candidate signal:

- timestamp, symbol, side, regime, indicator values, sentiment state, pattern,
  conviction, and configured risk;
- whether the signal was executed or refused, including the exact refusal;
- submitted, acknowledged, and filled quantities and prices;
- spread/slippage, exit reason, realized P&L, and broker-confirmed outcome.

An empty journal is not a successful test: it means the signal path or data
path needs investigation. A no-trade result is only interpretable when the
refusal reasons are present and the data-health checks passed.

## Evaluation rules

Evaluate net results after spread, fees, slippage, partial fills, and delayed
execution. Require the result to survive:

1. walk-forward out-of-sample testing;
2. purged or combinatorial cross-validation with no look-ahead;
3. clustered/block-bootstrap uncertainty intervals;
4. multiple-testing correction or deflated Sharpe; and
5. a cost-stress scenario above the observed average cost.

Do not select the best pattern, symbol, or threshold after looking at the test
results. If a feature is claimed to matter, run an ablation with the same
trades and report the difference, sample size, and uncertainty. Real news data
must remain data-gated; synthetic headlines are not evidence.

## Learning boundary

Learning stays disabled until outcomes are confirmed by the broker, the sample
is large enough for the configured confidence rule, and the owner separately
authorizes enablement. The learner must not rewrite the test configuration or
retroactively relabel historical outcomes. A negative or inconclusive result
is a valid result and should not be “fixed” by tuning until it wins.

## Go/no-go decision

Do not move to live capital merely because paper P&L is positive. The review
must show positive net expectancy, adequate sample size, drawdown within the
declared limit, stable operation, clean reconciliation, and robustness to
reasonable cost increases. If any of those are missing, keep the bot in paper
or manual mode and document the blocker.

Useful review commands:

```bash
python3 backend/decision_log.py
python3 backend/forward_test.py
python3 backend/tests/test_suite.py
python3 backend/tests/test_end_to_end.py
```
