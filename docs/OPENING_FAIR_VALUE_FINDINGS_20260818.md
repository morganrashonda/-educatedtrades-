# Opening fair-value, gap provenance, and inventory findings

Date: 2026-08-18
Status: retrospective research only; no production or learning changes

## Bottom line

The rolling QQQ-implied NQ fair-value residual did **not** improve the base
opening trade reliably. Raw QQQ opening-imbalance direction also remains
rejected. The useful information came from two simpler mechanism filters:

1. the NQ gap's size relative to prior-only 20-day volatility; and
2. whether the gap was created by a major 08:30 macro release or was already
   present before that release window.

The strongest new lead is not “a candle predicts the next candle.” It is a
conditional market-mechanism hypothesis: an unusually large pre-existing gap
often gives back part of its displacement when cash liquidity arrives, while a
gap newly created by scheduled information at 08:30 is more likely to be price
discovery and should not automatically be faded.

This remains a lead, not an edge claim. The volatility threshold was fitted in
the discovery block, but the 10% 08:30 provenance separator was found after
examining the data and therefore has no untouched historical holdout.

## Data and integrity

- 702 roll-clean NQ/QQQ sessions were reconstructed from 2023-08-17 through
  2026-08-17.
- The fair-value study had 109 eligible sessions with absolute NQ gaps of at
  least 1.00%; two early sessions lacked the required 40-session rolling fit.
- The QQQ NOII source contained 397,511 records, including 104,205 opening and
  248,247 closing messages.
- Fair value used only the prior 60 valid sessions; volatility used only the
  prior 20 sessions.
- Decision time remained 09:29 ET. No 09:30 cash-open value or later outcome
  entered a feature.
- Twenty-one overlapping sessions had exact one-second BBO data. Signed
  one-minute and exact-mid outcomes correlated 0.999817, with 0.685 NQ points
  mean absolute difference.
- All opening-research tests pass: 23/23.

## Frozen fair-value results

The discovery median normalized-gap threshold was 1.170437. Results below are
for fading the observed overnight gap from NQ 09:30 open through 09:31 close.

| Period | Trades | Wins | Win rate | Gross mean | Mean after 1 point | Mean without best |
|---|---:|---:|---:|---:|---:|---:|
| Discovery | 18 | 14 | 77.8% | +21.90 | +20.90 | +15.59 |
| Validation | 14 | 9 | 64.3% | +30.34 | +29.34 | +25.38 |
| Retrospective confirmation | 17 | 11 | 64.7% | +19.47 | +18.47 | +14.38 |
| All | 49 | 34 | 69.4% | +23.47 | +22.47 | +21.27 |

The all-sample 95% day-bootstrap interval for the gross mean was +11.79 to
+35.25 points. The Wilson interval for win rate was 55.5% to 80.5%. These are
historical estimates, not expected live profit.

The rolling fair-value direction itself was weaker: all-sample 60/109 wins,
+5.69 gross points, and a bootstrap interval crossing zero. The selective
residual rule and the late-rejection rule also crossed zero. They are rejected
as candidate improvements.

## 08:30 information-gap attribution

Exploratory inspection found nine high-normalized gaps where the absolute
08:30 one-minute move created at least 10% of the total gap. Every one aligned
with a verified official 08:30 macro release:

| Date | Official release | 08:30 share | Two-minute fade P&L |
|---|---|---:|---:|
| 2023-11-14 | CPI | 56.6% | +10.75 |
| 2024-02-13 | CPI | 33.4% | -3.75 |
| 2024-04-10 | CPI | 102.3% | +1.50 |
| 2024-04-25 | GDP advance | 15.3% | -29.50 |
| 2024-05-03 | Employment Situation | 46.7% | +19.00 |
| 2025-08-01 | Employment Situation | 11.4% | -5.75 |
| 2025-11-20 | delayed Employment Situation | 13.8% | +10.50 |
| 2025-12-18 | CPI | 15.0% | -35.00 |
| 2026-03-06 | Employment Situation | 30.2% | -8.00 |

