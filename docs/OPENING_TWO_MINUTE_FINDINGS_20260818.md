# NQ opening two-minute findings — 2026-08-18

Status: promising post-hoc candidate; **not an approved strategy**. No Tier 3,
learning, paper-order, or production behavior changed.

## Research question

Using only information available at 09:29 ET, predict the signed NQ move from
the 09:30 one-minute bar open through the 09:31 bar close. One cash session is
one independent observation.

## Data and integrity

- 1,059,653 NQ one-minute rows and 656,146 QQQ SIP one-minute rows inspected.
- 751 common sessions; 702 usable after missing-bar, prior-session, and
  continuous-contract controls.
- zero invalid NQ/QQQ OHLC rows; NQ had zero duplicate timestamps.
- 11 otherwise-usable NQ continuous-contract transition days were excluded so
  a contract switch could not create a synthetic overnight gap.
- features end with the 09:28 bar, known at 09:29; the outcome begins at 09:30.
- minute-bar entry is a feasibility proxy. A separate event-level replay was
  used to test the proxy and observed spread.

## What failed

The frozen price-only rules did not establish an edge. NQ/QQQ late momentum,
overnight continuation, opening-extreme breakout/rejection, and the frozen
multifeature score all changed sign or failed uncertainty/cost gates across the
three chronological blocks. None should be promoted.

## The useful signal

The only stable continuous association was an inverse relationship between the
09:29 overnight displacement and the next two-minute NQ move. This motivated a
clearly **post-hoc** candidate:

> If the absolute NQ move from the prior 15:59 cash close to the 09:28 close is
> at least 1.00%, enter opposite the overnight direction at the 09:30 open proxy
> and exit at the 09:31 close.

Roll-clean retrospective results:

| Period | Trades | Win rate | Gross mean | Median | PF | Mean after 1 point cost |
|---|---:|---:|---:|---:|---:|---:|
| Discovery | 24 | 58.3% | +2.07 pts | +4.25 | 1.22 | +1.07 pts |
| Validation | 44 | 59.1% | +17.32 pts | +13.00 | 3.10 | +16.32 pts |
| Retrospective confirmation | 43 | 58.1% | +9.01 pts | +4.00 | 1.70 | +8.01 pts |
| All | 111 | 58.6% | +10.80 pts | +6.50 | 2.05 | +9.80 pts |

All-sample day-bootstrap 95% interval for the gross mean: +3.61 to +18.30
points. The chronological discovery and confirmation intervals still cross
zero, so this is not final proof. Maximum historical drawdown was 139.25 points
and the worst trade was -81 points before costs.

At one contract and a 1.0-point all-in round-trip cost, the retrospective mean
was approximately:

- NQ: +$196.08 per qualifying trade; +$21,765 over 111 trades;
- MNQ: +$19.61 per qualifying trade; +$2,176.50 over 111 trades.

Those are model outputs, not expected account returns. They assume the entry
can be obtained near the 09:30 proxy and exclude sizing, margin, taxes, broker-
specific fees beyond the stated all-in cost, and future degradation.

## Event-level check

The independent one-second NQ order-book sample contained 21 qualifying,
roll-clean sessions:

- 14/21 wins (66.7%);
- +14.83 gross points per trade;
- observed crossing-cost estimate: 1.45 points per round trip;
- +13.38 points after that crossing estimate;
- minute proxy versus event-level signed result correlation: approximately
  0.9998 in the earlier full event comparison.

This supports the minute proxy, but 21 events are too few for promotion.

## Timing and mechanism

For the 111 roll-clean large-gap sessions, the reaction was concentrated around
the cash open. In the broader threshold audit, the fade began during 09:29–09:30,
accelerated in the 09:30 and 09:31 bars, and added essentially no average return
from 09:32 through 09:35. This timing is consistent with pre-open auction-price
discovery, but does not prove causation.

Nasdaq disseminates its Net Order Imbalance Indicator before the Opening Cross.
It contains paired shares, imbalance quantity/direction, reference price, and
indicative clearing prices. Databento normalizes these messages in its
`XNAS.ITCH` `imbalance` schema. These are the correct mechanism variables to
test next, rather than adding more candle names:

- [Nasdaq Opening and Closing Crosses](https://www.nasdaqtrader.com/Trader.aspx?id=OpenClose)
- [Nasdaq historical NOII description](https://www.nasdaqtrader.com/TraderNews.aspx?id=dtn2009-059)
- [Databento auction-imbalance example](https://databento.com/docs/examples/equities/auction-imbalance)
- [Databento XNAS imbalance normalization](https://databento.com/docs/knowledge-base/datasets)

The authenticated metadata estimate for three years of QQQ `XNAS.ITCH`
imbalance data was 397,511 records and $0.663418054581. The owner subsequently
approved the purchase; the dataset was downloaded, validated, and analyzed in
`OPENING_NOII_FINDINGS_20260818.md`.

## Next falsification test

Before download, freeze these QQQ auction variables at 09:29:00 and 09:29:50:

1. signed imbalance shares divided by paired shares;
2. near indicative clearing price minus reference price;
3. change and sign persistence in those quantities from 09:28 onward;
4. whether those variables explain the large-gap fade after controlling for
   overnight displacement;
5. whether they predict which large gaps fail rather than merely restating the
   QQQ opening price.

Required outcome: the mechanism variables must improve chronological
out-of-sample expectancy and reduce false entries after realistic crossing,
slippage, and commission. If they do not, the causal auction hypothesis is
rejected and the large-gap result remains a non-causal shadow candidate only.
