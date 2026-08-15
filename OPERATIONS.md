# Operating manual

Everything you need to run this, and the reasoning behind the numbers so you
can change them deliberately rather than by guess.

---

## You do not need a checklist

The startup **preflight** enforces everything that would otherwise be one, and
**refuses to trade** rather than warning into a log. It checks:

- live credentials are not writing into the paper data directory
- learning data from the superseded logic (blocks, and names the fix)
- the kill switch is not silently engaged
- indicator config cannot silently produce no signal
- the account is large enough to trade its universe
- stop/target and exposure caps are coherent

If any of those fail, the bot starts in **MANUAL** mode, places no orders, and
puts the reasons in the logs and in `state.preflight`. There is nothing to
remember and nothing to run first.

The only case it will send you to a script is contaminated learning data,
because archiving history is a decision, not a default:

```bash
python3 scripts/reset_learning.py           # dry run
python3 scripts/reset_learning.py --apply   # archive it
```

Optional, informative:

```bash
python3 scripts/repair_learning_data.py     # how much history had inverted shorts
python3 backend/tests/test_suite.py         # unit + reachability checks
python3 backend/tests/test_end_to_end.py    # full-chain integration
```

---

## Configuration

Defaults are in the code. Everything below is an environment variable.

Every setting has a working default. Nothing below is required.

| Variable | Default | Notes |
|---|---|---|
| `BAR_TIMEFRAME_MINUTES` | `30` | `1440` = daily (legacy). Everything else scales off this. |

### Paper vs live — automatic

The environment is **derived from your credentials**, not configured:

| Credentials | Environment | Data directory |
|---|---|---|
| `PK…` key, or a `paper-api` URL | paper | `$DATA_ROOT/paper` |
| `AK…` key, or an `api.alpaca.markets` URL | live | `$DATA_ROOT/live` |
| anything ambiguous | **paper** | `$DATA_ROOT/paper` |

Swapping in live keys switches the endpoint *and* the data directory together.
Paper and live never share a ledger, journal or pattern store — paper fills
are optimistic (no queue position, no partial fills, IEX-only feed), so mixing
them would contaminate the slippage measurement that real money exists to
produce.

Ambiguity always resolves to paper: guessing wrong toward paper costs nothing.

`DATA_ROOT` sets the parent (default `/home/team/shared/data`). Setting
`DATA_DIR` explicitly overrides everything, which is what the tests do.

**Do not set `DATA_DIR` in the systemd EnvironmentFile.** It overrides the
split, so paper and live share one directory and going live becomes a step
someone has to remember — the thing this exists to remove. Set `DATA_ROOT`
instead. Preflight warns if it finds `DATA_DIR` set by hand.

The resolved value is *exported* to the environment before any other module
loads. That is the whole mechanism, not a convenience: twelve modules read
`DATA_DIR` at import and each falls back to a different hardcoded default
when it is unset. Deriving it without exporting it meant the orchestrator
wrote its heartbeat to `$DATA_ROOT/paper` while the watchdog looked in
`/home/team/shared/data`, the decision journal landed in the working
directory, and paper and live shared the pattern store. Checks `DD-1`–`DD-8`
run a subprocess in the production configuration and assert all seven stores
agree; `DD-8` puts the bug back to prove the check can see it.

`health_check.py` runs under its own systemd unit and inherits nothing, so it
derives the same path from the same credentials independently.

### Risk

| Variable | Default | Reasoning |
|---|---|---|
| `STOP_LOSS_PCT` | `0.00693` | Derived: daily 2.5% × √(30/390). Volatility scales with √time. |
| `TAKE_PROFIT_PCT` | `0.00832` | Keeps the 1.2:1 shape. Breakeven win rate 45.5%. |
| `DAILY_LOSS_LIMIT_PCT` | `3.0` | Persisted — survives restart. |
| `MAX_TOTAL_EXPOSURE` | `0.30` | Portfolio-wide. Per-position caps do **not** bound correlated exposure. |
| `MAX_CONCURRENT_POSITIONS` | `3` | Raise only if the added symbols are genuinely uncorrelated. |
| `OVERNIGHT_RISK_PCT` | `0.025` | Positions held through a close are sized against gap risk, not the intraday stop. |
| `ENTRY_COOLDOWN_S` | `300` | Persisted via the ledger. |