The official records are the BLS archives for
[2023-11-14 CPI](https://www.bls.gov/news.release/archives/cpi_11142023.htm),
[2024-02-13 CPI](https://www.bls.gov/news.release/archives/cpi_02132024.htm),
[2024-04-10 CPI](https://www.bls.gov/news.release/archives/cpi_04102024.htm),
[2024-05-03 employment](https://www.bls.gov/news.release/archives/empsit_05032024.htm),
[2025-08-01 employment](https://www.bls.gov/news.release/archives/empsit_08012025.htm),
[2025-11-20 employment](https://www.bls.gov/news.release/archives/empsit_11202025.htm),
[2025-12-18 CPI](https://www.bls.gov/news.release/archives/cpi_12182025.htm), and
[2026-03-06 employment](https://www.bls.gov/news.release/archives/empsit_03062026.htm),
plus BEA's [2024-04-25 GDP release](https://www.bea.gov/news/2024/gross-domestic-product-first-quarter-2024-advance-estimate).

The nine event-created gaps produced 4/9 fade wins and -4.47 gross points per
trade. The other 40 produced 30/40 wins and +29.76 gross points; after a
one-point cost, +28.76. Their all-sample bootstrap interval was +16.63 to
+43.17, and mean without the best trade was +27.21.

The same direction appeared in each chronological block, but this does not
convert the result into validation because the 10% cutoff was selected after
inspection:

| Block | Non-event-created trades | Wins | Gross mean |
|---|---:|---:|---:|
| Discovery | 13 | 11 | +30.48 |
| Validation | 13 | 9 | +33.12 |
| Retrospective confirmation | 14 | 10 | +25.96 |

Federal Reserve research explains why this timestamp matters: major U.S.
releases commonly arrive at 08:30 while futures trade and cash equities are
closed, and futures lead cash price discovery over short intervals. See
[Real-Time Price Discovery in Global Markets](https://www.federalreserve.gov/pubs/ifdp/2006/871/ifdp871.htm).

## Prior-close inventory mechanism

The direct hypothesis that QQQ closing imbalance generally predicts the next
NQ overnight move was rejected:

- 701 joined next-session observations;
- all-session imbalance/overnight-return correlation: -0.044;
- inventory-direction accuracy: 38.2%;
- strong-imbalance direction accuracy: 52.7%;
- no stable discovery-to-confirmation directional relationship.

That is materially different from saying closing imbalance is useless as a
conditional label. Among absolute-gap-at-least-1% sessions with closing
imbalance at or above the discovery median, the observed gap either agreed or
disagreed with the inventory direction:

| Group | Trades | Wins | Gross mean | After 1 point | 95% bootstrap interval | Without best |
|---|---:|---:|---:|---:|---:|---:|
| Inventory-consistent | 29 | 21 | +23.00 | +22.00 | +9.38 to +36.13 | +20.44 |
| Inventory-inconsistent | 29 | 13 | +4.08 | +3.08 | crosses zero | — |

For the consistent group, discovery was weak (7 trades, +6.32; -1.46 without
best), while validation was 10/13 at +27.79 and confirmation was 7/9 at
+29.06. Exact BBO overlap was only six trades, but it was directionally
supportive: 5/6 wins, +32.86 after crossed spreads, and +25.39 without the best.

The high-normalized inventory subvariant is rejected despite attractive bar
statistics: exact overlap was only three trades, +0.54 after spread, and
negative after deleting the best trade.

The New York Fed's inventory-risk framework is relevant background, but its
recent update also shows why this mechanism cannot be assumed stable: closing
imbalance dispersion and the historical overnight drift weakened materially
after 2020. See [The Disappearing Overnight Drift](https://libertystreeteconomics.newyorkfed.org/2026/07/the-disappearing-overnight-drift/).

## Decision and next gate

No strategy is ready for Tier 3 or autonomous use.

The best next direction is a frozen shadow candidate with two independent
labels, not a larger indicator stack:

1. absolute NQ gap divided by prior-only 20-day volatility at least 1.170437;
2. record, but do not yet optimize on, the 08:30 gap-contribution ratio;
3. separately track strong prior QQQ close imbalance whose inventory direction
   agrees with the observed NQ gap;
4. enter no historical trades and place no orders—capture exact 09:30 BBO,
   spread, two-minute outcome, catalyst label, and all abstentions prospectively;
5. do not alter the thresholds until a predeclared forward sample closes.

The main causal data gap is the historical *surprise* component—actual minus
the market's pre-release consensus—for CPI, payrolls, GDP, PPI, and retail
sales. Official releases provide actual values, but a reliable point-in-time
consensus archive is still required. Until that exists, the observed 08:30
futures reaction is an event-response classifier, not proof of why the market
moved.
