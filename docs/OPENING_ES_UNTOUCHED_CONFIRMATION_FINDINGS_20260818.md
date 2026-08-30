# Untouched ES confirmation of the NQ gap-fade direction — findings, 2026-08-18

Status: complete research test, frozen protocol, result **PASS**. This confirms
direction transfer only. It does not establish a tradable ES edge and changes
no production, Tier 3, learning, or order behavior.

## Why this doc is late

The analysis ran and its report was written to
`backend/data/opening_research/opening_es_untouched_confirmation_report_3y.json`
on 2026-08-18, the same day as the rest of the Generation-1 gap-fade work. That
data directory is git-ignored (`data/` in `.gitignore`), so the completed
result never got a corresponding write-up in `docs/` alongside the other
`OPENING_*_FINDINGS_20260818.md` files from that day — the spec was frozen and
the analysis was run, but the paper trail stopped one step short. This
document closes that gap after the fact; no new data was requested or
analysis re-run to write it.

## Data

- Dataset: Databento `GLBX.MDP3`, symbol `ES.v.0` (volume-based continuous
  front contract), schema `ohlcv-1m`, continuous symbology.
- Period: 2023-08-17 through 2026-08-17.
- Source file: `backend/data/opening_research/es_1min_glbx_2023-08-17_2026-08-17.jsonl`
  — 1,060,848 source rows, 776 pre-market sessions, 773 RTH sessions.
- ES had not been loaded, inspected, or analyzed anywhere in the workspace
  before this test, per the frozen spec
  ([docs/OPENING_ES_UNTOUCHED_CONFIRMATION_SPEC.md](OPENING_ES_UNTOUCHED_CONFIRMATION_SPEC.md)).

## Build quality

| Step | Sessions |
|---|---:|
| Roll-clean, built before volatility filter | 732 |
| Excluded — continuous-contract roll transition | 12 |
| Excluded — insufficient prior volatility (< 20 valid sessions) | 20 |
| Excluded — missing prior 15:59 close | 28 |
| Excluded — no prior RTH session | 1 |
| **Usable roll-clean sessions** | **712** |

Of those, 61 sessions had an absolute 09:28 displacement ≥ 1.00%. Applying the
inherited normalized-gap threshold (1.170437, unchanged from the NQ study)
narrowed the primary confirmation sample to **41 sessions**, spanning
2023-09-19 through 2026-08-17.

## Inherited rule (unchanged from NQ)

1. 09:28 ET ES displacement from the prior 15:59 cash-session close.
2. Require absolute displacement ≥ 1.00%.
3. Normalize by close-to-close volatility over the prior 20 valid sessions.
4. Require normalized gap ≥ 1.170437 (the NQ-fitted threshold, inherited
   unchanged).
5. Trade **opposite** the overnight displacement (fade).
6. Exit at the end of the 09:31 one-minute bar.

No ES-specific threshold, macro filter, day filter, stop, target, absorption
feature, or alternative horizon was selected after seeing ES outcomes.

## Primary confirmation result (n = 41)

| Check | Result |
|---|---|
| ≥ 30 observations | 41 — pass |
| Positive gross mean | +2.82 pts — pass |
| Day-bootstrap 95% lower bound above zero | [+0.35, +5.39] — pass |
| Random-direction one-sided p < 0.05 | p = 0.0192 — pass |
| Positive mean after 1-point cost | +1.82 pts — pass |
| Positive mean after deleting the best trade | +2.225 pts — pass |
| Both chronological halves gross-positive | +3.29 (n=20) / +2.38 (n=21) — pass |
| **Decision** | **PASS** |

Win rate 53.7% (22/41), Wilson 95% interval [38.7%, 67.9%]. Gross total
+115.75 points; median +2.75. Worst single trade −9.75 points.

A blocked permutation test (calendar-quarter × prior-volatility tercile,
50,000 resamples) gave a consistent one-sided p = 0.0312 — the same-days
random-direction test is not the only randomization under which the result
holds.

### Direction matters, not just timing

The same 41 sessions under alternative rules, for context (none of these are
gated decisions — the primary gate only evaluates the fade):

| Rule | Gross mean | After 1pt cost | Win rate |
|---|---:|---:|---:|
| **Fade (the tested rule)** | **+2.82** | **+1.82** | 53.7% |
| Always long | +0.43 | −0.57 | 46.3% |
| Always short | −0.43 | −1.43 | 51.2% |
| Continuation (trade with the gap) | −2.82 | −3.82 | 43.9% |

The fade is the only one of the four that clears its own cost. Trading with
the gap (continuation) loses convincingly in the same sample — a useful sanity
check that this isn't a description of "any direction wins on big-gap days."

### Secondary sample (broader ≥1% absolute gap, n = 61, non-binding)

Included per the spec as context only; it cannot override the primary
decision above. Fade: gross +2.16, after 1pt cost +1.16, bootstrap 95% CI
[+0.13, +4.23] — directionally consistent with the primary result but with a
lower bound close to zero, as expected from a less-selective sample.

## Interpretation

> The overnight-gap-fade direction identified on NQ/QQQ transfers, unchanged,
> to ES — a different instrument, different liquidity profile, and a
> continuous-futures roll structure the NQ study never touched. That is
> evidence the mechanism is a real feature of equity-index opening behavior,
> not an artifact specific to one contract's fitting sample.

Per the frozen spec, this result **justifies acquiring broader exact
quote/order-flow coverage and specifying a delayed-entry acceptance/rejection
test** — it does not, on its own, establish a tradable edge, because OHLCV
open/close bars are not guaranteed executable BBO fills. No result from this
study changes the bot, its patterns, or its order path. It also does not
directly promote the existing NQ/QQQ gap-fade shadow-forward observer
(`opening_nq_qqq_forward.py`) — that stays gated on its own forward-evidence
review (30 initial / 60 stronger eligible sessions) — but it is corroborating
evidence for the mechanism that observer is measuring.

## Next gate

No ES-specific follow-on is proposed here, consistent with the frozen spec's
own scope: this was a direction-transfer confirmation, not the start of an ES
research track. Any future ES work would need its own frozen spec, its own
discovery/validation split, and independent purchase authorization — not an
extension of this untouched confirmation.
