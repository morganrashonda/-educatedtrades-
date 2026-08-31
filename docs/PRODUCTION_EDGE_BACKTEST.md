# Main 5 production-edge backtest

## Purpose

`backend/research/production_edge_backtest.py` is a research-only replay of
the current Main 5 price signal. It uses the production RSI, EMA, ADX, regime,
trend-conviction, mean-reversion, stop and target definitions without opening
the broker, production database, or control files.

It answers three separate questions:

1. `cold_start`: what the present bot can execute when pattern memory has no
   statistically accepted trend pattern. Mean-reversion remains eligible,
   matching the current runtime exception.
2. `raw_price`: whether the underlying RSI/EMA/ADX price rule has any edge
   before learned-pattern gating.
3. `trained_pattern`: whether patterns fitted only on each split's training
   outcomes improve the untouched test set. Pattern confidence uses the same
   Sidak-corrected Wilson rule as production. The replay also preserves the
   runtime's current post-fill `rsi_value=50.0` recording behavior, so a
   learning-key mismatch remains visible instead of being silently corrected.

## Evidence hierarchy

The expanding chronological walk-forward result is primary. Each test block
occurs strictly after its training blocks, and observations capable of
overlapping the test holding horizon are purged.

The production parameters are frozen; this harness does not choose thresholds
from test performance. If candidate parameters are added later, they must be
selected in an inner training-only walk-forward loop before any outer result
is exposed.

The 6-group, 2-test-group CPCV run has all 15 combinations. It is a robustness
diagnostic, not the deployment estimate, because CPCV can train on a calendar
group later than one of its test groups.

Signals are formed using bars through time `t` and enter only when the next
bar opens during regular hours. Extended-hours bars may inform indicators but
cannot become fills. Stops win when a single OHLC bar touches both stop and target. Costs and
per-side slippage are deducted. Daily block-bootstrap confidence intervals,
a same-signal random-side benchmark, and a Sidak multiple-test adjustment are
reported. The report also includes cost stress at 0/3/6/10 bps, buy-and-hold,
per-symbol/side/exit attribution, and a machine-readable edge verdict.

## Frozen data contract

The bars CSV requires:

`timestamp,symbol,open,high,low,close,volume`

Timestamps must include a timezone. `symbol` may be omitted only when passed
with `--symbol`. Invalid ranges, duplicate keys, and non-monotonic per-symbol
timestamps fail the run rather than being silently repaired.

The optional real-news CSV requires:

`decision_timestamp,observed_at,symbol,score,source`

An observation later than its decision timestamp fails the run. News is
reported only as telemetry availability because current Main 5 does not use
sentiment as alpha. This harness never creates synthetic headlines or swaps
in price momentum as a sentiment proxy.

## Example: provisional QQQ 30-minute replay

```bash
cd "/Users/shaym/Downloads/Educated_Trades-main 5"
source backend/venv/bin/activate
python3 backend/research/production_edge_backtest.py \
  --bars backend/data/opening_research/qqq_1min_sip_2025-08-18_2026-08-17.csv \
  --symbol QQQ \
  --aggregate-minutes 30 \
  --cost-bps 3 \
  --slippage-bps 1 \
  --output backend/data/research_results/main5_qqq_30m.json
```

QQQ alone is provisional because production averages regime strength across
its configured universe. The decision-grade run needs synchronized,
point-in-time 30-minute bars for every configured symbol and realistic costs
per instrument.

The historical input does not contain per-cycle news-fetch health. The edge
test therefore states the explicit operational assumption that this gate was
healthy; it does not pretend that availability was reconstructed.

No result from this script enables learning, changes autonomous mode, or
authorizes capital.
