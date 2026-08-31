# NQ-signal to QQQ executable bridge findings

Date: 2026-08-18

Status: `QQQ_EXECUTION_BRIDGE_PASS` for retrospective execution feasibility.
This does not authorize Tier 3, learning, paper orders, live orders, or a
production-code change.

## Frozen test

The test retained the 68 previously frozen, roll-clean NQ large-gap dates and
directions. It changed only the executable vehicle:

- positive NQ gap: short QQQ;
- negative NQ gap: long QQQ;
- enter using the first valid QQQ SIP quote at or after 09:30:01 ET;
- exit using the first valid quote at or after 09:32:01 ET;
- cross the ask to buy and the bid to sell;
- report five- and ten-second delayed entries; and
- subtract two additional cents per QQQ share in the primary result.

No QQQ feature changed the signal or direction.

## Data integrity

- Qualifying NQ signals: 68.
- Valid QQQ sessions: 68.
- Final refusals: 0.
- Four selected SIP quotes retained per session; full quote streams discarded.
- Initial collection completed 66 sessions. Two dates began with locked quotes
  and were recovered using the earliest subsequent valid quote inside the same
  frozen two-second window. The recovery has a separate manifest.
- Hash mismatches across selected-quote files, manifests, and report: 0.
- Mean selected-quote delay: 3.40 milliseconds.
- Maximum selected-quote delay: 36.23 milliseconds.
- Mean observed spread across marks: 4.19 cents.
- Median observed spread: 4.00 cents.
- Maximum observed spread: 14.00 cents.

Historical SIP retrieval used the Main 5 Alpaca data account and incurred no
new Databento charge.

## Primary result

After side-correct SIP spread crossing and two additional cents per share:

- Wins: 45 of 68 (`66.18%`).
- Mean: `+$0.3775 per QQQ share`.
- Median: `+$0.27 per share`.
- Total across one share on every signal: `+$25.67`.
- Session-bootstrap 95% interval for the mean: `[$0.1441, $0.6157]`.
- Profit factor: `2.6487`.
- Maximum chronological drawdown: `$2.55 per share`.
- Best session: `+$3.15 per share`.
- Worst session: `-$1.41 per share`.

Deleting the best session leaves 67 observations with a `+$0.3361` mean,
bootstrap interval `[$0.1155, $0.5622]`, and profit factor `2.4464`.

Illustrative multiplication of the historical mean is `$3.775` for 10 shares
and `$37.75` for 100 shares. These are arithmetic translations, not recommended
sizes or promised returns. At 100 shares, the worst observed trade is about
`-$141` and the historical sequential drawdown is about `$255` under the tested
cost contract.

## Stability

Chronological-third means after primary costs:

- First 22: `+$0.3455` per share.
- Middle 23: `+$0.4352` per share.
- Last 23: `+$0.3504` per share.

The first and last thirds' individual bootstrap intervals cross zero, so the
positive block means are stability diagnostics rather than independent proof.

Delayed entries remain positive:

- Five-second delay: mean `+$0.3594`, interval `[$0.1194, $0.6088]`, profit
  factor `2.5687`.
- Ten-second delay: mean `+$0.2988`, interval `[$0.0651, $0.5324]`, profit
  factor `2.1732`.

Both directions are independently positive after primary costs:

- Long QQQ: 36 sessions, 24 wins, mean `+$0.4069`, bootstrap lower bound
  `+$0.0706`, profit factor `2.7758`.
- Short QQQ: 32 sessions, 21 wins, mean `+$0.3444`, bootstrap lower bound
  `+$0.0359`, profit factor `2.5055`.

At ten additional cents per share, the result remains positive: mean
`+$0.2975`, median `+$0.19`, bootstrap interval `[$0.0641, $0.5357]`, and profit
factor `2.1567`.

## Cross-instrument relationship

The direction-adjusted NQ and QQQ trade outcomes have Pearson correlation
`0.99936`; 67 of 68 sessions have the same profit/loss sign. This is unusually
strong evidence that QQQ is capturing the same two-minute move rather than an
unrelated ETF effect.

Correlation is not causation. NQ and QQQ are both exposures to the Nasdaq-100,
and the test does not establish which market leads at every timestamp.

## Honest conclusion

The historical NQ opening-gap fade translated cleanly into the instrument Main
5 can trade. The result survived actual SIP spread crossing, additional
slippage, delayed entry, best-session deletion, long/short separation, and
chronological subdivision.

It is still retrospective and selection-biased because the NQ threshold and
strategy family were found by inspecting historical outcomes. This bridge test
answers executable vehicle feasibility; it does not replace untouched
confirmation or shadow-forward evidence.

## Next gate

Keep the rule unchanged and test it on older NQ dates not used to discover the
threshold. In parallel, implement a research-only shadow-forward observer that
records the frozen NQ decision and QQQ SIP marks without placing orders. Tier 3
design can begin after those gates, but order capability should remain disabled
until separately reviewed and authorized.
