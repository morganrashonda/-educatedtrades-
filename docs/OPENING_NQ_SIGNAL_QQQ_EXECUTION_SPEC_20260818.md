# Frozen NQ-signal to QQQ-execution bridge test

Status: research only. This specification does not authorize Tier 3, learning,
paper orders, live orders, or changes to the running coordinator.

Frozen: 2026-08-18 before retrieving historical QQQ quote outcomes.

## Question

Can Main 5's executable instrument, QQQ, monetize the already frozen NQ
large-gap fade over the same two-minute cash-open interval after side-correct
SIP quote crossing and explicit additional slippage?

This is a cross-instrument execution-feasibility test on previously inspected
signal dates. It is not untouched edge confirmation.

## Signal

- Use exactly the 68 roll-clean NQ dates from the executable BBO audit.
- NQ gap is the 09:28 ET close versus the prior RTH 15:59 close.
- Trigger only when `abs(gap) > 1.278097837%`.
- Fade direction: positive NQ gap means short QQQ; negative NQ gap means long
  QQQ.
- No QQQ price, candle, news, macro, order-flow, or calendar input may change
  the trigger or direction.

## QQQ executable quote contract

- Source: Alpaca historical stock quotes with `feed=sip` and symbol `QQQ`.
- Each mark request uses `sort=asc` and `limit=1`, beginning exactly at the
  nominal mark. The single returned quote is therefore the first post-mark
  quote. If that quote is invalid, pagination may continue only until the first
  valid quote inside the same two-second window is found; later records are not
  retained.
- Nominal marks: 09:30:01, 09:30:06, 09:30:11, and 09:32:01 ET.
- At each nominal mark, use the first valid SIP quote stamped at or after the
  mark and no later than two seconds after it. Never use a quote from before
  the mark.
- A valid quote has finite positive bid and ask, positive bid and ask size, and
  `bid < ask`.
- Long entry crosses the ask and long exit crosses the bid.
- Short entry crosses the bid and short exit crosses the ask.
- Missing, malformed, locked, crossed, stale, or duplicate selected marks
  refuse the session. No midpoint, trade, or minute-bar substitution is
  allowed.
- Store only the selected quote and its source timestamp for each mark; do not
  retain the full quote stream.

## Costs and outputs

The SIP spread is embedded by crossing the observed quote. Report additional
round-trip slippage of 0, 1, 2, 5, and 10 cents per QQQ share. The primary gate
uses two cents per share. Report expectancy in dollars per share and example
translations for 1, 10, and 100 shares; do not call any size recommended.

No fixed commission is assumed. Alpaca's actual account and regulatory fee
schedule must be checked separately before any paper-forward proposal.

## Gates

The primary QQQ bridge passes only if all are true after the embedded spread
and two cents per share additional slippage:

- at least 50 valid sessions;
- positive mean and median;
- session-bootstrap 95% lower bound above zero;
- profit factor above 1.10;
- positive mean after deleting the best session;
- positive mean in every chronological third; and
- positive mean for both five- and ten-second delayed entries.

A pass means QQQ is a viable shadow-forward vehicle for this signal. It does
not prove an unbiased edge or authorize an order.

## Integrity and publication boundary

- Follow a pagination token only when every quote inspected so far is invalid.
  Stop at the first valid quote or when the two-second mark window is exhausted.
- Record HTTP failures and refused dates; never silently drop them.
- Never log or store Alpaca credentials.
- Raw/API response data remains local under ignored `backend/data/`.
- Research code, tests, this specification, and a compact findings document
  may later be published in a separate research-only change.
