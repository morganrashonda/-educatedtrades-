# Honest Out-of-Sample Backtest — Educated Trades

**Author:** Quant Engineer · **Generated from:** `backtest.py` → `backtest_results.json`

## TL;DR (read this first)

On **real out-of-sample data**, the current strategy **lost money and badly
underperformed simply owning the index**. This is the honest result the owner
asked for — no overfitting, no cherry-picking.

| Strategy (out-of-sample) | Total return | Win rate | Profit factor | Sharpe | Max drawdown |
|---|---|---|---|---|---|
| **A — Price-only** | **−11.53%** | 39.7% | 0.76 | −0.46 | −16.4% |
| **B — Hybrid (momentum sentiment)** | **−13.60%** | 38.0% | 0.71 | −0.58 | −19.4% |
| **Benchmark — Buy & Hold SPY** | **+14.79%** | — | — | +0.76 | −19.0% |

The strategy lost ~11–14% during a window when the S&P 500 **rose ~15%** — a
~26–28 point underperformance. The **85% win-rate milestone is not remotely
met** (actual ≈ 38–40%). **Recommendation: do NOT deploy real capital.**

## What was tested

- **Data:** real daily OHLCV for **SPY, QQQ, IWM** from Alpaca's historical API
  (Stooq fallback). **874 bars each, 2022-01-03 → 2025-06-27** (~3.5 years).
- **Out-of-sample split (mandatory):**
  - **Train / in-sample:** 2022-01-03 → 2024-06-07 (611 bars, oldest 70%).
    Pattern-memory win rates were learned *only* here.
  - **Test / out-of-sample:** **2024-06-10 → 2025-06-27** (263 bars, newest 30%).
    All reported metrics are from this unseen window.
- **Strategy replay** reuses the **production indicator code** (`patterns.compute_adx`,
  `compute_rsi`, `compute_ema`, `classify_regime`) so the backtest can't drift
  from the live logic:
  - **ADX(14) regime detection** → trend-following in trending regimes,
    **RSI mean-reversion** (fade extremes) in range-bound, half size when
    transitioning.
  - **Pattern-memory gate:** setups whose *in-sample* win rate was < 40% (n ≥ 10)
    are skipped in the test period.
  - **Variant A (price-only):** technical signals only.
  - **Variant B (hybrid):** adds a **price-momentum sentiment proxy** (positive
    trailing 10-day return → bullish, negative → bearish) blended into conviction.
- **Risk model (mirrors production):** hard **−2.5% stop / +3.0% target**,
  checked intraday against each day's high/low, 15-day time stop, one position
  per symbol, 2%-risk position sizing capped at 25% of equity.

## No look-ahead bias

Signals on day *t* use **only** bars up to *t*; entry is at **day t+1's open**;
exits use day *t+1…* highs/lows/closes. Indicators are recomputed on the
trailing window each day. The pattern-memory gate is fit **exclusively on the
train slice** and frozen before the test window begins.

## What the numbers say

- **It's not a win-rate problem you can tune away.** Even *in-sample*, learned
  win rates were mediocre: `trending|long` 52.6%, `trending|short` 46.2%,
  `range_bound|short` 40.9%, `transitioning|short` 40.0%. The one genuinely
  decent edge was **`range_bound|long` at 65.4%** — buying oversold dips in a
  range. Short setups were consistently the weakest, echoing the earlier
  signal-audit finding that this system over-produces losing shorts.
- **Adding "sentiment" made it worse.** The momentum-based sentiment proxy
  (Variant B) *reduced* return (−13.6% vs −11.5%) and Sharpe (−0.58 vs −0.46).
  A naive momentum overlay is not additive here.
- **Both variants drew down hardest on 2025-04-09** (−16.4% / −19.4%), the same
  volatile stretch where buy-and-hold also bottomed — the strategy took the
  downside without capturing the subsequent recovery the index enjoyed.
- **Profit factor < 1** for both variants (0.76, 0.71) means gross losses
  exceeded gross wins — a structurally unprofitable edge over this window, not
  just bad luck.

## Honest caveats

- **Sentiment can't be replayed faithfully** without a historical news archive,
  so the *real* sentiment engine is only *approximated* (Variant B). The live
  system's news-driven edge (if any) is neither proven nor disproven here — but
  the price/technical core, on its own, is a net loser out-of-sample.
- Single 3.5-year window, one 70/30 split, US index ETFs only. Results would
  firm up with walk-forward splits and more symbols/regimes.
- Costs: fills assume open/stop/target prices with no commission (Alpaca is
  commission-free) and no slippage modeling — real results would be *slightly
  worse*, not better.

## Recommendation

1. **Do not move to real capital.** The strategy fails its own KPIs
   out-of-sample and loses to a passive index.
2. **Prioritise the one real edge:** `range_bound|long` mean-reversion (buy
   oversold dips) was the only setup > 60% in-sample. Consider restricting the
   live system to long/mean-reversion setups and dropping short signals until
   they can be shown to work.
3. **Re-evaluate the sentiment premise.** The momentum proxy hurt; the live news
   engine needs its *own* out-of-sample validation before it's credited with
   alpha.
4. **Keep the safety layer.** Stops/targets/market-hours gating are correct and
   should stay regardless of strategy changes.

## Files

- `backend/backtest.py` — the backtest engine (data load, signal replay, metrics).
- `backend/backtest_results.json` — full metrics per variant + **daily equity
  curve** (plottable on the dashboard) + benchmark + in-sample pattern stats.
- This report.