### Universe and signals

| Variable | Default | Reasoning |
|---|---|---|
| `TRADING_SYMBOLS` | `SPY,QQQ,IWM,XLE,TLT,GLD` | The last three are cross-asset on purpose — SPY/QQQ/IWM are ~0.9 correlated and are close to one observation, not three. |
| `EMA_SHORT_PERIOD` / `EMA_LONG_PERIOD` | `20` / `50` | Deliberately **not** tuned on history. Optimising periods on past data is the fastest route to a strategy that only worked in the past. |
| `RSI_PERIOD` / `ADX_PERIOD` | `14` / `14` | Same reasoning. |
| `INDICATOR_FETCH_BARS` | `200` | EMA-50 is 0.25% off at 50 bars and 0.0005% off at 200. That error is ~30% of the conviction scale. |

### Control API

| Variable | Default | Notes |
|---|---|---|
| `API_AUTH_TOKEN` | *(none)* | **Required.** The process refuses to start without it, on every startup path. |
| `API_BIND` | `127.0.0.1` | Loopback. Only widen behind a firewall or reverse proxy. |
| `API_PORT` | `3099` | Now actually read from the environment; it used to be hardcoded. |

The API can change mode and place trades, so it is worth being blunt about
what it used to do. Authentication was written `if API_AUTH_TOKEN:` — an
unset token *disabled* auth rather than refusing to serve. The check that
required a token lived inside `Orchestrator.start()`, which `--api-only`
never calls. And the listener bound `0.0.0.0`. Together that meant
`main.py --api-only` published an unauthenticated trade-execution API to
every interface; sending `POST /api/mode {"mode":"autonomous"}` with no
header returned 200 and switched the bot to autonomous. That was confirmed
by hand before the fix and re-confirmed closed after: 401 without a token,
401 with a wrong one, connection refused off-loopback.

Auth now fails closed, the token is required before any socket binds, and
the comparison is constant-time. Checks `AU-1`–`AU-12`.

**If a dashboard talks to this from another machine it will now be refused.**
That is the intended direction — put it behind a proxy that terminates TLS
and forwards to loopback, rather than setting `API_BIND=0.0.0.0`.

### Execution quality

| Variable | Default | Reasoning |
|---|---|---|
| `MAX_SLIPPAGE_PCT_OF_TARGET` | `10` | Expressed against the target move, so it stays correct if you change timeframe. |
| `SLIPPAGE_WINDOW` | `20` | Rolling — a symbol that degrades gets blocked and can come back. |
| `SLIPPAGE_OUTLIER_MULTIPLE` | `5` | One fill this far over budget blocks immediately rather than waiting for the average. |

---

## Daily operation — also automatic

After each session the bot runs its own review and **raises alerts**; you do
not have to run anything. It checks execution cost per symbol, positions that
ran without a working stop, orders that never settled, whether the strategy is
systematically stopping out too early, and whether the forward-test gate has
been met. It also prunes settled orders from the hot ledger.

Alerts arrive by severity, so the distinction is deliberate:

| Severity | Meaning |
|---|---|
| `critical` | act now — a symbol is too expensive, a position ran unprotected, or the readiness gate changed |
| `warning` | orders did not settle by the close |
| `info` | strategy diagnoses — "stop may be too tight", "winners gave back X%" |

Run these yourself only when you want detail:

```bash
python3 backend/decision_log.py     # full postmortem, per-symbol slippage
python3 backend/forward_test.py     # readiness, with the confidence interval
```

