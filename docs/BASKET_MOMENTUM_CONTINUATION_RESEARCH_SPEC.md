# SPY/QQQ/IWM relative-strength continuation — research spec

Status: frozen before this hypothesis's own validation/confirmation were
touched. Research only. No production, Tier 3, learning, or order-path
imports or authorization.

## Why this exists

[BASKET_DIVERGENCE_RESEARCH_SPEC.md](BASKET_DIVERGENCE_RESEARCH_SPEC.md)
tested whether a SPY/QQQ/IWM basket member that diverges from its two peers
converges back. It didn't — every cell in its discovery grid was negative,
monotonically worsening with horizon. That pattern is the mirror image of a
momentum/continuation hypothesis: the laggard keeps lagging, the leader
keeps leading, rather than reverting.

Reporting that mirror image as "discovered" from the same grid would be
exactly the look-then-decide mistake this research program's falsification
tests exist to catch (the divergence findings doc says so explicitly). So
this hypothesis gets its own frozen spec, and — critically — is scored only
on a calendar slice the reversion test's discovery stage never touched.

## Avoiding contamination

Reversion's discovery slice was the first 60% of the full 2016–2026 daily
history (ending 2022-05-26 in this run). Reversion's discovery grid never
reached a candidate that cleared its own bar, so **validation and
confirmation were never run at all** — meaning the remaining ~40% of the
calendar (2022-05-26 onward) has never had any spread-return statistic
computed on it, in either direction, by either hypothesis.

This test's entire universe is that unseen ~40% slice. Within it, the same
60/20/20 discovery/validation/confirmation split is applied fresh. Nothing
about this test's outcome was known in advance from the reversion run — only
the sign convention (informed, honestly, by reversion's failure) and the
existence of an untouched calendar window to test it on.

## Rule

Identical mechanics to the reversion spec, with one flip: for a laggard
(relative RSI very negative), `spread_return = peer_forward_return −
symbol_forward_return` (profits if the laggard keeps underperforming); for a
leader, the mirrored sign (profits if the leader keeps outperforming). Same
RSI period, same divergence-threshold and horizon grid, same cost stress,
same bootstrap and random-direction falsification methodology, same PASS /
INSUFFICIENT_EVIDENCE / FAIL gate as the reversion spec.

## Interpretation

A pass would still not authorize a trade — it would justify the same next
step reversion's spec describes (measuring genuine executable pricing before
any promotion). An INSUFFICIENT_EVIDENCE or FAIL result closes this
direction too, at least at this granularity (daily bars) and sample size.
