# Educated Trades

An algorithmic trading system for US equity ETFs, built on a single premise:
**the bot must never be able to misreport what it did.**

It runs a continuous Indicators → Regime → Pattern → Execution pipeline against
the Alpaca API in paper or live mode. Every entry carries a broker-side stop and
target. Every decision — including every refusal — is written to an auditable
journal. And it will not promote itself to real capital until a forward test
says the evidence justifies it.

> **Risk notice.** Configured with live credentials, this software places real
> orders. Trading involves substantial risk of loss. Start in paper mode, and
> never deploy capital you cannot afford to lose.

---

## What it does

**Signal.** RSI, EMA(20/50) and ADX on 30-minute bars across
`SPY, QQQ, IWM, XLE, TLT, GLD`. ADX selects the regime — trend-following above
25, mean-reversion below 20, half size in the transition band. The last three
symbols are cross-asset by design: SPY, QQQ and IWM correlate at roughly 0.9,
so trading all three is closer to one observation than to three.

**Execution.** Orders pass a single authorization gate covering the kill
switch, daily loss limit, operator mode, market session, startup preflight,
per-symbol exposure, entry cooldown, and a rolling execution-quality check that
suspends any symbol whose recent slippage exceeds 10% of the target move.
Entries and exits both route through an idempotent SQLite order ledger, so an
ambiguous submission is recoverable rather than invisible.

**Learning.** Outcomes are recorded direction-aware and scored with Wilson
confidence bounds under a Šidák correction for multiple testing. A pattern needs
roughly 200 resolved trades before it carries weight. The learner is permitted
to find nothing — silence means the evidence did not support a conclusion, which
is a working outcome rather than a failure.

**What it is not.** It is not a demonstrated edge. RSI, EMA and ADX on liquid
ETFs is standard technical analysis, not a discovery. The engineering here makes
the record trustworthy; whether the strategy is profitable remains an open
question that only forward testing can answer.

---

## Requirements

- Python 3.10 or later
- Node.js 18+ and Bun — for the dashboard only, optional
- An [Alpaca](https://alpaca.markets) account; paper keys to start
- A [Finnhub](https://finnhub.io/register) key — optional, news context only

---

## Setup

```bash
git clone https://github.com/morganrashonda/-educatedtrades-.git
cd -educatedtrades-

cp .env.example .env
$EDITOR .env
```

`.env.example` documents every setting. Three are worth calling out:

| Variable | Purpose | If omitted |
|---|---|---|
| `API_AUTH_TOKEN` | **Required.** The control API can change mode and place trades. | The process refuses to start |
| `DATA_ROOT` | Parent directory for runtime data | `/var/lib/educated-trades` |
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Alpaca credentials | Runs in simulation; no real orders |

Paper and live are **derived from your credentials**, not configured separately.
A `PK…` key selects paper and writes to `$DATA_ROOT/paper`; an `AK…` key selects
live and writes to `$DATA_ROOT/live`. Anything ambiguous resolves to paper.

Do not set `DATA_DIR`. It overrides that split and places both environments in
one directory — paper fills are optimistic and IEX-only, so mixing them
contaminates the very slippage measurement that live trading exists to produce.

```bash
set -a; source .env; set +a
```

---

## Running the backend

```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python3 main.py --live       # connect to the broker
python3 main.py --simulate   # never place real orders
python3 main.py --api-only   # serve the API without the trading loop
```

**Operating mode is not settable from the command line.** It is owned by
`$DATA_DIR/orchestrator_mode.txt` and changed through `POST /api/mode`. The bot
starts in `MANUAL` unless that file says otherwise. See
[docs/MODE_PRECEDENCE.md](docs/MODE_PRECEDENCE.md).

A startup **preflight** drops the bot to MANUAL rather than trading if anything
is unsafe: live credentials pointed at a paper directory, learning data from
superseded logic, an engaged kill switch, an indicator configuration that would
silently produce no signal, or an account too small to trade its universe.

### HTTP API

Binds `127.0.0.1:3099` by default. Every endpoint requires
`Authorization: Bearer $API_AUTH_TOKEN`.

| Endpoint | Description |
|---|---|
| `GET /api/status` | System status, mode, current regime |
| `GET /api/portfolio` | Account and open positions |
| `GET /api/stats` | Win rate, expectancy, drawdown |
| `GET /api/patterns/top` | Pattern-memory summary |
| `GET /api/trades/recent` | Recent trade history |
| `POST /api/mode` | `{"mode": "manual" \| "autonomous" \| "stopped"}` |
| `POST /api/execute` | Manual entry, subject to the same safety gates |

Widen `API_BIND` only behind a firewall or a TLS-terminating proxy. This API can
place trades.

---

## Dashboard (optional)

```bash
cd site
bun install
bun run dev      # http://localhost:3000
```

---

## Safety architecture

- **Broker-side brackets** on every entry, so protection holds even if this process dies.
- **Fast-track monitor** re-checks open positions every 15 seconds, independent of the main pipeline.
- **Idempotent order ledger** — entries and exits reserve before submitting and commit the broker's own answer afterwards.
- **Persistent kill switch, daily loss limit and entry cooldown**, all of which survive a restart. This matters: the systemd unit uses `Restart=always`, so anything held only in memory is one crash away from being forgotten.
- **Decision journal** recording every entry, refusal and price excursion with its reasoning. Read it before the P&L — it answers questions the P&L cannot.
- **Forward-test gate** requiring 30 trades over 20 trading days, drawdown under 10%, and the *lower bound* of the win-rate confidence interval above 50%.

Operating detail and the reasoning behind every parameter are in
**[OPERATIONS.md](OPERATIONS.md)**.

---

## Tests

```bash
python3 backend/tests/test_suite.py        # unit, integrity, concurrency
python3 backend/tests/test_end_to_end.py   # full chain, real objects, stubbed broker
```

Neither requires pytest. The end-to-end suite wires the real components together
with only the broker SDK stubbed, because unit tests cannot detect code that is
correct but unreachable — a failure mode this codebase has produced six times.

The suite also exercises shared state under 30 to 40 concurrent threads. Those
checks found defects that extended code review did not: a shared SQLite
connection dropping orders, duplicate closes capable of flipping a long position
short, and a single trade recorded eight times. Concurrency bugs are largely
invisible to reading, because each line in isolation is correct.

---

## Security

Real credentials belong only in your local `.env`, which is git-ignored; only
`.env.example` is committed. Enable GitHub secret scanning and push protection
on any fork. Begin in paper mode and move to live keys only once you have your
own evidence.

---

## License

Copyright 2026 R Morgan.

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE), or
<http://www.apache.org/licenses/LICENSE-2.0>.

## Disclaimer

This software places real orders with a real broker when configured with live
credentials. It is provided "AS IS", without warranties or conditions of any
kind, and does not constitute investment advice.

Trading carries risk of loss. Nothing here claims this strategy is profitable.
The code is built to record honestly what happens, which is a different thing
from making money. Run it on paper until you have your own evidence, never risk
capital you cannot afford to lose, and understand that you are responsible for
anything it does with your account.