**Read the decision journal before the P&L.** It answers the questions the P&L
cannot: why a trade was taken, why one was refused, how far a trade ran before
it closed, and whether the stop is systematically too tight.

---

## What to watch, in order

**First hour.** Check the logs for `PREFLIGHT PASSED` and the first
`INDICATOR [SPY]` lines. Then run `decision_log.py` once — the question is not
"did it trade" but *why not*. Blocked reasons should be varied. An **empty
journal means the signal path is broken**: every refusal is recorded, so
silence means nothing ever reached a gate.

**First day.**
- Trade count: expect ~5 a day across six symbols. Zero or fifty both mean something is wrong.
- Excursion scale: MFE/MAE should sit in ±0.3–1.5%. Seeing ±3% means positions are held far too long for the timeframe.
- Slippage per symbol, against the 0.083% budget.
- `unprotected_positions` should be empty.

**First week.**
- Ledger unresolved count should return to zero each session. Persistent `reserved` means a crash mid-submit; persistent `residual` means positions are not actually flattening.
- Pattern stats accumulating while `signal_strength` stays `0.0`. **This is correct and will feel wrong.** A pattern needs ~200 resolved trades before it clears the multiple-testing bar.
- Postmortem findings appearing — "stop may be too tight", "winners gave back X%".

**Do not interpret P&L for two months.** At ~5 trades a day that is roughly
where a win rate starts meaning anything.

---

## Stop immediately if

- The same symbol is closed twice, or a long flips short on exit
- `daily_loss_hit` fires more than once in a week
- Any broker position is absent from the ledger
- Entry prices do not match a real quote from that minute
- Round-trip slippage exceeds 0.083% on a symbol you keep trading

---

## Things that are true and easy to forget

**Silence is ambiguous, but diagnosable.** No trades for days is either the
confidence gate correctly refusing noise, or a broken signal path. Refusals
*with reasons* mean it is working; an empty journal means it is not.

**Paper runs on IEX data; live does not.** Paper accounts get IEX only (~2–3%
of consolidated volume). At 30-minute resolution those bars differ materially
from SIP. Paper results will not transfer exactly.

**The backtester assumes stops fill at the stop price.** They do not — a stop
becomes a market order once touched. In simulation that assumption alone was
worth 0.08% per trade, nearly three times the transaction cost.

**More trades is not more information.** Correlated symbols inflate trade count
without increasing effective sample size. That is why the universe is
cross-asset and concurrency stays at 3.

**The learner is allowed to find nothing.** The multiple-testing correction is
applied to every pattern evaluation, scaled by how many patterns exist. With
500 candidates a pattern needs z=3.88 rather than 1.96 — a 57.5% win rate over
200 trades carries weight when tested alone and **none** once you admit you
searched 15 candidates to find it. Silence means the evidence did not support
anything, which is a working outcome.

---

## Architecture, briefly

| File | Role |
|---|---|
| `execution_safety.py` | Order ledger (SQLite), kill switch, `PositionTruth` — the single authority on exposure |
| `trading.py` | Broker adapter, sizing, guarded entry/exit |
| `main.py` | Orchestrator, indicators, risk gates |
| `patterns.py` | Indicators, pattern learning, confidence bounds |
| `decision_log.py` | Decision journal, excursion analysis, execution-quality gate |
| `forward_test.py` | Go/no-go gate before live capital |
| `stats.py` | Portfolio statistics |
| `watchdog.py` | Heartbeat monitor, in-process sibling — defers to `market_clock`, debounced |
| `scripts/health_check.py` | External monitor under its own systemd unit — derives its own paths |
| `data_backup.py` | Daily snapshots, filed per environment |

Everything that gates trading — kill switch, daily loss limit, position truth,
order ledger, entry cooldown — is persisted and survives a restart. That matters
because the systemd unit uses `Restart=always`.

---

