# QQQ NOII mechanism-test findings — 2026-08-18

Status: complete research test. The simple auction-direction causal hypothesis
failed. One stronger price-state candidate remains provisional. No production,
Tier 3, learning, or order behavior changed.

## Purchase and validation

The owner approved the Databento metadata estimate of $0.663418054581 for QQQ
Nasdaq TotalView-ITCH imbalance data covering 2023-08-17 through 2026-08-17.

- downloaded records: 397,511, exactly matching the quoted count;
- raw size: 250,221,465 bytes;
- malformed or missing-required-field records: zero;
- QQQ mappings: 397,511;
- opening-auction (`O`) records: 104,205;
- closing/extended records excluded from analysis: 293,306;
- roll-clean large-gap sessions joined to both frozen snapshots: 111/111.

The immutable raw file is
`backend/data/opening_research/qqq_xnas_imbalance_2023-08-17_2026-08-18.jsonl`.

## Frozen causal test result

The unchanged base signal fades an absolute NQ overnight displacement of at
least 1.00%. QQQ NOII variables were frozen at 09:29:00 and 09:29:50 ET using
capture-received timestamps. Four pre-registered filters tested whether the
auction supported the fade: signed imbalance, indicative near-price movement,
both, or no opposition.

The simple mechanism failed its acceptance gate. At 09:29:00, requiring
imbalance/near-price support produced:

| Chronological block | Trades | Wins | Gross mean | Mean after 1 point | Mean without best trade |
|---|---:|---:|---:|---:|---:|
| Discovery | 12 | 8 | +24.04 | +23.04 | +14.48 |
| Validation | 9 | 3 | +1.19 | +0.19 | **-8.88** |
| Retrospective confirmation | 15 | 11 | +13.93 | +12.93 | +9.64 |

The 09:29:50 support rule had the same validation failure. The relationship was
therefore not stable, had fewer than the required 20 later-block trades, and
failed the outlier-removal requirement. QQQ auction imbalance direction cannot
be claimed as the cause of the NQ reaction.

The recent event-level overlap looked strong—6/7 wins for 09:29 imbalance
support and +30.52 NQ points after estimated crossing—but it is too small and
is contradicted by the larger historical validation block.

## Diagnostic model

The frozen nine-variable diagnostic score selected 24/37 sessions in each
later block:

- validation: +14.43 gross points versus +11.78 for the unfiltered base;
- confirmation: +13.77 versus +12.59;
- both remained positive after deleting their best trade.

This does not rescue the causal claim. Post-result ablation showed that most of
the stable information came from **absolute overnight displacement**, not NOII
direction. Directional NOII correlations changed sign across chronological
blocks, and the full model did not consistently beat a model using gap size
alone.

## Stronger provisional candidate

A discovery-only one-feature score implied that predicted fade expectancy
turned positive above an absolute overnight displacement of
**1.278097837%**. Applied unchanged to the later blocks:

| Block | Trades | Gross mean | Mean after 1 point | Win rate | Mean without best trade |
|---|---:|---:|---:|---:|---:|
| Discovery | 21 | +11.31 | +10.31 | 61.9% | +5.41 |
| Validation | 26 | +14.30 | +13.30 | 61.5% | +11.08 |
| Confirmation | 21 | +21.85 | +20.85 | 66.7% | +17.89 |

Across all 68 selected sessions:

- wins: 43/68 (63.2%); Wilson 95% interval 51.4%–73.7%;
- gross mean/median: +15.71 / +11.63 NQ points;
- profit factor: 2.73;
- gross mean day-bootstrap 95% interval: +6.21 to +25.64 points;
- after a 1.0-point all-in cost: +14.71 points per trade;
- retrospective dollars at one contract: +$294.12 NQ or +$29.41 MNQ per
  qualifying trade;
- maximum historical drawdown: 91.75 points;
- worst historical trade: -59.75 points.

In the independent event-level overlap, 12 qualifying sessions produced 7
wins and +18.57 points per trade after estimated spread crossing.

This threshold is a **post-hoc simplification of a discovery-fitted model**. It
is more credible than an all-sample threshold search because it was held fixed
for two later blocks, but it is not untouched evidence. It is suitable only for
future shadow signals.

## Interpretation

The evidence supports this narrower statement:

> Large overnight displacements identify a state in which NQ has historically
> tended to reverse during the cash-open window. QQQ auction data helps explain
> why the timing is concentrated near 09:30, but QQQ imbalance direction does
> not reliably determine whether the fade succeeds.

Nasdaq's NOII is an actual opening-auction supply/demand measure, not a candle
label. However, QQQ is only one ETF auction; NQ fair value also reflects the
underlying Nasdaq-100 constituent basket and macro information. Establishing a
causal mechanism would require historically correct constituent weights and
constituent-level auction imbalances, plus scheduled macro-event attribution.

## Next gate

Freeze a research-only shadow rule before the next eligible session:

- trigger: absolute roll-clean NQ displacement greater than 1.278097837% at
  09:29 ET;
- direction: opposite the displacement;
- observation exit: 09:32 ET;
- record exact bid/ask executable prices, latency, fees, and all refusals;
- no orders, no learning writes, and no threshold changes;
- minimum initial review: 30 qualifying future sessions; stronger decision
  gate: 60 sessions.

Historical results must not be converted directly into a Tier 3 strategy.
