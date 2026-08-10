"""End-to-end smoke test: does the whole chain actually connect?

Every other test in this repo is a unit test. Unit tests call functions
directly, so a function can be perfectly correct AND completely unreachable
and still pass. That failure mode has appeared three times in this codebase:

  * ExecutionSafety was fully implemented and imported by nothing
  * live_indicators never published the EMAs that trend_conviction needs,
    so conviction silently evaluated to 0.0 forever
  * the trading universe was hardcoded in six places, one of them the
    indicator loop

None of those were caught by unit tests. All three would have been caught by
running the real objects against a fake broker and asserting a trade happens.

So this test wires the ACTUAL TradingEngine, ExecutionSafety, PositionTruth,
BrokerExecutionAdapter and DecisionLog together -- no mocks of our own code,
only a stubbed broker at the boundary -- and walks a position from signal to
fill to exit, checking that every link recorded what it should.

Run:  python3 backend/tests/test_end_to_end.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

RESULTS = []


def chk(name, ok, got=None, exp=None):
    RESULTS.append((name, bool(ok), got, exp))


# --- stub only the broker SDK, nothing of ours -----------------------------
def install_fake_alpaca():
    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    for n in ("alpaca", "alpaca.trading", "alpaca.trading.client",
              "alpaca.data", "alpaca.data.historical", "alpaca.data.requests",
              "alpaca.data.timeframe", "alpaca.data.enums"):
        mod(n)
    req = sys.modules["alpaca.trading.requests"] = types.ModuleType(
        "alpaca.trading.requests")
    enums = sys.modules["alpaca.trading.enums"] = types.ModuleType(
        "alpaca.trading.enums")

    class _R:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    for n in ("MarketOrderRequest", "LimitOrderRequest",
              "TakeProfitRequest", "StopLossRequest"):
        setattr(req, n, type(n, (_R,), {}))
    for n, v in (("OrderSide", {"BUY": "buy", "SELL": "sell"}),
                 ("TimeInForce", {"DAY": "day", "GTC": "gtc"}),
                 ("OrderClass", {"SIMPLE": "simple", "BRACKET": "bracket"})):
        setattr(enums, n, type(n, (object,), dict(v)))
    sys.modules["alpaca.data.enums"].DataFeed = type(
        "DataFeed", (object,), {"IEX": "iex"})


install_fake_alpaca()

DATA = Path(tempfile.mkdtemp())
os.environ["DATA_DIR"] = str(DATA)
os.environ["DB_PATH"] = str(DATA / "patterns.db")
os.environ["EXECUTION_LEDGER_PATH"] = str(DATA / "ledger.db")
os.environ["DECISION_LOG_PATH"] = str(DATA / "decisions.jsonl")

import trading  # noqa: E402
from trading import (AlpacaBroker, TradingEngine, TradeSignal,  # noqa: E402
                     OrderSide, OrderStatus, AccountInfo)
from execution_safety import (ExecutionSafety, BrokerExecutionAdapter,  # noqa: E402
                              PositionTruth)
from decision_log import DecisionLog  # noqa: E402


class StubClient:
    """A broker at the API boundary. Everything above this is the real code."""

    def __init__(self):
        self.orders = {}
        self.positions = {}
        self.submitted = []
        self._seq = 0

    def get_account(self):
        return types.SimpleNamespace(
            buying_power="100000", cash="100000", portfolio_value="100000",
            long_market_value="0", short_market_value="0", equity="100000")

    def get_all_positions(self):
        return list(self.positions.values())

    def submit_order(self, order_data):
        self._seq += 1
        oid = "ord-%d" % self._seq
        self.submitted.append(order_data)
        qty = int(getattr(order_data, "qty", 0))
        symbol = getattr(order_data, "symbol", "SPY")
        order = types.SimpleNamespace(
            id=oid, status="filled", filled_qty=qty, filled_avg_price=100.0,
            order_class="simple",
            client_order_id=getattr(order_data, "client_order_id", None))
        self.orders[oid] = order
        self.positions[symbol] = types.SimpleNamespace(
            symbol=symbol, qty=float(qty), market_value=qty * 100.0,
            cost_basis=qty * 100.0, unrealized_pl=0.0, unrealized_plpc=0.0,
            avg_entry_price=100.0)
        return order

    def get_order_by_id(self, oid):
        return self.orders[oid]

    def get_order_by_client_id(self, key):
        for order in self.orders.values():
            if getattr(order, "client_order_id", None) == key:
                return order
        return None

    def get_position(self, symbol):
        if symbol not in self.positions:
            raise Exception("position does not exist")
        return self.positions[symbol]

    def close_position(self, symbol):
        self.positions.pop(symbol, None)
        return types.SimpleNamespace(id="close-%s" % symbol)


def live_engine():
    broker = AlpacaBroker(simulate=True)
    broker._simulate = False
    broker._client = StubClient()
    broker._connected = True
    engine = TradingEngine(alpaca_broker=broker, max_position_size=0.15,
                           risk_per_trade=0.005)
    TradingEngine._get_reference_price = staticmethod(lambda sym: 100.0)
    safety = ExecutionSafety(str(DATA / ("ledger-%d.db" % id(broker))))
    engine._position_truth = PositionTruth(
        safety, BrokerExecutionAdapter(broker),
        types.SimpleNamespace(load_positions=lambda: []))
    engine._journal = DecisionLog(
        path=str(DATA / ("journal-%d.jsonl" % id(broker))))
    return engine, broker, safety


def signal(symbol="SPY"):
    return TradeSignal(symbol=symbol, action="buy", conviction=0.9,
                       source="e2e", reason="end-to-end test",
                       stop_loss_pct=0.00693, take_profit_pct=0.00832)


# ===========================================================================
# 1. Entry: signal -> gate -> broker -> ledger -> journal
# ===========================================================================
eng, br, sf = live_engine()
result = eng.execute(signal())

chk("E2E-1 the order reached the broker", len(br._client.submitted) == 1,
    len(br._client.submitted), 1)
chk("E2E-2 the order filled", result.success is True, result.error, "success")
chk("E2E-3 a real fill quantity was recorded", result.filled_qty > 0,
    result.filled_qty, "> 0")
chk("E2E-4 the fill price came from the broker", result.filled_price == 100.0,
    result.filled_price, 100.0)

sent = br._client.submitted[-1]
chk("E2E-5 sizing respected the position cap",
    int(sent.qty) * 100.0 <= 100000 * 0.15 + 0.01,
    int(sent.qty) * 100.0, "<= 15000")
chk("E2E-6 a protective bracket was attached",
    getattr(sent, "order_class", None) == "bracket"
    or getattr(sent, "stop_loss", None) is not None,
    getattr(sent, "order_class", None), "bracket")

entries = [e for e in eng.journal.read() if e["event"] == "entered"]
chk("E2E-7 the entry was journalled", len(entries) == 1, len(entries), 1)
chk("E2E-8 the journal captured the sizing arithmetic",
    bool(entries and entries[0].get("sizing")), "sizing present")
chk("E2E-9 the journal captured the reference price for slippage",
    bool(entries and entries[0]["inputs"].get("reference_price")))

# ===========================================================================
# 2. Exposure: the gate must now refuse a second entry
# ===========================================================================
second = eng.execute(signal())
chk("E2E-10 a duplicate entry is refused", second.success is False,
    second.error, "blocked")
chk("E2E-11 the broker was not called again",
    len(br._client.submitted) == 1, len(br._client.submitted), 1)
chk("E2E-12 the refusal was journalled",
    any(e["event"] == "blocked" for e in eng.journal.read()))

# ===========================================================================
# 3. Exit: guarded close -> confirmed flat -> ledger resolved
# ===========================================================================
closed = eng.close_position_guarded("SPY", reason="TAKE_PROFIT")
chk("E2E-13 the guarded exit succeeded", closed.success is True,
    closed.error, "success")
chk("E2E-14 the broker position is gone",
    "SPY" not in br._client.positions)

exits = [r for r in sf._read()["orders"].values() if r.get("is_exit")]
chk("E2E-15 the exit is in the ledger", len(exits) == 1, len(exits), 1)
chk("E2E-16 a confirmed-flat exit resolves terminal",
    exits and exits[0]["status"] == "filled",
    exits[0]["status"] if exits else None, "filled")
chk("E2E-17 no unresolved exit remains",
    sf.has_unresolved_exit("SPY") is False)

# ===========================================================================
# 4. Re-entry is possible once flat
# ===========================================================================
# The 5-minute per-symbol cooldown is separate from the exposure gate and
# fires first. It never binds in practice on 30-minute bars, but it does here
# because the test runs in milliseconds -- so clear it to exercise the gate
# that this section is actually about.
chk("E2E-17b the cooldown refusal is recorded",
    any(e.get("blocker") == "cooldown" for e in eng.journal.read()),
    [e.get("blocker") for e in eng.journal.read() if e["event"] == "blocked"],
    "cooldown recorded")

# Clearing the IN-MEMORY cooldown must no longer be enough. Entries are
# written to the order ledger now, so last_entry_time() answers from disk --
# which is the whole point: a restart used to wipe the cooldown and let the
# bot immediately re-enter a symbol it had just traded. Before entries reached
# the ledger this assertion could not fail, because the query read a table
# that never had a row in it.
eng._last_trade.clear()
blocked_again = eng.execute(signal())
chk("E2E-17c the cooldown survives losing the in-memory copy",
    blocked_again.success is False and "ooldown" in (blocked_again.error or ""),
    blocked_again.error, "still on cooldown from the ledger")
chk("E2E-17d the ledger is what remembers the entry",
    sf.last_entry_time("SPY") is not None,
    sf.last_entry_time("SPY"), "a timestamp")

# Age the persisted entry past the cooldown to exercise the gate this section
# is actually about. Note this is the ONLY way to clear it now.
sf._connect().execute(
    "UPDATE orders SET created_at = created_at - 100000 WHERE is_exit = 0")
sf._connect().commit()

third = eng.execute(signal())
chk("E2E-18 a new entry is allowed after a clean exit",
    third.success is True, third.error, "allowed")
chk("E2E-19 the broker saw the new order",
    len(br._client.submitted) == 2, len(br._client.submitted), 2)

# ===========================================================================
# 5. The kill switch must stop the whole chain
# ===========================================================================
eng2, br2, sf2 = live_engine()
sf2.set_kill(True)
killed = eng2.execute(signal("QQQ"))
chk("E2E-20 the kill switch blocks entry", killed.success is False)
chk("E2E-21 no order reached the broker while killed",
    len(br2._client.submitted) == 0, len(br2._client.submitted), 0)

# ===========================================================================
# 6. A broker outage must fail closed, not trade blind
# ===========================================================================
eng3, br3, sf3 = live_engine()


def boom(symbol):
    raise ConnectionError("broker unreachable")


br3._client.get_position = boom
blocked = eng3.execute(signal("IWM"))
chk("E2E-22 an unreachable broker blocks entry", blocked.success is False,
    blocked.error, "blocked")
chk("E2E-23 nothing was submitted during the outage",
    len(br3._client.submitted) == 0, len(br3._client.submitted), 0)

# ===========================================================================
# 7. Signal generation: real bars -> real indicators -> real conviction
# ===========================================================================
# The link that broke twice. live_indicators must publish EVERYTHING that
# trend_conviction consumes, for EVERY symbol in the universe -- a missing key
# or a missing symbol produces 0.0 conviction, silently, forever.
import main as _main  # noqa: E402
import patterns as _patterns  # noqa: E402


def _trending_bars(n=250, drift=0.0015):
    closes, price = [], 100.0
    for i in range(n):
        price *= (1 + drift + 0.0004 * ((i * 7) % 5 - 2))
        closes.append(round(price, 4))
    return {
        "highs": [c * 1.001 for c in closes],
        "lows": [c * 0.999 for c in closes],
        "closes": closes,
        "opens": closes,
        "volumes": [1_000_000] * n,
        "bar_dates": [None] * n,
        "bar_timestamp": None,
    }


orch = _main.Orchestrator.__new__(_main.Orchestrator)
orch.state = _main.PipelineState()
orch._pattern_engine = types.SimpleNamespace(
    db=types.SimpleNamespace(get_recent_daily_bars=lambda s, limit=0: []))
orch._trading_engine = types.SimpleNamespace(
    broker=types.SimpleNamespace(is_simulating=False))
orch._fetch_ohlc = lambda sym, bars=0: _trending_bars()
orch.state.market_hours = {"is_open": True}
orch._compute_indicators_this_cycle(market_open=True)

ind = orch.state.live_indicators
chk("E2E-24 indicators were computed for the whole universe",
    set(ind) == set(_main.TRADING_SYMBOLS), sorted(ind), _main.TRADING_SYMBOLS)

REQUIRED = ("rsi", "adx", "regime", "ema_short", "ema_long", "volatility_pct")
missing = [k for k in REQUIRED if any(ind[s].get(k) is None for s in ind)]
chk("E2E-25 every input trend_conviction needs is published",
    not missing, missing, "none missing")

spy = ind.get("SPY", {})
conv = _patterns.trend_conviction(
    spy.get("adx"), spy.get("ema_short"), spy.get("ema_long"),
    spy.get("volatility_pct"))
chk("E2E-26 a trending series produces non-zero conviction",
    conv != 0.0, conv, "!= 0")
chk("E2E-27 an uptrend is scored bullish", conv > 0, conv, "> 0")

# ...and the mirror image, so the sign is not an accident of the fixture.
orch._fetch_ohlc = lambda sym, bars=0: _trending_bars(drift=-0.0015)
orch._compute_indicators_this_cycle(market_open=True)
down = orch.state.live_indicators["SPY"]
conv_down = _patterns.trend_conviction(
    down.get("adx"), down.get("ema_short"), down.get("ema_long"),
    down.get("volatility_pct"))
chk("E2E-28 a downtrend is scored bearish", conv_down < 0, conv_down, "< 0")

# A symbol whose data is unusable must be excluded, not published half-filled.
def _partial(sym, bars=0):
    return _trending_bars() if sym != "GLD" else {
        "highs": [1.0] * 5, "lows": [1.0] * 5, "closes": [1.0] * 5,
        "opens": [1.0] * 5, "volumes": [1] * 5, "bar_dates": [None] * 5,
        "bar_timestamp": None}


orch._fetch_ohlc = _partial
orch._compute_indicators_this_cycle(market_open=True)
chk("E2E-29 a symbol with insufficient bars is excluded entirely",
    "GLD" not in orch.state.live_indicators,
    sorted(orch.state.live_indicators), "no GLD")
chk("E2E-30 the other symbols are unaffected",
    "SPY" in orch.state.live_indicators)


# ===========================================================================
print("\n" + "=" * 72)
print("END-TO-END — real objects, stubbed broker only")
print("=" * 72)
fails = 0
for name, ok, got, exp in RESULTS:
    if ok:
        print("[PASS] %s" % name)
    else:
        fails += 1
        print("[FAIL] %s" % name)
        if got is not None or exp is not None:
            print("       got: %s" % (got,))
            print("       exp: %s" % (exp,))
print("\n%d/%d passed, %d failed" % (len(RESULTS) - fails, len(RESULTS), fails))
sys.exit(1 if fails else 0)