## Tests

```bash
python3 backend/tests/test_suite.py            # unit + reachability checks
python3 backend/tests/test_suite.py exec       # execution safety only
python3 backend/tests/test_suite.py learning   # learning integrity only
python3 backend/tests/test_end_to_end.py       # full-chain integration
```

No pytest, no dependencies. The end-to-end suite wires the real objects
together with only the broker SDK stubbed — it exists because unit tests cannot
catch code that is correct but unreachable, which happened three times here.

## The web dashboard and the API token

The backend requires a bearer token on every endpoint; the frontend sent no
`Authorization` header at all, so every request returned 401. It also called
`/api/reset-kill`, which the backend does not implement.

**The obvious fix would have been worse than the bug.** Adding the header to
`server/api.ts` looks right until you notice that three *client* components
imported that module directly. The token would then have been bundled into the
browser — readable by anyone who loads the page, on an API that can change mode
and place trades. Trading 401s for a published credential is a bad trade.

So the fix is architectural rather than a header:

- `server/api.ts` is **server-only** and throws if evaluated in a browser, so a
  future mistake fails loudly instead of leaking silently.
- The token comes from `process.env.API_AUTH_TOKEN`, never a literal.
- Client components reach the API through `createServerFn` wrappers in
  `server/actions.ts`. The browser talks to the dashboard server; the dashboard
  server holds the token and talks to the bot over loopback.
- CORS never applies, because the call is server-to-server. `Authorization` was
  added to the allowed headers anyway, so anything that *does* call from a
  browser fails with a real error rather than at preflight.

`FE-1`–`FE-9` cover this, including a check that no client component imports the
token-bearing module, and a vacuity check so "all calls authenticated" cannot
pass by finding zero calls. Six negative controls confirm each check fires.

Two dashboards exist: `src/` (current) and `site/` (an older copy). The README
points at `site/`. The terminal dashboard — `scripts/dashboard.py`, no Node
required — supersedes both.

## Broker failure

The most dangerous defects in this codebase were not in the happy path. They
were in what happens when the broker is unreachable — code that only runs when
something has already gone wrong, which is why it survived review.

**A disconnected live broker used to fabricate fills.** `execute_order`
dispatched on *connectivity* — `if connected: live else: simulated` — so a
live broker whose connection dropped fell through to the simulated branch and
returned an invented fill, while the audit record said `"mode": "live"`. The
system would hold a position the broker never received, with P&L, learning
data and slippage computed from a price nobody traded at. A DNS outage during
an order is enough, and one occurred in production on the first live day.

`close_position` had the same shape with a worse consequence: a phantom close
that dropped tracking while real exposure remained open and unmonitored.

Simulation is now a **mode you choose**, never a fallback. Live intent with no
broker refuses (`BR-1`–`BR-8`).

**`success` on a close now means confirmed flat.** It used to be `True`
regardless, with only `status` distinguishing FILLED from PENDING — and every
caller keys on `.success`, so an unconfirmed close was recorded as a completed
trade. An unconfirmed submission now returns `success=False` with
`close_submitted` set, so reconciliation resolves it instead of the caller
assuming it is done (`BR-9`–`BR-11`).

**Exit side follows the position.** `register_exit` was called with
`side="sell"` unconditionally, so every short exit went into the ledger
backwards — the same direction-blindness that made the learner invert every
short trade, in a different file. The signed quantity is now preserved and the
side derived from it (`XS-1`–`XS-3`).

**The order ledger is environment-segregated.** It defaulted to
`backend/data/execution_ledger.json` — inside the source tree, identical for
paper and live — and nothing set `EXECUTION_LEDGER_PATH` outside the tests, so
that path was the one in use. Paper and live would have shared one order
history. It now routes through the same derivation as every other store, and
warns if a ledger is left at the old location rather than silently orphaning
it (`LP-1`–`LP-4`).

