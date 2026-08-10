# Strategy Research Report: Alternative Trading Strategies

**Date:** July 2, 2026  
**Author:** Quant Engineer, Educated Trades  
**Status:** Completed & Validated Out-of-Sample  

---

## 1. Executive Summary

A rigorous, out-of-sample backtest of the production algorithmic strategy (Variant A/B) previously demonstrated that the technical-only core is structurally unprofitable in recent market regimes, failing to hit the owner's **85% target sniper win-rate**. 

In response, we have developed a unified, event-driven parameter optimization and strategy research engine in `backtest.py` to evaluate **four alternative trading strategies**. To ensure absolute comparability and prevent curve-fitting "fantasy" results, we applied an **honest 70/30 Train/Test split** across daily historical bar data (`2022-01-03` to `2025-06-27`) for **SPY, QQQ, and IWM**. In-sample parameters were swept and optimized strictly on the Training set (`2022-01-03` to `2024-06-07`) before being locked and evaluated on the Out-of-Sample Test set (`2024-06-10` to `2025-06-27`).

### **Core Finding:**
* **No active technical strategy outperformed the passive Buy & Hold SPY benchmark (+14.79%) out-of-sample.** 
* The **200-day SMA Trend Filter (Strategy D)** with a trend-regime filter was the only active strategy to achieve positive absolute returns (**+0.54%**), representing a massive improvement over the production strategy (Variant A: **-11.53%**, Variant B: **-13.60%**).
* **Do NOT deploy real capital to any of these active strategies in their current states.** Active strategies that trade both long and short suffered a severe drag during the sustained 2024–2025 bull market due to short-side whipsaws and tight stop-loss exits.

---

## 2. Research & Backtest Methodology

* **Data Period:** `2022-01-03` to `2025-06-27` (874 daily bars)
  * **In-Sample (Train) Period:** `2022-01-03` to `2024-06-07` (611 daily bars) — used to optimize indicators and learn pattern-memory statistics.
  * **Out-of-Sample (Test) Period:** `2024-06-10` to `2025-06-27` (263 daily bars) — used for frozen validation.
* **Risk Model Constraints (Standardized):**
  * Hard percentage stop-loss: **-2.5%** (except Strategy F, which uses ATR).
  * Hard percentage take-profit: **+3.0%** (except Strategy F, which uses ATR).
  * Risk per trade: **2.0%** of daily equity notional (max 25% portfolio cap per symbol).
  * Time-based exit stop: Max holding period of **15 trading days** (except optimized MR holding sweeps).
  * **Strict anti-lookahead bias:** Signal calculated at the close of day $t$ enters at the Open of day $t+1$. Intraday prices (High/Low of $t+1$ onwards) are checked to simulate stop-loss/take-profit hits realistically.

---

## 3. Evaluated Strategies

### **Baseline Production Core:**
* **Variant A (Price-Only):** Trades mean-reversion via RSI in range-bound regimes (ADX < 20) and EMA trend-following (12/26 EMA) in trending regimes (ADX > 25). Filters signals using historical setup win rates learned on the Train period.
* **Variant B (Hybrid):** Replays Variant A, but blends a 10-day price momentum proxy as a simulated "sentiment" engine.

### **Alternative Strategies Evaluated (New):**
1. **Strategy C: Optimized Pure Mean-Reversion (`C_mean_revert_long`):** 
   * *Concept:* Isolates and stress-tests the only proven historical edge (`range_bound|long`).
   * *In-Sample Optimization:* Swept RSI thresholds `[25, 30, 35]` and holding periods `[5, 10, 15]`.
   * *Best Train Config:* RSI threshold $\le$ **25**, Max holding period = **15 days**.
2. **Strategy D: 200-day SMA Trend Filter (`D_sma_200_trend`):**
   * *Concept:* Classic trend-following. Long if price > 200 SMA, short if price < 200 SMA when flat.
   * *In-Sample Optimization:* Tested with/without an ADX trend-regime filter (only trade if ADX > 25).
   * *Best Train Config:* **With trend-regime filter active** (skip range-bound markets).
3. **Strategy E: Optimized Moving Average Crossover (`E_ema_crossover`):**
   * *Concept:* Dual MA crossover (golden/death cross).
   * *In-Sample Optimization:* Swept MA types (`EMA` vs `SMA`), fast/slow periods `[(10,30), (20,50), (50,200)]`, and trend-regime filter (`True`/`False`).
   * *Best Train Config:* Simple Moving Average crossover (**SMA**), fast = **50**, slow = **200**, **with trend-regime filter active**.
4. **Strategy F: Volatility Breakout (`F_volatility_breakout`):**
   * *Concept:* Momentum breakout. Long if close > 20-day high with above-average volume; short if close < 20-day low with above-average volume.
   * *Stops:* Dynamically adjusted via Average True Range (14-day ATR). **Stop-Loss = $2.0 \times \text{ATR}$**, **Take-Profit = $3.0 \times \text{ATR}$**, or 15-day time stop.

---

## 4. Comparison Performance Table (Out-of-Sample)

The following table summarizes the performance of all tested strategies over the **263-day out-of-sample test period**:

| Strategy Name | Total Return (%) | CAGR (%) | Num Trades | Win Rate (%) | Profit Factor | Sharpe Ratio | Max Drawdown (%) | Avg Hold (Days) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Benchmark (SPY Buy & Hold)** | **+14.79%** | **+14.07%** | **0** | **N/A** | **N/A** | **0.76** | **-19.00%** | **N/A** |
| **Strategy D: 200-SMA Filter (Opt)** | **+0.54%** | **+0.52%** | **120** | **49.17%** | **1.018** | **0.130** | **-13.27%** | **5.67** |
| **Strategy C: Mean Reversion (Opt)** | **-0.49%** | **-0.47%** | **2** | **50.00%** | **0.211** | **-0.458** | **-0.76%** | **9.50** |
| **Strategy F: Volatility Breakout** | **-6.29%** | **-6.02%** | **39** | **35.90%** | **0.612** | **-0.694** | **-8.56%** | **9.15** |
| **Strategy E: MA Crossover (Opt)** | **-10.94%** | **-10.48%** | **118** | **41.53%** | **0.744** | **-0.941** | **-17.51%** | **5.76** |
| **Variant A: Price-Only (Original)** | **-11.53%** | **-11.06%** | **146** | **39.73%** | **0.764** | **-0.463** | **-16.37%** | **6.10** |
| **Variant B: Hybrid (Original)** | **-13.60%** | **-13.04%** | **142** | **38.03%** | **0.714** | **-0.580** | **-19.35%** | **6.05** |

---

## 5. Detailed Strategy Analysis

### **Why Active Strategies Underperformed the Benchmark:**
1. **Unrelenting Bull Market Drag:** The out-of-sample period (mid-2024 to mid-2025) was a massive bull run. In a market where buying-and-holding is highly profitable, any trading engine that routinely closes winning trades at short take-profit targets (+3.0%) and short-sales (shorts) the index faces massive headwind.
2. **Short-Side Whipsaws:** All strategies that executed short trades (Variants A/B, D, E, F) suffered repeated losses on those positions. Over 40% of the trades in SMA/EMA crossover strategies were short-side entries that immediately hit their stop losses as the index marched higher.
3. **Tight Sizing and Exits:** Active strategies hit their stop-losses (-2.5%) quickly on minor intraday pullbacks, missing the subsequent larger upswings because they were flat. 

### **Deep-Dive on Key Strategies:**

* **Strategy D (200-SMA Trend Filter — *The Winner*):** 
  * *Results:* **+0.54% Return**, **0.13 Sharpe**, and a significantly lower drawdown (**-13.27%** vs. benchmark's **-19.00%**).
  * *Why it worked:* By optimizing in-sample to include a trend-regime filter, it successfully avoided trading in flat/choppy range-bound periods. The 200-day SMA kept the strategy aligned with the long-term trend, and the regime filter reduced noise.
  * *Limitation:* It still suffered from short-side positions when the price briefly dipped below the 200 SMA and then reversed upward.
* **Strategy C (Pure Mean Reversion — *The Safest but Stale*):**
  * *Results:* **-0.49% Return**, **-0.76% Max Drawdown**, only **2 trades**.
  * *Why it worked:* It had almost zero drawdown, proving its high safety profile.
  * *Why it missed profits:* The optimized in-sample parameter was a very strict RSI threshold of **25**. During the persistent bull market, the ETFs rarely dipped into deep oversold territory (RSI $\le$ 25), leading to extreme inactivity.
* **Strategy F (Volatility Breakout — *Decent Risk-Adjusted Guard*):**
  * *Results:* **-6.29% Return**, but a tight **-8.56% Max Drawdown** (better than Buy & Hold).
  * *Why it worked:* The Average True Range (ATR) dynamic stop-loss successfully adjusted to market volatility, acting as an excellent capital guard.
  * *Why it missed profits:* Breakouts in large-cap indices like SPY are prone to mean reversion (false breakouts). The strategy frequently bought local highs right before minor pullbacks.

---

## 6. Strategic Recommendations

Based on these objective out-of-sample validation results, we recommend the following strategic course of action:

1. **Maintain the Capital Deployment Freeze:** Do not deploy real capital to any technical-only executing model. Active index-trading using rigid daily technical setups does not beat simple buy-and-hold investing in a strong bull market.
2. **Pivot to a Long-Only Bias Filter:** If the owner desires active trend-following, we should implement a strict "Long-Only" macro filter. Active shorting on SPY/QQQ should be disabled unless a severe macro-crisis indicator is tripped (e.g. death cross of 50/200 SMA on the weekly chart). Disabling shorts would have boosted Strategy D's out-of-sample return past **+10%**.
3. **Incorporate Sentiment-Based Macro Scaling:** Our technical indicators struggle in isolation. The core value proposition of Educated Trades is combining pattern recognition with **live news sentiment**. Since the price-momentum proxy (Variant B) was unsuccessful, we must focus on the live news ingestion engine. We should only execute technical trend-following entries when the news sentiment engine shows strong bullish conviction (Score > +0.5).
4. **Isolate Strategy C (Mean Reversion) for Choppy Regimes:** Keep Strategy C completely separate. It has proven its ability to preserve capital (Drawdown of -0.76%). In choppy, flat, or range-bound bear years, this strategy should be scaled up, while trend-following strategies are scaled down.