All four have negative controls that reintroduce the original code and confirm
the checks fail. The first one took two attempts: the initial mutation
replaced the wrong branch and fell through to the refusal, so the control
passed while testing nothing.

## Concurrency

Three threads run against shared state: the pipeline, the 15-second position
monitor, and the API server. Every defect in this section was invisible to
reading and obvious to measurement — the code looks correct because each line
is correct.

| Store | Symptom, measured | State |
|---|---|---|
| Order ledger | 40 concurrent submits → 12 errors, **4 orders lost** | per-thread connections |
| Pattern database | 30 writes + 30 reads → 11 errors, **9 writes lost (30%)** | per-thread connections, WAL |
| Portfolio stats | shared connection, read from the API thread | per-thread connections |
| Status JSON files | **61.6%** of concurrent reads got a truncated file | atomic write, then rename |
| Position exit | 10 concurrent exits → **6 closes submitted** | per-symbol exit claim |
| Trade bookkeeping | 8 concurrent closes → **8 P&L milestones for one trade** | claimed by atomic DELETE |
| Position state file | 20 concurrent adds → **1 position kept, 19 lost** | lock + per-writer temp file |
| Control API | one slow request **blocked the kill switch 2.6s** | `ThreadingHTTPServer` |

The exit race was the dangerous one. A duplicate close on a long does not
merely flatten it — the surplus sell opens a short. Ten requests submitting
six closes against a 10-share long is 60 shares sold: flat, then **short 50**.
All three exit paths (the 15-second monitor, the daily-loss flatten, the kill
switch) can fire together, and are most likely to under exactly the stress
that triggers them. Exit locks are separate from entry locks, so a
liquidation never waits behind an entry's broker round-trip.

The bookkeeping race inflated the statistics the forward-test gate reads to
decide whether real money is justified. The claim is the `DELETE` itself,
checked by `rowcount`, so exactly one caller proceeds — and unlike a lock, it
holds across processes.

`position_state.py` documented `add_position()` as *"Thread-safe: loads
current state, updates, saves"* — a sequence that is the definition of not
thread-safe. A comment asserting the opposite of the truth is worse than none,
because it stops the next reader looking.

The API fix had to come **last**. Threading the server while the stores below
it were still losing writes would have converted an availability problem into
a data-loss one.

**Checked and found clean:** shared integer counters (`cycle_count`,
`signal_trade_count`, failure counters). 400,000 increments across four
threads lost none, so `+= 1` was left alone rather than wrapped in a lock for
appearance. Not every theoretical race is a real one.

`check_same_thread=False` does not make sharing safe — it only silences
SQLite's guard. `CN-4` fails if any module adds a connection without
`threading.local()`, so this cannot quietly come back.

`CN-5` fails on any method defined twice in one class. That is not
housekeeping: `close()` was defined twice with identical bodies, looked
harmless, and then silently overrode the per-thread fix applied to the first
copy. A duplicate is a trap for whoever edits the wrong one.

**The order ledger was not thread-safe, and it lost orders.** One SQLite
connection was shared by the pipeline thread, the 15-second position monitor
and the API thread, opened with `check_same_thread=False` and guarded by
nothing — the module docstring credited an advisory file lock that nothing
called and that, being per-process, would not have helped between threads
anyway. Measured: 40 concurrent submits, 12 exceptions, **36 of 40 orders in
the ledger.** Four orders vanished from the record whose entire purpose is
that no order can be lost. This was also the source of the intermittent
single-check failure that went unexplained for roughly 57 runs.

Connections are now per-thread, and a losing idempotency race rolls back so
its aborted insert cannot block the winner's commit (`TS-1`–`TS-6`).

**Startup recovery kept only one position.** `active_positions.record_id` is
the primary key and the insert is `INSERT OR REPLACE`, but recovery passed
`record_id=0` for every position adopted from the broker. Measured: three
adopted, one tracked. The other two were invisible to the stop/target monitor
and to the unprotected-position check — only the broker-side bracket still
held them. Adopted positions now get a stable id derived from the symbol
(`AD-1`–`AD-7`, with a negative control).

**Manual entry clears the same halts as automatic entry.** `POST /api/execute`
used to call the trading engine directly, so none of the orchestrator's gates
applied. Measured: an order reached the engine with the kill switch engaged,
with the daily loss limit hit, and with the operator mode set to STOPPED —
your own kill switch did not stop the API. `authorize_entry()` now gates both
paths on kill, daily loss, STOPPED, a failed preflight, and market session,
failing closed when session truth is unavailable. MANUAL mode still permits a
manual trade; that is what it is for. Manual overrides strategy, never safety
(`MA-1`–`MA-9`).

**Mode does not come from the command line.** The systemd unit passed
`--autonomous` and nothing parsed it, so the unit promised autonomous
operation while the bot started MANUAL. Mode is owned by
`{DATA_DIR}/orchestrator_mode.txt` and changed with `POST /api/mode`
(`docs/MODE_PRECEDENCE.md`). The flag is gone from the unit, and unrecognised
flags now produce a warning naming the real authority rather than being
ignored in silence (`FL-1`–`FL-3`).

**A gate that reads, then acts, is not a gate.** `can_enter()` reads exposure
and the order that follows writes it. Two threads could both see "flat" and
both submit. Measured with twelve concurrent attempts on one symbol: five
orders reached the broker, fifty shares against an intended ten. The gate
looked correct because it refused the other seven. Entry now holds a
per-symbol claim across the check and the order (`CC-1`–`CC-5`, with a
negative control that double-enters when the claim is removed).

**Entries go through the order ledger.** They used not to — exits did, entries
called the broker directly. Confirmed by running one and querying the table:
`client_order_id: <ABSENT>`, zero ledger rows. Three safeguards were reading
an empty table:

- **No idempotency key.** A submit the broker accepted but whose answer never
  came back — a timeout, a dropped connection — could be retried into a second
  real position. The entry claim stops two *simultaneous* entries; it does
  nothing about one order accepted twice. `client_order_id` is now set on the
  Alpaca request, and a repeated key reaches the broker once (`EL-5`).
- **`can_enter`'s unresolved-order check was blind on the entry side.**
- **The persisted cooldown was inert.** `last_entry_time()` queries
  `WHERE is_exit=0` — rows that were never written — so it always returned
  `None` and fell back to the in-memory value a restart clears. That was the
  bug the persistence was added to fix, still present.

The end-to-end suite now proves the last one directly: clearing the in-memory
copy is no longer enough to re-enter, because the ledger remembers
(`E2E-17c`). Before entries reached the ledger, that assertion could not have
failed.

**Some things only appear when you start it.** Booting the orchestrator once
found three defects no unit test was ever going to reach: the `--api-only`
auth bypass, `API_PORT` being ignored despite being documented as a setting,
and `--simulate` refusing to run whenever Alpaca credentials are present —
which is always, on the server. Run it before you trust it.

**A fixture that sets up the happy path can hide the bug.** Every test set
`DATA_DIR` explicitly — the one configuration in which the modules agree —
so a split-brain across seven data stores passed 656 checks. The `DD-*` and
`HC-*` checks now run a subprocess in the *production* configuration, with
nothing set, because that is the only place the failure exists.

**When you add a capability, the suite verifies it is actually called.** The
`RE-*` checks parse the codebase and fail if a safety capability becomes
unreachable from production code. That check has caught six real problems —
`ExecutionSafety` imported by nothing, `live_indicators` missing the EMAs,
the universe hardcoded past the indicator loop, the multiple-testing
correction never applied, `can_enter` ignoring its `side` argument, and
`reconcile_flat` never detecting residual positions. Passing unit tests say
nothing about reachability; those six all had passing tests.
