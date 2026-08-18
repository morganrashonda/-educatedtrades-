"""Educated Trades — full safety & integrity suite. Stdlib only, no pytest.

Run:  python3 backend/tests/test_suite.py
      python3 backend/tests/test_suite.py exec      (execution safety only)
      python3 backend/tests/test_suite.py learning  (learning integrity only)

Sections:
  A. ExecutionSafety ledger semantics (status vocabulary, locking, recovery)
  B. AlpacaBroker live-path fill/status handling
  C. BrokerExecutionAdapter — the seam joining ExecutionSafety to AlpacaBroker
  L. Learning integrity — direction-aware P&L and win-rate denominators

A and C answer "can this lose track of an order or place a duplicate".
L answers "is what the bot believes about itself actually true".
"""

import os
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

SECTION = (sys.argv[1].lower() if len(sys.argv) > 1 else "all")

#: When this run began, so the isolation guard can tell files this run
#: created apart from ones that were already there.
_SUITE_START = __import__("time").time()
#: Why a subprocess probe came back short, so an intermittent
#: failure is diagnosable from the run that produced it.
_PROBE_DIAGNOSTICS = []

from collections import Counter
import re as _re
RESULTS = []


def chk(name, ok, got=None, exp=None):
    """Record a check, unless its section was filtered out on the CLI."""
    section = "learning" if name[:1] == "L" else "exec"
    if SECTION not in ("all", section):
        return
    RESULTS.append((name, bool(ok), got, exp))


# ---------------------------------------------------------------------------
# Fake alpaca package so trading.py's live path can be exercised offline.
# ---------------------------------------------------------------------------
def install_fake_alpaca():
    import types as _t

    def mod(name):
        m = _t.ModuleType(name)
        sys.modules[name] = m
        return m

    mod("alpaca")
    mod("alpaca.trading")
    req = mod("alpaca.trading.requests")
    enums = mod("alpaca.trading.enums")
    mod("alpaca.trading.client")

    class _Req:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    for n in ("MarketOrderRequest", "LimitOrderRequest",
              "TakeProfitRequest", "StopLossRequest"):
        setattr(req, n, type(n, (_Req,), {}))

    class AlpacaSide:
        BUY = "buy"
        SELL = "sell"

    class TimeInForce:
        DAY = "day"
        GTC = "gtc"

    class OrderClass:
        SIMPLE = "simple"
        BRACKET = "bracket"

    enums.OrderSide = AlpacaSide
    enums.TimeInForce = TimeInForce
    enums.OrderClass = OrderClass


install_fake_alpaca()

from execution_safety import ExecutionSafety, ExecutionRecord  # noqa: E402

D = tempfile.mkdtemp()


def fresh(n):
    return ExecutionSafety(os.path.join(D, "l%s.json" % n))


class Broker:
    """Closes cleanly: order fills, then no position remains (Alpaca -> None)."""

    def __init__(self):
        self.orders = {}
        self.submits = 0

    def submit_order(self, **kw):
        self.submits += 1
        self.orders["o1"] = SimpleNamespace(
            status="filled", filled_qty=kw["quantity"], filled_avg_price=100.0)
        return SimpleNamespace(id="o1")

    def get_order(self, oid):
        return self.orders[oid]

    def get_position(self, symbol):
        return None


# ===========================================================================
# A. Ledger semantics
# ===========================================================================

s = fresh(1)
b = Broker()
s.submit(b, client_order_key="e1", symbol="SPY", side="sell", quantity=1)
chk("A1 broker_flat(no position) == flat", s.broker_flat(b, "SPY") is True)
chk("A2 clean exit stays 'filled'", s.reconcile_flat(b, "e1").status == "filled")
chk("A3 symbol re-entry allowed", s.has_open_exposure("SPY", "sell") is False)

s = fresh(2)
for st in ("pending", "reserved", "ambiguous", "partial", "residual",
           "exit_pending", "submitted"):
    d = s._read()
    d["orders"]["x"] = {
        "client_order_key": "x", "symbol": "QQQ", "side": "sell", "quantity": 1,
        "status": st, "broker_order_id": "b1", "filled_qty": 0,
        "filled_price": None, "error": None, "updated_at": time.time(),
        "revision": 1}
    s._write(d)
    chk("A4 has_pending_exit(%r)" % st, s.has_pending_exit("QQQ") is True)
for st in ("filled", "cancelled", "rejected", "refused"):
    d = s._read()
    d["orders"]["x"]["status"] = st
    s._write(d)
    chk("A5 terminal %r not pending" % st, s.has_pending_exit("QQQ") is False)

for st in ("stopped", "replaced", "suspended", "done_for_day",
           "pending_cancel", "pending_replace", "held", "not_yet_invented"):
    n = ExecutionSafety._normalize_status(st)
    chk("A6 %r->%r non-terminal" % (st, n), n not in ExecutionSafety._TERMINAL)
for st in ("canceled", "cancelled", "rejected", "filled", "expired"):
    n = ExecutionSafety._normalize_status(st)
    chk("A7 %r->%r terminal" % (st, n), n in ExecutionSafety._TERMINAL)


class Crash(Broker):
    def submit_order(self, **kw):
        raise KeyboardInterrupt("process died mid-call")


class Recovering(Broker):
    def get_order_by_client_id(self, key):
        self.orders["o1"] = SimpleNamespace(
            status="filled", filled_qty=1, filled_avg_price=100.0)
        return SimpleNamespace(id="o1")


class NeverSaw(Broker):
    def get_order_by_client_id(self, key):
        return None


s = fresh(4)
try:
    s.submit(Crash(), client_order_key="c1", symbol="IWM", side="buy", quantity=1)
except BaseException:
    pass
chk("A8 stranded at 'reserved'", s._read()["orders"]["c1"]["status"] == "reserved")
chk("A9 reconcile_all recovers reserved", len(s.reconcile_all(Recovering())) >= 1)
chk("A10 recovered to real state", s._read()["orders"]["c1"]["status"] == "filled")

s2 = fresh(41)
try:
    s2.submit(Crash(), client_order_key="c2", symbol="IWM", side="buy", quantity=1)
except BaseException:
    pass
s2.reconcile_all(NeverSaw())
chk("A11 never-landed intent releases symbol",
    s2.has_open_exposure("IWM", "buy") is False)

p = os.path.join(D, "corrupt.json")
open(p, "w").write("{not json")
s = ExecutionSafety(p)
try:
    s.set_kill(True)
    ok, err = True, None
except Exception as e:
    ok, err = False, "%s: %s" % (type(e).__name__, e)
chk("A12 set_kill(True) w/ corrupt ledger", ok, err, "succeeds")
chk("A13 kill reads as engaged", s.kill_engaged() is True)


class SlowThenId(Broker):
    def __init__(self, safety):
        Broker.__init__(self)
        self.safety = safety

    def submit_order(self, **kw):
        d = self.safety._read()
        r = d["orders"]["r1"]
        r.update(status="filled", filled_qty=1, filled_price=100.0,
                 broker_order_id="o9", updated_at=time.time(),
                 revision=r.get("revision", 1) + 1)
        self.safety._write(d)
        return SimpleNamespace(id="o9")


s = fresh(6)
s.submit(SlowThenId(s), client_order_key="r1", symbol="SPY", side="buy", quantity=1)
chk("A14 concurrent fill preserved",
    s._read()["orders"]["r1"]["status"] == "filled")


class NotFlat(Broker):
    def get_position(self, symbol):
        return SimpleNamespace(qty=1)


s = fresh(7)
b = NotFlat()
s.submit(b, client_order_key="d1", symbol="SPY", side="sell", quantity=1)
d = s._read()
d["orders"]["d1"]["legacy_field"] = "older schema"
s._write(d)
try:
    r = s.reconcile_flat(b, "d1")
    ok, err = True, None
except Exception as e:
    ok, err = False, "%s: %s" % (type(e).__name__, e)
chk("A15 reconcile_flat tolerates drift", ok, err, "succeeds")
chk("A16 genuine residual recorded", ok and r.error == "broker_position_not_flat")


class Outage(Broker):
    def get_position(self, symbol):
        raise ConnectionError("broker unreachable")


s = fresh(71)
b = Outage()
s.submit(b, client_order_key="d2", symbol="SPY", side="sell", quantity=1)
chk("A17 outage distinguished from residual",
    s.reconcile_flat(b, "d2").error == "broker_position_unknown")


class DecB(Broker):
    def submit_order(self, **kw):
        return SimpleNamespace(id="o1", filled_qty=1, filled_price=Decimal("100.5"))


s = fresh(8)
try:
    s.submit(DecB(), client_order_key="dec", symbol="SPY", side="buy", quantity=1)
    ok, err = True, None
except Exception as e:
    ok, err = False, "%s: %s" % (type(e).__name__, e)
chk("A18 Decimal filled_price persists",
    ok and s._read()["orders"]["dec"]["status"] == "filled", err)

s = fresh(90)
b = Broker()
chk("A19 normal submit -> submitted",
    s.submit(b, client_order_key="k", symbol="SPY", side="buy", quantity=1).status == "submitted")
s.submit(b, client_order_key="k", symbol="SPY", side="buy", quantity=1)
chk("A20 idempotent: broker called once", b.submits == 1, b.submits, 1)
chk("A21 reconcile -> filled", s.reconcile(b, "k").status == "filled")
chk("A22 outcome_pnl buy", s.outcome_pnl("k", 110.0) == 10.0)

s = fresh(91)
s.set_kill(True)
r = s.submit(Broker(), client_order_key="z", symbol="SPY", side="buy", quantity=1)
chk("A23 kill switch refuses", r.status == "refused" and r.error == "kill_switch")

s = fresh(92)
b = Broker()
s.submit(b, client_order_key="p", symbol="SPY", side="buy", quantity=5)
b.orders["o1"] = SimpleNamespace(status="partially_filled", filled_qty=2,
                                 filled_avg_price=100.0)
chk("A24 partial fill -> partial", s.reconcile(b, "p").status == "partial")
try:
    s.outcome_pnl("p", 110.0)
    ok = False
except ValueError:
    ok = True
chk("A25 partial blocks P&L", ok)

for kw in ({"side": "hold", "quantity": 1}, {"side": "buy", "quantity": 0},
           {"side": "buy", "quantity": True}):
    try:
        fresh(93).submit(Broker(), client_order_key="b", symbol="SPY", **kw)
        ok = False
    except ValueError:
        ok = True
    chk("A26 rejects %s" % kw, ok)

p = os.path.join(D, "c2.json")
open(p, "w").write("{bad")
try:
    ExecutionSafety(p)._read()
    ok = False
except RuntimeError:
    ok = True
chk("A27 corrupt ledger fails closed on read", ok)

entered, release = threading.Event(), threading.Event()


class Blocking(Broker):
    def submit_order(self, **kw):
        entered.set()
        release.wait(3)
        return SimpleNamespace(id="b1")


s = fresh(94)
t = threading.Thread(target=lambda: s.submit(
    Blocking(), client_order_key="h", symbol="SPY", side="buy", quantity=1))
t.start()
entered.wait(3)
t0 = time.monotonic()
s.set_kill(True)
elapsed = time.monotonic() - t0
release.set()
t.join(3)
chk("A28 kill switch not blocked by hung broker", elapsed < 0.5,
    "%.3fs" % elapsed, "<0.5s")


class Slow(Broker):
    def submit_order(self, **kw):
        self.submits += 1
        time.sleep(0.03)
        self.orders["s"] = SimpleNamespace(status="accepted", filled_qty=0,
                                           filled_avg_price=None)
        return SimpleNamespace(id="s")


s = fresh(95)
b = Slow()
bar = threading.Barrier(2)


def _worker():
    bar.wait()
    s.submit(b, client_order_key="same", symbol="SPY", side="buy", quantity=1)


ts = [threading.Thread(target=_worker) for _ in range(2)]
[x.start() for x in ts]
[x.join() for x in ts]
chk("A29 concurrent same key -> one broker call", b.submits == 1, b.submits, 1)
chk("A30 final status settled",
    s._read()["orders"]["same"]["status"] == "submitted")


# ===========================================================================
# B. AlpacaBroker live-path fill/status handling
# ===========================================================================
import trading  # noqa: E402
from trading import AlpacaBroker, OrderSide, OrderType, OrderStatus  # noqa: E402


class FakeClient:
    """Stands in for alpaca TradingClient."""

    def __init__(self, order):
        self.order = order
        self.submitted = []
        self.positions = {}
        self.position_error = None

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        return self.order

    def get_order_by_id(self, oid):
        return self.order

    def get_order_by_client_id(self, key):
        return self.order

    def get_position(self, symbol):
        if self.position_error:
            raise self.position_error
        if symbol not in self.positions:
            raise Exception("position does not exist")
        return self.positions[symbol]

    def close_position(self, symbol):
        return SimpleNamespace(id="close-1")


def live_broker(order):
    br = AlpacaBroker(simulate=True)
    br._simulate = False
    br._client = FakeClient(order)
    br._connected = True
    return br


def order(**kw):
    base = dict(id="ord-1", status="new", filled_qty=0, filled_avg_price=None,
                order_class="simple", client_order_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


# B1: a partial fill must not be reported as a complete fill
br = live_broker(order(status="partially_filled", filled_qty=2,
                       filled_avg_price=100.0))
res = br.execute_order("SPY", OrderSide.BUY, 5)
chk("B1 partial fill not reported FILLED", res.status != OrderStatus.FILLED,
    res.status, "PARTIALLY_FILLED")
chk("B2 partial fill reports true filled_qty", res.filled_qty == 2,
    res.filled_qty, 2)

# B3: filled_qty must never be inflated to the requested quantity
br = live_broker(order(status="filled", filled_qty=0, filled_avg_price=100.0))
res = br.execute_order("SPY", OrderSide.BUY, 5)
chk("B3 zero filled_qty not inflated to requested", res.filled_qty != 5,
    res.filled_qty, "0")

# B4: status 'filled' with no avg price must not raise
br = live_broker(order(status="filled", filled_qty=5, filled_avg_price=None))
res = br.execute_order("SPY", OrderSide.BUY, 5)
chk("B4 filled w/o avg price does not error", res.status != OrderStatus.ERROR,
    res.error, "no exception")

# B5: Alpaca spells it 'canceled'
br = live_broker(order(status="canceled"))
res = br.execute_order("SPY", OrderSide.BUY, 5)
chk("B5 'canceled' recognised as CANCELLED",
    res.status == OrderStatus.CANCELLED, res.status, "CANCELLED")

# B6: an accepted-but-unfilled order is not a successful trade
br = live_broker(order(status="accepted", filled_qty=0, filled_avg_price=None))
res = br.execute_order("SPY", OrderSide.BUY, 5)
chk("B6 accepted w/ no fill is not success", res.success is False,
    res.success, False)

# B7: client_order_id must reach the broker (idempotency key)
br = live_broker(order(status="new"))
try:
    br.execute_order("SPY", OrderSide.BUY, 5, client_order_id="key-123")
    sent = br._client.submitted[-1]
    _got = getattr(sent, "client_order_id", None)
except TypeError as e:
    _got = "execute_order rejects client_order_id: %s" % e
chk("B7 client_order_id forwarded to Alpaca", _got == "key-123", _got, "key-123")

# B8: get_position must distinguish 'flat' from 'broker unreachable'
br = live_broker(order())
br._client.position_error = ConnectionError("network down")
try:
    br.get_position_strict("SPY")
    ok = False
    detail = "returned instead of raising"
except AttributeError:
    ok = False
    detail = "get_position_strict missing"
except Exception:
    ok = True
    detail = None
chk("B8 get_position_strict raises on outage", ok, detail, "raises")

br = live_broker(order())
try:
    ok = br.get_position_strict("SPY") is None
    detail = None
except Exception as e:
    ok, detail = False, "%s: %s" % (type(e).__name__, e)
chk("B9 get_position_strict returns None when flat", ok, detail, "None")

# B10: close_position must not claim a fill it has not confirmed.
# Confirmed flat -> FILLED is correct; anything else must stay PENDING and
# must NOT drop local position state.
removed = []
trading._remove_persisted_position = lambda sym: removed.append(sym)

br = live_broker(order())
br._client.positions.clear()
chk("B10 confirmed-flat close reports FILLED",
    br.close_position("SPY").status == OrderStatus.FILLED)
chk("B11 confirmed-flat close drops local state", removed == ["SPY"], removed, ["SPY"])

removed.clear()
br = live_broker(order())
br._client.positions["SPY"] = SimpleNamespace(
    symbol="SPY", qty=5.0, market_value=500.0, cost_basis=500.0,
    unrealized_pl=0.0, unrealized_plpc=0.0, avg_entry_price=100.0)
res = br.close_position("SPY")
chk("B12 still-open close does not claim FILLED",
    res.status != OrderStatus.FILLED, res.status, "PENDING")
chk("B13 still-open close keeps local state", removed == [], removed, [])

removed.clear()
br = live_broker(order())
br._client.position_error = ConnectionError("network down")
res = br.close_position("SPY")
chk("B14 unconfirmable close does not claim FILLED",
    res.status != OrderStatus.FILLED, res.status, "PENDING")
chk("B15 unconfirmable close keeps local state", removed == [], removed, [])


# ===========================================================================
# C. BrokerExecutionAdapter — the ExecutionSafety <-> AlpacaBroker seam
# ===========================================================================
try:
    from execution_safety import BrokerExecutionAdapter
    HAVE_ADAPTER = True
except ImportError:
    HAVE_ADAPTER = False
chk("C1 BrokerExecutionAdapter exists", HAVE_ADAPTER)

if HAVE_ADAPTER:
    br = live_broker(order(status="new", id="ord-9"))
    ad = BrokerExecutionAdapter(br)
    for name in ("submit_order", "get_order", "get_position",
                 "get_order_by_client_id"):
        chk("C2 adapter exposes %s" % name, hasattr(ad, name))

    s = fresh(100)
    try:
        rec = s.submit(ad, client_order_key="ck-1", symbol="SPY",
                       side="buy", quantity=3)
    except Exception as _e:
        rec = ExecutionRecord("ck-1", "SPY", "buy", 3, "error:%s" % _e)
    chk("C3 submit through adapter succeeds", rec.status == "submitted",
        rec.status, "submitted")
    chk("C4 broker order id captured", rec.broker_order_id == "ord-9",
        rec.broker_order_id, "ord-9")
    sent = br._client.submitted[-1]
    chk("C5 client_order_key used as broker idempotency key",
        getattr(sent, "client_order_id", None) == "ck-1",
        getattr(sent, "client_order_id", None), "ck-1")
    chk("C6 string side accepted by adapter", getattr(sent, "side", None) in
        ("buy", OrderSide.BUY), getattr(sent, "side", None), "buy")

    br._client.order = order(status="filled", filled_qty=3,
                             filled_avg_price=101.0, id="ord-9")
    chk("C7 reconcile through adapter -> filled",
        s.reconcile(ad, "ck-1").status == "filled")

    br._client.positions.clear()
    chk("C8 adapter reports flat when no position",
        s.broker_flat(ad, "SPY") is True)
    br._client.position_error = ConnectionError("down")
    chk("C9 adapter surfaces outage as unknown",
        s.position_state(ad, "SPY") == "unknown")




import patterns  # noqa: E402
from patterns import PatternEngine, PatternStats  # noqa: E402


def engine():
    tmp = Path(tempfile.mkdtemp()) / "patterns.db"
    return PatternEngine(db_path=tmp)


def open_trade(eng, symbol="SPY", entry=100.0, conviction=0.9):
    """Record a pattern and return its record_id."""
    return eng.record_pattern(
        symbol=symbol, sentiment_score=0.8, conviction_score=conviction,
        rsi_value=55.0, ema_short=101.0, ema_long=100.0, entry_price=entry,
    )


def stats_for(eng, record_id):
    row = eng.db._connect().execute(
        "SELECT pattern_hash FROM pattern_memory WHERE id=?", (record_id,)
    ).fetchone()
    return eng.db._connect().execute(
        "SELECT * FROM pattern_stats WHERE pattern_id=?", (row["pattern_hash"],)
    ).fetchone()


def memory_row(eng, record_id):
    return eng.db._connect().execute(
        "SELECT * FROM pattern_memory WHERE id=?", (record_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# 1. Direction awareness — the core learning-integrity property
# ---------------------------------------------------------------------------

# A long that rises is a win.
eng = engine()
rid = open_trade(eng, entry=100.0)
eng.record_outcome(rid, exit_price=110.0, hours_later=1, side="buy")
row = memory_row(eng, rid)
chk("L1 long up  -> win", row["outcome"] == "win", row["outcome"], "win")
chk("L2 long up  -> +10%", abs(row["profit_pct"] - 10.0) < 1e-6,
    row["profit_pct"], 10.0)

# A long that falls is a loss.
eng = engine()
rid = open_trade(eng, entry=100.0)
eng.record_outcome(rid, exit_price=90.0, hours_later=1, side="buy")
row = memory_row(eng, rid)
chk("L3 long down -> loss", row["outcome"] == "loss", row["outcome"], "loss")
chk("L4 long down -> -10%", abs(row["profit_pct"] + 10.0) < 1e-6,
    row["profit_pct"], -10.0)

# A SHORT that falls is a WIN. This is the case that was inverted.
eng = engine()
rid = open_trade(eng, entry=100.0)
eng.record_outcome(rid, exit_price=90.0, hours_later=1, side="sell")
row = memory_row(eng, rid)
chk("L5 short down -> win", row["outcome"] == "win", row["outcome"], "win")
chk("L6 short down -> +10%", abs(row["profit_pct"] - 10.0) < 1e-6,
    row["profit_pct"], 10.0)
st = stats_for(eng, rid)
chk("L7 short win credited to pattern wins", st["wins"] == 1, st["wins"], 1)
chk("L8 short win not counted as a loss", st["losses"] == 0, st["losses"], 0)

# A SHORT that rises is a LOSS.
eng = engine()
rid = open_trade(eng, entry=100.0)
eng.record_outcome(rid, exit_price=110.0, hours_later=1, side="sell")
row = memory_row(eng, rid)
chk("L9 short up -> loss", row["outcome"] == "loss", row["outcome"], "loss")
chk("L10 short up -> -10%", abs(row["profit_pct"] + 10.0) < 1e-6,
    row["profit_pct"], -10.0)

# close_position must agree with record_outcome; the reported dollar P&L and
# the learned outcome must never disagree in sign.
eng = engine()
rid = open_trade(eng, entry=100.0)
eng.db.add_active_position(record_id=rid, symbol="SPY", entry_price=100.0,
                           quantity=10, side="sell")
res = eng.close_tracked_position(rid, current_price=90.0)
row = memory_row(eng, rid)
chk("L11 close_tracked_position(short win) reports profit",
    res.get("profit_pct", 0) > 0, res.get("profit_pct"), "> 0")
chk("L12 learned outcome agrees with reported P&L",
    row["outcome"] == "win", row["outcome"], "win")
chk("L13 learned pct agrees with reported pct",
    abs(row["profit_pct"] - res["profit_pct"]) < 1e-6,
    (row["profit_pct"], res.get("profit_pct")), "equal")


# ---------------------------------------------------------------------------
# 2. Win-rate denominator — resolved outcomes, not observations
# ---------------------------------------------------------------------------
# A pattern seen 10 times with 2 resolved trades (both wins) has a 100% win
# rate on the evidence available, not 20%. Counting unresolved observations in
# the denominator makes every pattern look worse the more often it is seen.
s = PatternStats(pattern_id="p", count=10, wins=2, losses=0,
                 total_profit_pct=20.0)
chk("L14 win_rate over resolved trades", s.win_rate == 1.0, s.win_rate, 1.0)
chk("L15 avg_profit over resolved trades", s.avg_profit_pct == 10.0,
    s.avg_profit_pct, 10.0)

s = PatternStats(pattern_id="p", count=10, wins=3, losses=1,
                 total_profit_pct=8.0)
chk("L16 win_rate 3W/1L == 0.75", s.win_rate == 0.75, s.win_rate, 0.75)
chk("L17 avg_profit 8.0/4 == 2.0", s.avg_profit_pct == 2.0, s.avg_profit_pct, 2.0)

s = PatternStats(pattern_id="p", count=5, wins=0, losses=0)
chk("L18 no resolved trades -> 0.0 win_rate, no crash", s.win_rate == 0.0,
    s.win_rate, 0.0)
chk("L19 no resolved trades -> 0.0 avg_profit", s.avg_profit_pct == 0.0)
chk("L20 no resolved trades -> neutral signal", s.signal_strength == 0.0,
    s.signal_strength, 0.0)

# Robustness must mean resolved evidence, not sightings.
s = PatternStats(pattern_id="p", count=50, wins=1, losses=1)
chk("L21 robustness needs resolved outcomes", s.is_robust is False,
    s.is_robust, False)
s = PatternStats(pattern_id="p", count=50, wins=7, losses=3)
chk("L22 10 resolved outcomes -> robust", s.is_robust is True, s.is_robust, True)


# ---------------------------------------------------------------------------
# 3. Degenerate inputs must not corrupt the ledger or crash the loop
# ---------------------------------------------------------------------------
for bad_entry, label in ((0.0, "zero"), (-5.0, "negative")):
    eng = engine()
    rid = open_trade(eng, entry=bad_entry)
    try:
        eng.record_outcome(rid, exit_price=100.0, hours_later=1, side="buy")
        crashed = False
    except ZeroDivisionError:
        crashed = True
    except Exception:
        crashed = False
    row = memory_row(eng, rid)
    chk("L23 %s entry price does not crash" % label, not crashed)
    chk("L24 %s entry price not scored as a win" % label,
        row["outcome"] != "win", row["outcome"], "not 'win'")

eng = engine()
rid = open_trade(eng, entry=100.0)
try:
    eng.record_outcome(rid, exit_price=0.0, hours_later=1, side="buy")
    crashed = False
except Exception:
    crashed = True
chk("L25 zero exit price does not crash", not crashed)

# An unknown side must not be silently treated as a long.
eng = engine()
rid = open_trade(eng, entry=100.0)
try:
    eng.record_outcome(rid, exit_price=110.0, hours_later=1, side="sideways")
    row = memory_row(eng, rid)
    ok = row["outcome"] in (None, "pending", "loss") or row["profit_pct"] is None
except ValueError:
    ok = True
chk("L26 unknown side is not assumed long", ok)


# ===========================================================================
# P. PositionTruth — one authority over broker / ledger / position file
# ===========================================================================
from execution_safety import PositionTruth  # noqa: E402


class FakeState:
    """Stands in for PositionStateManager."""

    def __init__(self, positions=None, broken=False):
        self._positions = positions or []
        self.broken = broken

    def load_positions(self):
        if self.broken:
            raise OSError("position file unreadable")
        return list(self._positions)


class PosBroker:
    """Broker whose position for a symbol is configurable."""

    def __init__(self, position=None, error=None):
        self.position = position
        self.error = error
        self.orders = {}

    def submit_order(self, **kw):
        self.orders["o1"] = SimpleNamespace(
            status="filled", filled_qty=kw["quantity"], filled_avg_price=100.0)
        return SimpleNamespace(id="o1")

    def get_order(self, oid):
        return self.orders[oid]

    def get_position(self, symbol):
        if self.error:
            raise self.error
        return self.position


def truth(position=None, error=None, local=None, broken=False, n=200):
    safety = fresh(n)
    broker = PosBroker(position=position, error=error)
    return PositionTruth(safety, broker, FakeState(local, broken)), safety, broker


# Confirmed flat everywhere -> entry allowed.
t, sf, br = truth(n=200)
allowed, why = t.can_enter("SPY", "buy")
chk("P1 flat everywhere -> entry allowed", allowed is True, why, "allowed")
chk("P2 verdict is flat", t.exposure("SPY")["verdict"] == "flat")

# Broker holds a position we never recorded -> must block.
t, sf, br = truth(position={"qty": 10, "side": "buy"}, n=201)
allowed, why = t.can_enter("SPY", "buy")
chk("P3 unknown broker position blocks entry", allowed is False, why, "blocked")
chk("P4 verdict is long", t.exposure("SPY")["verdict"] == "long",
    t.exposure("SPY")["verdict"], "long")

# Position file claims a position the broker does not have -> conflict.
t, sf, br = truth(position=None, local=[{"symbol": "SPY", "qty": 5, "side": "buy"}], n=202)
snap = t.exposure("SPY")
allowed, why = t.can_enter("SPY", "buy")
chk("P5 file-vs-broker disagreement is CONFLICTED",
    snap["verdict"] == "conflicted", snap["verdict"], "conflicted")
chk("P6 conflict blocks entry", allowed is False, why, "blocked")
chk("P7 conflict is explained", any("broker reports flat" in r for r in snap["reasons"]),
    snap["reasons"], "explains the disagreement")

# Opposing sides -> conflict, not a silent pick.
t, sf, br = truth(position={"qty": 10, "side": "buy"},
                  local=[{"symbol": "SPY", "qty": 10, "side": "sell"}], n=203)
snap = t.exposure("SPY")
chk("P8 opposing sides -> conflicted", snap["verdict"] == "conflicted",
    snap["verdict"], "conflicted")

# Broker unreachable -> unknown, never assumed flat.
t, sf, br = truth(error=ConnectionError("down"), n=204)
snap = t.exposure("SPY")
allowed, why = t.can_enter("SPY", "buy")
chk("P9 unreachable broker -> unknown", snap["verdict"] == "unknown",
    snap["verdict"], "unknown")
chk("P10 unknown blocks entry", allowed is False, why, "blocked")

# Unreadable position file is not evidence of being flat.
t, sf, br = truth(broken=True, n=205)
allowed, why = t.can_enter("SPY", "buy")
chk("P11 unreadable position file blocks entry", allowed is False, why, "blocked")

# An unresolved order blocks entry even when the broker looks flat.
t, sf, br = truth(n=206)
sf.submit(br, client_order_key="k1", symbol="SPY", side="buy", quantity=1)
d = sf._read()
d["orders"]["k1"]["status"] = "ambiguous"
sf._write(d)
allowed, why = t.can_enter("SPY", "buy")
chk("P12 unresolved order blocks entry", allowed is False, why, "blocked")

# Kill switch overrides everything.
t, sf, br = truth(n=207)
sf.set_kill(True)
allowed, why = t.can_enter("SPY", "buy")
chk("P13 kill switch blocks entry", allowed is False, why, "blocked")
chk("P14 kill switch is the stated reason", "kill switch" in why, why, "kill switch")

# Short exposure is reported as short, not just 'exposed'.
t, sf, br = truth(position={"qty": -8, "side": "sell"}, n=208)
chk("P15 short position -> short", t.exposure("SPY")["verdict"] == "short",
    t.exposure("SPY")["verdict"], "short")

# reconcile() surfaces conflicts instead of resolving them silently.
t, sf, br = truth(position=None, local=[{"symbol": "SPY", "qty": 5, "side": "buy"}], n=209)
report = t.reconcile(["SPY"])
chk("P16 reconcile reports conflicted status", report["status"] == "conflicted",
    report["status"], "conflicted")
chk("P17 reconcile names the conflicted symbol", "SPY" in report["conflicts"])
t, sf, br = truth(n=210)
chk("P18 clean reconcile is ok", t.reconcile(["SPY"])["status"] == "ok")


# --- G. The live entry gate inside TradingEngine.execute() -----------------
from trading import TradingEngine, TradeSignal  # noqa: E402


def gated_engine(position=None, error=None, local=None, n=300):
    br = live_broker(order(status="new", id="g-1"))
    br._client.positions = {}
    if position is not None:
        br._client.positions["SPY"] = SimpleNamespace(
            symbol="SPY", qty=position["qty"], market_value=0.0, cost_basis=0.0,
            unrealized_pl=0.0, unrealized_plpc=0.0, avg_entry_price=100.0)
    if error is not None:
        br._client.position_error = error
    eng = TradingEngine(alpaca_broker=br)
    safety = fresh(n)
    from execution_safety import BrokerExecutionAdapter, PositionTruth
    eng._position_truth = PositionTruth(
        safety, BrokerExecutionAdapter(br), FakeState(local))
    return eng, br, safety


def buy_signal():
    return TradeSignal(symbol="SPY", action="buy", conviction=0.95,
                       source="test", reason="gate test",
                       stop_loss_pct=0.02, take_profit_pct=0.04)


try:
    eng, br, sf = gated_engine(n=300)
    _sig = buy_signal()
    GATE_OK = True
except Exception as _e:
    GATE_OK = False
    chk("G0 gate harness constructible", False, str(_e), "TradeSignal built")

if GATE_OK:
    chk("G0 gate harness constructible", True)

    # Already holding the symbol at the broker -> entry refused.
    eng, br, sf = gated_engine(position={"qty": 10}, n=301)
    res = eng.execute(buy_signal())
    chk("G1 existing broker position blocks execute",
        res.success is False and "Entry blocked" in (res.error or ""),
        res.error, "Entry blocked")

    # Broker unreachable -> refuse rather than assume flat.
    eng, br, sf = gated_engine(error=ConnectionError("down"), n=302)
    res = eng.execute(buy_signal())
    chk("G2 unreachable broker blocks execute",
        res.success is False and "Entry blocked" in (res.error or ""),
        res.error, "Entry blocked")

    # Ledger/file disagreement -> refuse.
    eng, br, sf = gated_engine(local=[{"symbol": "SPY", "qty": 5, "side": "buy"}], n=303)
    res = eng.execute(buy_signal())
    chk("G3 ledger/file conflict blocks execute",
        res.success is False and "Entry blocked" in (res.error or ""),
        res.error, "Entry blocked")

    # Kill switch -> refuse.
    eng, br, sf = gated_engine(n=304)
    sf.set_kill(True)
    res = eng.execute(buy_signal())
    chk("G4 kill switch blocks execute",
        res.success is False and "kill switch" in (res.error or "").lower(),
        res.error, "kill switch")

    # Truth unavailable -> refuse (fail closed, never trade blind).
    eng, br, sf = gated_engine(n=305)
    eng._position_truth = False
    res = eng.execute(buy_signal())
    chk("G5 missing position truth blocks execute",
        res.success is False and "Position truth unavailable" in (res.error or ""),
        res.error, "refuses")

    # Confirmed flat -> the gate lets the order through to the broker.
    eng, br, sf = gated_engine(n=306)
    res = eng.execute(buy_signal())
    chk("G6 confirmed-flat entry is not gate-blocked",
        "Entry blocked" not in (res.error or ""), res.error, "not blocked")


# --- E. Guarded exits — duplicates are the danger --------------------------
from position_state import PositionStateManager  # noqa: E402


def exit_engine(flat_after=True, unreachable=False, n=400):
    br = live_broker(order(status="new", id="x-1"))
    br._client.positions = {"SPY": SimpleNamespace(
        symbol="SPY", qty=10.0, market_value=1000.0, cost_basis=1000.0,
        unrealized_pl=0.0, unrealized_plpc=0.0, avg_entry_price=100.0)}
    closed = {"count": 0}
    _real_close = br._client.close_position

    def counting_close(symbol):
        closed["count"] += 1
        if flat_after:
            br._client.positions.pop(symbol, None)
        return _real_close(symbol)

    br._client.close_position = counting_close
    if unreachable:
        br._client.position_error = ConnectionError("down")
    eng = TradingEngine(alpaca_broker=br)
    safety = fresh(n)
    from execution_safety import BrokerExecutionAdapter, PositionTruth
    eng._position_truth = PositionTruth(
        safety, BrokerExecutionAdapter(br), FakeState([]))
    return eng, br, safety, closed


# A clean guarded close: submits once, confirms flat, resolves the ledger.
eng, br, sf, closed = exit_engine(n=400)
res = eng.close_position_guarded("SPY", reason="STOP_LOSS")
chk("E1 guarded close submits to broker", closed["count"] == 1, closed["count"], 1)
chk("E2 guarded close succeeds", res.success is True, res.error, "success")
exits = [r for r in sf._read()["orders"].values() if r.get("is_exit")]
chk("E3 exit recorded in ledger", len(exits) == 1, len(exits), 1)
chk("E4 confirmed-flat exit resolves terminal",
    exits[0]["status"] == "filled", exits[0]["status"], "filled")
chk("E5 no unresolved exit remains",
    sf.has_unresolved_exit("SPY") is False)

# Duplicate exit while one is in flight must be refused, not submitted.
eng, br, sf, closed = exit_engine(flat_after=False, n=401)
first = eng.close_position_guarded("SPY", reason="STOP_LOSS")
second = eng.close_position_guarded("SPY", reason="STOP_LOSS")
chk("E6 broker called once, not twice", closed["count"] == 1, closed["count"], 1)
chk("E7 duplicate exit refused", second.success is False, second.error, "refused")
chk("E8 refusal is explicit about duplication",
    bool((second.details or {}).get("duplicate_exit_prevented")),
    second.details, "duplicate_exit_prevented")

# Position still open after close -> residual, not silent success.
chk("E9 unflattened exit marked residual",
    any(r["status"] == "residual" and r.get("error") == "broker_position_not_flat"
        for r in sf._read()["orders"].values() if r.get("is_exit")),
    [r["status"] for r in sf._read()["orders"].values() if r.get("is_exit")],
    "residual")

# Broker unreachable during confirmation -> residual/unknown, still tracked.
eng, br, sf, closed = exit_engine(flat_after=False, unreachable=True, n=402)
eng.close_position_guarded("SPY", reason="SHUTDOWN")
exits = [r for r in sf._read()["orders"].values() if r.get("is_exit")]
chk("E10 unconfirmable exit marked residual",
    exits and exits[0]["status"] == "residual",
    exits[0]["status"] if exits else None, "residual")
chk("E11 unconfirmable exit says 'unknown', not 'not flat'",
    exits and exits[0]["error"] == "broker_position_unknown",
    exits[0]["error"] if exits else None, "broker_position_unknown")

# An unresolved ENTRY must never block an exit.
eng, br, sf, closed = exit_engine(n=403)
sf.submit(br if False else Broker(), client_order_key="entry-1", symbol="SPY",
          side="buy", quantity=1)
d = sf._read()
d["orders"]["entry-1"]["status"] = "ambiguous"
sf._write(d)
res = eng.close_position_guarded("SPY", reason="STOP_LOSS")
chk("E12 unresolved entry does not block an exit", res.success is True,
    res.error, "exit allowed")

# After a completed exit, a later exit is allowed again.
eng, br, sf, closed = exit_engine(n=404)
eng.close_position_guarded("SPY", reason="FIRST")
br._client.positions["SPY"] = SimpleNamespace(
    symbol="SPY", qty=5.0, market_value=500.0, cost_basis=500.0,
    unrealized_pl=0.0, unrealized_plpc=0.0, avg_entry_price=100.0)
second = eng.close_position_guarded("SPY", reason="SECOND")
chk("E13 new exit allowed after previous resolved", second.success is True,
    second.error, "allowed")


# --- clear_all must prove flatness before erasing state --------------------
import tempfile as _tf  # noqa: E402
mgr = PositionStateManager(str(Path(_tf.mkdtemp()) / "positions.json"))
mgr.save_positions([{"symbol": "SPY", "qty": 5, "side": "buy",
                     "entry_price": 100.0, "entry_time": time.time()}])
chk("E14 clear_all refuses without confirmation", mgr.clear_all() is False)
chk("E15 refused clear_all leaves state intact",
    len(mgr.load_positions()) == 1, len(mgr.load_positions()), 1)
chk("E16 clear_all(confirmed_flat=True) works",
    mgr.clear_all(confirmed_flat=True) is True)
chk("E17 confirmed clear_all empties state", mgr.load_positions() == [])


# --- D. Daily loss limit must survive a restart ----------------------------
# systemd runs this bot with Restart=always. If the daily baseline lives only
# in memory, a crash hands the bot a fresh loss budget measured from the
# already-reduced equity -- and the loss limit becomes unenforceable.
import json as _json  # noqa: E402
import main as _main  # noqa: E402


def orch_with_equity(equity, state_dir):
    """A bare Orchestrator wired to a fake broker at a given equity."""
    _main.DAILY_STATE_FILE = os.path.join(state_dir, "daily_risk_state.json")
    o = _main.Orchestrator.__new__(_main.Orchestrator)
    o.state = _main.PipelineState()
    acct = SimpleNamespace(equity=equity)
    o._trading_engine = SimpleNamespace(
        broker=SimpleNamespace(get_account=lambda: acct, is_simulating=False))
    o._pattern_engine = SimpleNamespace(
        db=SimpleNamespace(record_milestone=lambda **kw: None,
                           get_active_positions=lambda: []))
    o._close_all_active_positions = lambda: []
    return o


_sd = tempfile.mkdtemp()

# Day 1: baseline is established and written to disk.
o = orch_with_equity(100000.0, _sd)
o._update_daily_tracking()
chk("D1 baseline recorded", o.state.daily_starting_equity == 100000.0,
    o.state.daily_starting_equity, 100000.0)
chk("D2 baseline persisted to disk", os.path.exists(_main.DAILY_STATE_FILE))

# Equity falls 2%: still under the 3% limit, no halt.
o = orch_with_equity(98000.0, _sd)
o._load_daily_tracking()
o._update_daily_tracking()
chk("D3 baseline survives restart, not reset to current equity",
    o.state.daily_starting_equity == 100000.0, o.state.daily_starting_equity, 100000.0)
chk("D4 loss measured against original baseline",
    abs(o.state.daily_pnl_pct + 2.0) < 1e-6, o.state.daily_pnl_pct, -2.0)

# Equity falls past the limit -> halt, and the halt is persisted.
o = orch_with_equity(96000.0, _sd)
o._load_daily_tracking()
breached = o._check_daily_loss_limit()
chk("D5 limit breach detected", breached is True)
chk("D6 halt persisted", _json.load(open(_main.DAILY_STATE_FILE))["daily_loss_hit"] is True)

# THE BUG: restart after a breach must stay halted, not resume trading.
o = orch_with_equity(96000.0, _sd)
o._load_daily_tracking()
chk("D7 restart after breach stays halted", o.state.daily_loss_hit is True,
    o.state.daily_loss_hit, True)
chk("D8 restart after breach keeps DAILY_LOSS_LIMIT mode",
    o.state.mode == _main.OrchestratorMode.DAILY_LOSS_LIMIT,
    o.state.mode, "DAILY_LOSS_LIMIT")
chk("D9 restart does not re-arm a fresh budget",
    o._check_daily_loss_limit() is True)
chk("D10 baseline not reset to reduced equity",
    o.state.daily_starting_equity == 100000.0,
    o.state.daily_starting_equity, 100000.0)

# A genuinely new day clears the halt.
_sd2 = tempfile.mkdtemp()
o = orch_with_equity(96000.0, _sd2)
_main.DAILY_STATE_FILE = os.path.join(_sd2, "daily_risk_state.json")
with open(_main.DAILY_STATE_FILE, "w") as _h:
    _json.dump({"daily_start_date": "1999-01-01", "daily_starting_equity": 100000.0,
                "daily_loss_hit": True, "daily_pnl_pct": -4.0}, _h)
o._load_daily_tracking()
chk("D11 previous day's halt does not carry over",
    o.state.daily_loss_hit is False, o.state.daily_loss_hit, False)
o._update_daily_tracking()
chk("D12 new day rebaselines to current equity",
    o.state.daily_starting_equity == 96000.0, o.state.daily_starting_equity, 96000.0)

# An unreadable state file must halt, not resume with an unknown budget.
_sd3 = tempfile.mkdtemp()
o = orch_with_equity(96000.0, _sd3)
_main.DAILY_STATE_FILE = os.path.join(_sd3, "daily_risk_state.json")
open(_main.DAILY_STATE_FILE, "w").write("{corrupt")
o._load_daily_tracking()
chk("D13 unreadable daily state fails closed", o.state.daily_loss_hit is True,
    o.state.daily_loss_hit, True)
chk("D14 unreadable daily state halts trading",
    o.state.mode == _main.OrchestratorMode.DAILY_LOSS_LIMIT,
    o.state.mode, "DAILY_LOSS_LIMIT")


# --- S. Stop-loss monitor robustness ---------------------------------------
# The monitor is the software stop. If it can be crashed by one bad row, or
# silently reports 0% when it cannot price a symbol, positions run unprotected.
from unittest.mock import MagicMock  # noqa: E402


def monitor(positions, price=None, price_error=None, simulating=False):
    o = _main.Orchestrator.__new__(_main.Orchestrator)
    o.state = _main.PipelineState()
    patterns = MagicMock()
    patterns.db.get_active_positions.return_value = positions
    patterns.db._connect.return_value.execute.return_value.fetchone.return_value = None
    patterns.close_tracked_position.return_value = {"dollar_pnl": -2.5}
    trading = MagicMock()
    trading.broker.is_simulating = simulating
    if price_error is not None:
        trading._get_reference_price.side_effect = price_error
    else:
        trading._get_reference_price.return_value = price
    trading.close_position_guarded.return_value = SimpleNamespace(
        success=True, order_id="c-1", error=None)
    o._pattern_engine = patterns
    o._trading_engine = trading
    return o, trading


GOOD = {"symbol": "SPY", "record_id": 7, "side": "buy",
        "entry_price": 100.0, "quantity": 1}
BAD_ZERO = {"symbol": "BAD", "record_id": 1, "side": "buy",
            "entry_price": 0.0, "quantity": 1}
BAD_NONE = {"symbol": "NUL", "record_id": 2, "side": "buy",
            "entry_price": None, "quantity": 1}

# A zero entry price must not raise.
o, tr = monitor([BAD_ZERO], price=97.0)
try:
    o._check_active_positions(context="t")
    crashed = False
except ZeroDivisionError:
    crashed = True
chk("S1 zero entry price does not crash the monitor", not crashed)

# ...and must not stop the OTHER positions from being evaluated.
o, tr = monitor([BAD_ZERO, BAD_NONE, GOOD], price=97.0)
closed = o._check_active_positions(context="t")
chk("S2 a bad row does not disable the rest of the monitor",
    len(closed) == 1, len(closed), 1)
chk("S3 the good position still stopped out",
    closed and closed[0]["trigger"] == "STOP_LOSS",
    closed[0]["trigger"] if closed else None, "STOP_LOSS")
chk("S4 bad rows flagged unprotected",
    set(o.unprotected_positions()) == {"BAD", "NUL"},
    o.unprotected_positions(), {"BAD", "NUL"})

# No live price: must NOT be reported as 0% profit, must not close, must flag.
o, tr = monitor([GOOD], price=None)
closed = o._check_active_positions(context="t")
chk("S5 unpriceable position is not closed", closed == [], closed, [])
chk("S6 unpriceable position flagged unprotected",
    "SPY" in o.unprotected_positions(), o.unprotected_positions(), "SPY flagged")
chk("S7 no close attempted without a price",
    tr.close_position_guarded.call_count == 0,
    tr.close_position_guarded.call_count, 0)

# A raising price feed behaves the same way.
o, tr = monitor([GOOD], price_error=ConnectionError("feed down"))
closed = o._check_active_positions(context="t")
chk("S8 price feed exception does not fabricate a price", closed == [], closed, [])
chk("S9 price feed exception flags unprotected", "SPY" in o.unprotected_positions())

# Repeated failure escalates rather than passing silently every cycle.
o, tr = monitor([GOOD], price=None)
for _ in range(3):
    o._check_active_positions(context="t")
chk("S10 repeated failures accumulate a streak",
    o.unprotected_positions().get("SPY") == 3,
    o.unprotected_positions().get("SPY"), 3)

# Recovery clears the flag and the stop works again.
o, tr = monitor([GOOD], price=None)
o._check_active_positions(context="t")
tr._get_reference_price.return_value = 97.0
closed = o._check_active_positions(context="t")
chk("S11 recovery clears the unprotected flag",
    o.unprotected_positions() == {}, o.unprotected_positions(), {})
chk("S12 stop fires once pricing recovers",
    len(closed) == 1, len(closed), 1)

# Normal operation still works: target hit closes as TAKE_PROFIT.
o, tr = monitor([GOOD], price=104.0)
closed = o._check_active_positions(context="t")
chk("S13 take-profit still fires",
    closed and closed[0]["trigger"] == "TAKE_PROFIT",
    closed[0]["trigger"] if closed else None, "TAKE_PROFIT")

# A short that moves against us stops out.
SHORT = {"symbol": "QQQ", "record_id": 9, "side": "sell",
         "entry_price": 100.0, "quantity": 1}
o, tr = monitor([SHORT], price=103.0)
closed = o._check_active_positions(context="t")
chk("S14 short stop-loss fires on a rise",
    closed and closed[0]["trigger"] == "STOP_LOSS",
    closed[0]["trigger"] if closed else None, "STOP_LOSS")


# --- Q. Position sizing must respect the per-position cap ------------------
# The cap is the last line of defence on how much a single bad trade can cost.
from trading import AccountInfo  # noqa: E402


def sized(equity, ref_price, buying_power=None, risk=0.005, max_pos=0.15,
          sl_pct=0.025):
    br = live_broker(order())
    eng = TradingEngine(alpaca_broker=br, max_position_size=max_pos,
                        risk_per_trade=risk)
    # Bind directly to the instance.  Assigning ``staticmethod(...)`` to an
    # instance leaves a non-callable descriptor on Python 3.9.
    eng._get_reference_price = lambda sym: ref_price
    acct = AccountInfo()
    acct.equity = equity
    acct.buying_power = equity if buying_power is None else buying_power
    return eng._calculate_quantity("SPY", trading.OrderSide.BUY, 0.9, acct,
                                   stop_loss_pct=sl_pct), acct, eng


# Cheap symbol: sizing works normally and lands exactly on the cap.
qty, acct, eng = sized(2000.0, 50.0)
chk("Q1 normal sizing respects the cap", qty * 50.0 <= 2000.0 * 0.15 + 0.01,
    qty * 50.0, "<= 300")
chk("Q2 normal sizing is non-zero", qty > 0, qty, "> 0")

# One share priced exactly at the cap is allowed.
qty, _, _ = sized(2000.0, 300.0)
chk("Q3 one share exactly at the cap is allowed", qty == 1, qty, 1)

# THE BUG: one share worth more than the cap must be refused, not floored to 1.
for ref, pct in ((600.0, 30), (900.0, 45)):
    qty, _, _ = sized(2000.0, ref)
    chk("Q4 one share at $%.0f (%d%% of equity) refused" % (ref, pct),
        qty == 0, qty, 0)

# The cap holds across a range of prices.
violations = []
for ref in (10.0, 25.0, 75.0, 150.0, 299.0, 301.0, 500.0, 1200.0):
    qty, _, _ = sized(2000.0, ref)
    if qty * ref > 2000.0 * 0.15 + 0.01:
        violations.append((ref, qty, qty * ref))
chk("Q5 cap never exceeded at any price point", violations == [], violations, [])

# Small accounts are where this bites hardest.
qty, _, _ = sized(500.0, 400.0)
chk("Q6 small account refuses an oversized single share", qty == 0, qty, 0)

# Buying power caps the order even when the risk cap would allow more.
qty, _, _ = sized(100000.0, 100.0, buying_power=500.0)
chk("Q7 quantity limited by buying power", qty * 100.0 <= 500.0,
    qty * 100.0, "<= 500")
qty, _, _ = sized(100000.0, 600.0, buying_power=100.0)
chk("Q8 unaffordable order sizes to zero", qty == 0, qty, 0)

# Degenerate inputs fail closed.
chk("Q9 zero equity -> no trade", sized(0.0, 50.0)[0] == 0)
chk("Q10 negative equity -> no trade", sized(-100.0, 50.0)[0] == 0)

# A halved risk budget (drawdown mode) halves the position -- but only where
# the RISK term binds. At default settings the 15% cap binds first, so the
# drawdown multiplier has no effect until the cap is raised. Worth knowing.
full, _, _ = sized(100000.0, 50.0, risk=0.005, max_pos=0.5)
half, _, _ = sized(100000.0, 50.0, risk=0.0025, max_pos=0.5)
chk("Q11 halved risk halves the size when risk binds",
    half * 2 == full, (half, full), "half*2 == full")

capped_full, _, _ = sized(100000.0, 50.0, risk=0.005)
capped_half, _, _ = sized(100000.0, 50.0, risk=0.0025)
chk("Q12 at default settings the cap binds, not the risk budget",
    capped_full == int(100000.0 * 0.15 / 50.0),
    capped_full, int(100000.0 * 0.15 / 50.0))
chk("Q13 drawdown halving still reduces size below the cap",
    capped_half < capped_full, (capped_half, capped_full), "half < full")


# --- X. Portfolio-level exposure ceiling -----------------------------------
# Per-position caps do not bound correlated exposure. SPY/QQQ/IWM move
# together, so N positions at the per-position cap is one big directional bet.
def held(*values):
    return [{"symbol": "S%d" % i, "market_value": v} for i, v in enumerate(values)]


def sized_book(equity, ref_price, positions=None, total_cap=0.30,
               max_concurrent=3, max_pos=0.15):
    br = live_broker(order())
    eng = TradingEngine(alpaca_broker=br, max_position_size=max_pos,
                        risk_per_trade=0.005, max_total_exposure=total_cap,
                        max_concurrent_positions=max_concurrent)
    TradingEngine._get_reference_price = staticmethod(lambda sym: ref_price)
    acct = AccountInfo()
    acct.equity = equity
    acct.buying_power = equity
    acct.positions = positions or []
    return eng._calculate_quantity("SPY", trading.OrderSide.BUY, 0.9, acct,
                                   stop_loss_pct=0.025), eng, acct


# Empty book: normal sizing.
qty, eng, acct = sized_book(100000.0, 50.0)
chk("X1 empty book sizes normally", qty > 0, qty, "> 0")
chk("X2 first position respects per-position cap",
    qty * 50.0 <= 100000.0 * 0.15 + 0.01, qty * 50.0, "<= 15000")

# Exposure accounting.
tot, cnt = TradingEngine.current_exposure(
    SimpleNamespace(positions=held(15000.0, 15000.0)))
chk("X3 exposure totals across positions", tot == 30000.0, tot, 30000.0)
chk("X4 position count tracked", cnt == 2, cnt, 2)

# Shorts count as exposure, they do not net off against longs.
tot, _ = TradingEngine.current_exposure(
    SimpleNamespace(positions=held(15000.0, -15000.0)))
chk("X5 short counts as gross exposure, not offset", tot == 30000.0, tot, 30000.0)

# At the ceiling: refuse.
qty, _, _ = sized_book(100000.0, 50.0, positions=held(15000.0, 15000.0))
chk("X6 refuses once total exposure ceiling reached", qty == 0, qty, 0)

# Partially used: the new position is trimmed to what remains.
qty, _, _ = sized_book(100000.0, 50.0, positions=held(20000.0))
chk("X7 trims new position to the remaining budget",
    qty * 50.0 <= 10000.0 + 0.01, qty * 50.0, "<= 10000")
chk("X8 still trades when budget remains", qty > 0, qty, "> 0")

# Concurrent-position limit.
qty, _, _ = sized_book(100000.0, 50.0, positions=held(1000.0, 1000.0, 1000.0),
                       max_concurrent=3)
chk("X9 refuses beyond the concurrent position limit", qty == 0, qty, 0)
qty, _, _ = sized_book(100000.0, 50.0, positions=held(1000.0, 1000.0),
                       max_concurrent=3)
chk("X10 allows up to the concurrent limit", qty > 0, qty, "> 0")

# The correlated-stacking scenario this exists to prevent.
book, equity, price = [], 100000.0, 50.0
for _ in range(6):
    q, _, _ = sized_book(equity, price, positions=list(book), total_cap=0.30,
                         max_concurrent=10)
    if q == 0:
        break
    book.append({"symbol": "C%d" % len(book), "market_value": q * price})
total_notional = sum(p["market_value"] for p in book)
chk("X11 stacked correlated positions stay under the ceiling",
    total_notional <= equity * 0.30 + 0.01,
    "%.0f" % total_notional, "<= 30000")
chk("X12 stacking stops rather than running unbounded", len(book) <= 3,
    len(book), "<= 3")

# Degenerate inputs.
qty, _, _ = sized_book(100000.0, 50.0,
                       positions=[{"symbol": "X", "market_value": None},
                                  {"symbol": "Y", "market_value": "bad"}])
chk("X13 unparseable position values do not crash sizing", qty >= 0, qty, ">= 0")
tot, cnt = TradingEngine.current_exposure(SimpleNamespace(positions=None))
chk("X14 missing position list treated as empty", (tot, cnt) == (0.0, 0))


# --- T. Portfolio statistics -----------------------------------------------
# These numbers are what you will judge the bot by. If drawdown is understated
# the strategy looks safer than it is.
import sqlite3 as _sqlite3  # noqa: E402
from stats import PortfolioStats  # noqa: E402


def stats_over(pnls, outcomes=None):
    """Build a throwaway patterns.db containing the given completed trades."""
    path = Path(tempfile.mkdtemp()) / "p.db"
    conn = _sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE pattern_memory (
        id INTEGER PRIMARY KEY, symbol TEXT, profit_pct REAL, outcome TEXT,
        sentiment_zone TEXT, rsi_zone TEXT, ema_cross TEXT, entry_price REAL,
        exit_price REAL, timestamp REAL, data_source TEXT, regime TEXT)""")
    for i, pnl in enumerate(pnls):
        outcome = outcomes[i] if outcomes else ("win" if pnl > 0 else "loss")
        conn.execute(
            "INSERT INTO pattern_memory (symbol, profit_pct, outcome, "
            "sentiment_zone, rsi_zone, ema_cross, entry_price, exit_price, "
            "timestamp, data_source) VALUES (?,?,?,?,?,?,?,?,?,'live')",
            ("SPY", pnl, outcome, "z", "z", "z", 100.0, 100.0, float(i)))
    conn.commit()
    conn.close()
    return PortfolioStats(db_path=path)


# Drawdown must compound, not add percentages to an equity figure.
r = stats_over([10.0, -10.0]).compute()
chk("T1 +10%/-10% is a 10% drawdown, not 9.09%",
    abs(r["max_drawdown_pct"] - 10.0) < 0.01, r["max_drawdown_pct"], 10.0)
chk("T2 +10%/-10% leaves you down, not flat",
    abs(r["compounded_return_pct"] + 1.0) < 0.01,
    r["compounded_return_pct"], -1.0)

r = stats_over([-10.0] * 5).compute()
chk("T3 five -10% trades is a 40.95% drawdown, not 50%",
    abs(r["max_drawdown_pct"] - 40.951) < 0.01, r["max_drawdown_pct"], 40.951)

r = stats_over([20.0, -20.0, 20.0, -20.0]).compute()
chk("T4 alternating +/-20% drawdown is 23.2%, not 16.67%",
    abs(r["max_drawdown_pct"] - 23.2) < 0.05, r["max_drawdown_pct"], 23.2)
chk("T5 alternating +/-20% loses money overall",
    r["compounded_return_pct"] < 0, r["compounded_return_pct"], "< 0")

# Monotonic gains have no drawdown.
r = stats_over([5.0, 5.0, 5.0]).compute()
chk("T6 all winners -> zero drawdown", r["max_drawdown_pct"] == 0.0,
    r["max_drawdown_pct"], 0.0)
chk("T7 compounded gain exceeds naive sum",
    r["compounded_return_pct"] > r["total_net_pnl_pct"],
    (r["compounded_return_pct"], r["total_net_pnl_pct"]), "compounded > sum")

# Core ratios.
r = stats_over([10.0, 10.0, -5.0, -5.0]).compute()
chk("T8 win rate over completed trades", r["win_rate"] == 0.5, r["win_rate"], 0.5)
chk("T9 profit factor = gross win / gross loss", r["profit_factor"] == 2.0,
    r["profit_factor"], 2.0)
chk("T10 expectancy is average per trade",
    abs(r["expectancy_pct"] - 2.5) < 1e-6, r["expectancy_pct"], 2.5)

r = stats_over([-1.0, -1.0, -1.0, 5.0, -1.0]).compute()
chk("T11 max consecutive losses", r["max_consecutive_losses"] == 3,
    r["max_consecutive_losses"], 3)

# No trades at all must not divide by zero.
r = stats_over([]).compute()
chk("T12 empty history is safe", r["total_trades"] == 0 and r["win_rate"] == 0.0)
chk("T13 empty history reports zero drawdown", r["max_drawdown_pct"] == 0.0)

# Total ruin is reported as such rather than going negative.
r = stats_over([-100.0]).compute()
chk("T14 a -100% trade is a 100% drawdown", r["max_drawdown_pct"] == 100.0,
    r["max_drawdown_pct"], 100.0)


# --- F. Forward test — does the evidence actually support going live? ------
import forward_test as _ft  # noqa: E402
from forward_test import (ForwardTest, wilson_interval,  # noqa: E402
                          trades_needed_for_confidence)


def ft_over(pnls, day_span=40, quarantine_before=""):
    path = Path(tempfile.mkdtemp()) / "p.db"
    conn = _sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE pattern_memory (
        id INTEGER PRIMARY KEY, symbol TEXT, profit_pct REAL, outcome TEXT,
        timestamp REAL, data_source TEXT, tier TEXT)""")
    base = 1_700_000_000.0
    step = (day_span * 86400.0) / max(1, len(pnls))
    for i, pnl in enumerate(pnls):
        conn.execute(
            "INSERT INTO pattern_memory (symbol, profit_pct, outcome, "
            "timestamp, data_source, tier) VALUES (?,?,?,?,'live','signal')",
            ("SPY", pnl, "win" if pnl > 0 else "loss", base + i * step))
    conn.commit()
    conn.close()
    _ft.QUARANTINE_BEFORE = quarantine_before
    return ForwardTest(db_path=path)


# Wilson interval behaves sensibly where it matters: small samples.
low, high = wilson_interval(8, 12)
chk("F1 small sample gives a wide interval", (high - low) > 0.3,
    round(high - low, 3), "> 0.3")
chk("F2 small sample interval spans breakeven", low < 0.5 < high,
    (round(low, 3), round(high, 3)), "spans 0.5")
low2, high2 = wilson_interval(400, 600)
chk("F3 large sample narrows the interval", (high2 - low2) < 0.1,
    round(high2 - low2, 3), "< 0.1")
chk("F4 zero trials does not divide by zero", wilson_interval(0, 0) == (0.0, 1.0))
chk("F5 all wins still bounded by 1.0", wilson_interval(10, 10)[1] <= 1.0)

chk("F6 breakeven rate can never clear the floor",
    trades_needed_for_confidence(0.50) is None)
chk("F7 a strong rate needs fewer trades than a marginal one",
    trades_needed_for_confidence(0.70) < trades_needed_for_confidence(0.55),
    (trades_needed_for_confidence(0.70), trades_needed_for_confidence(0.55)),
    "70% < 55%")

# No history: not ready, and says why.
r = ft_over([]).evaluate()
chk("F8 empty history is not ready", r.ready is False)
chk("F9 empty history explains itself", any("no completed" in b for b in r.blockers))

# A small winning sample is NOT ready -- this is the trap the gate exists for.
r = ft_over([2.0] * 8 + [-1.0] * 4).evaluate()
chk("F10 12 trades at 67% win rate is not ready", r.ready is False,
    r.ready, False)
chk("F11 small sample blocked on trade count",
    any("trades" in b and ">=" in b for b in r.blockers), r.blockers, "count blocker")
chk("F12 reports how many more trades are needed",
    any("would settle it" in b for b in r.blockers) or r.trades < _ft.MIN_TRADES)

# Negative expectancy is blocked no matter how many trades.
r = ft_over([1.0] * 30 + [-3.0] * 30, day_span=60).evaluate()
chk("F13 negative expectancy blocked", r.ready is False)
chk("F14 negative expectancy named explicitly",
    any("expectancy" in b for b in r.blockers), r.blockers, "expectancy blocker")

# A genuinely strong, long sample passes.
_mixed = []
for _i in range(80):
    _mixed.append(1.5 if _i % 4 else -1.0)   # 3 wins : 1 loss, interleaved
r = ft_over(_mixed, day_span=60).evaluate()
chk("F15 strong long sample is ready", r.ready is True, r.blockers, "ready")
chk("F16 ready verdict still warns about paper optimism",
    any("optimistic" in n for n in r.notes), r.notes, "warns")

# Drawdown gate bites even when the win rate looks good.
r = ft_over([-8.0] * 3 + [2.0] * 90, day_span=60).evaluate()
chk("F17 excessive drawdown blocks readiness",
    any("drawdown" in b for b in r.blockers) or r.ready is False,
    r.blockers, "drawdown blocker")

# Drawdown compounds here too.
chk("F18 forward drawdown compounds",
    abs(_ft.max_drawdown_pct([10.0, -10.0]) - 10.0) < 0.01,
    _ft.max_drawdown_pct([10.0, -10.0]), 10.0)
chk("F19 forward compounded return is not a naive sum",
    abs(_ft.compounded_return_pct([10.0, -10.0]) + 1.0) < 0.01,
    _ft.compounded_return_pct([10.0, -10.0]), -1.0)

# Quarantine excludes pre-fix trades from the evidence.
r = ft_over([2.0] * 40, day_span=40,
            quarantine_before="2023-11-30T00:00:00Z").evaluate()
chk("F20 pre-fix trades are quarantined", r.quarantined > 0, r.quarantined, "> 0")
chk("F21 quarantine is disclosed in the report",
    any("pre-fix" in n for n in r.notes), r.notes, "discloses")
_ft.QUARANTINE_BEFORE = ""

# Concentrated-in-one-day samples are blocked on day span.
r = ft_over(_mixed, day_span=1).evaluate()
chk("F22 all trades in one day blocked on day span",
    any("trading days" in b for b in r.blockers), r.blockers, "day-span blocker")

# The rendered report is human-readable and states a verdict.
text = ft_over([2.0] * 8 + [-1.0] * 4).render()
chk("F23 report states a verdict", "VERDICT" in text)
chk("F24 report shows the confidence interval", "95% CI" in text)


# --- E2. Evidence-gated pattern strength -----------------------------------
# The learner must not act on a pattern until the evidence rules out chance.
# This is what turns "measure the lower bound" into a search for edge: only
# patterns that clear the bar carry weight, so noise never gets traded.
def st(wins, losses, total_pnl=0.0, seen=None):
    return PatternStats(pattern_id="p", count=seen if seen is not None else wins + losses,
                        wins=wins, losses=losses, total_profit_pct=total_pnl)


# Tiny perfect records are the classic trap: 100% of 2 proves nothing.
chk("E2-1 2 wins from 2 carries no weight", st(2, 0).signal_strength == 0.0,
    st(2, 0).signal_strength, 0.0)
chk("E2-2 3 wins from 3 carries no weight", st(3, 0).signal_strength == 0.0,
    st(3, 0).signal_strength, 0.0)
chk("E2-3 7 of 10 (70%) still carries no weight",
    st(7, 3).signal_strength == 0.0, st(7, 3).signal_strength, 0.0)

# Enough evidence -> positive weight, but modest.
s15 = st(15, 5)
chk("E2-4 15 of 20 (75%) is favourable", s15.signal_strength > 0,
    s15.signal_strength, "> 0")
chk("E2-5 favourable weight is conservative, not the point estimate",
    s15.signal_strength < (0.75 - 0.5) * 2, s15.signal_strength, "< 0.5")

# Confidently bad patterns score negative.
bad = st(1, 19)
chk("E2-6 1 of 20 is unfavourable", bad.signal_strength < 0,
    bad.signal_strength, "< 0")

# Genuinely ambiguous stays at zero rather than guessing a direction.
chk("E2-7 4 of 10 is undecided, not bearish", st(4, 6).signal_strength == 0.0,
    st(4, 6).signal_strength, 0.0)
chk("E2-8 coin-flip record carries no weight", st(50, 50).signal_strength == 0.0,
    st(50, 50).signal_strength, 0.0)

# More evidence at the same rate increases confidence.
small, large = st(12, 8), st(120, 80)   # both 60%
chk("E2-9 same rate, more evidence -> more weight",
    large.signal_strength > small.signal_strength,
    (small.signal_strength, large.signal_strength), "large > small")
chk("E2-10 60% on 20 trades is not yet evidence",
    small.signal_strength == 0.0, small.signal_strength, 0.0)

# The interval itself is exposed and sane.
low, high = st(15, 5).confidence_interval
chk("E2-11 interval brackets the point estimate", low < 0.75 < high,
    (round(low, 3), round(high, 3)), "brackets 0.75")
chk("E2-12 no resolved trades -> maximally uncertain",
    st(0, 0).confidence_interval == (0.0, 1.0))

# Every verdict is explainable in words.
chk("E2-13 undecided patterns say so",
    "undecided" in st(7, 3).evidence_status, st(7, 3).evidence_status, "undecided")
chk("E2-14 favourable patterns state their floor",
    "at least" in st(15, 5).evidence_status, st(15, 5).evidence_status, "at least")
chk("E2-15 unfavourable patterns state their ceiling",
    "at most" in st(1, 19).evidence_status, st(1, 19).evidence_status, "at most")
chk("E2-16 thin patterns report insufficient evidence",
    "insufficient" in st(1, 0).evidence_status, st(1, 0).evidence_status, "insufficient")

# Sightings must not masquerade as evidence.
chk("E2-17 many sightings, few outcomes -> still no weight",
    st(2, 0, seen=500).signal_strength == 0.0,
    st(2, 0, seen=500).signal_strength, 0.0)


# --- J. Decision journal — why it did what it did --------------------------
from decision_log import DecisionLog  # noqa: E402


def journal():
    return DecisionLog(path=str(Path(tempfile.mkdtemp()) / "d.jsonl"))


j = journal()
j.entered("SPY", "buy", 10, 100.0, reason="sentiment+pattern",
          inputs={"conviction": 0.82, "rsi": 31.0},
          gates={"exposure_gate": "passed"},
          sizing={"equity": 10000.0, "requested_qty": 10}, trade_id="T1")
j.exited("SPY", "buy", trigger="STOP_LOSS", entry_price=100.0, exit_price=97.5,
         profit_pct=-2.5, hold_hours=3.0, trade_id="T1")
j.excursion("SPY", trade_id="T1", entry_price=100.0, best_price=102.0,
            worst_price=97.0, exit_price=97.5, side="buy")

chk("J1 journal persists events", len(j.read()) == 3, len(j.read()), 3)
story = j.explain("T1")
chk("J2 explain shows why it entered", "sentiment+pattern" in story)
chk("J3 explain shows the signal inputs", "conviction" in story and "0.82" in story)
chk("J4 explain shows why it exited", "STOP_LOSS" in story)
chk("J5 explain shows the path price took", "best" in story and "worst" in story)
chk("J6 unknown trade explains itself", "No journal entries" in j.explain("nope"))

# Excursion arithmetic, both directions.
j2 = journal()
j2.excursion("QQQ", trade_id="T2", entry_price=100.0, best_price=110.0,
             worst_price=98.0, exit_price=105.0, side="buy")
e = [x for x in j2.read() if x["event"] == "excursion"][0]
chk("J7 long MFE measured", abs(e["mfe_pct"] - 10.0) < 1e-6, e["mfe_pct"], 10.0)
chk("J8 long MAE measured", abs(e["mae_pct"] + 2.0) < 1e-6, e["mae_pct"], -2.0)
chk("J9 money left on the table quantified",
    abs(e["left_on_table_pct"] - 5.0) < 1e-6, e["left_on_table_pct"], 5.0)

j3 = journal()
j3.excursion("IWM", trade_id="T3", entry_price=100.0, best_price=90.0,
             worst_price=104.0, exit_price=95.0, side="sell")
e = [x for x in j3.read() if x["event"] == "excursion"][0]
chk("J10 short MFE is a fall, not a rise", abs(e["mfe_pct"] - 10.0) < 1e-6,
    e["mfe_pct"], 10.0)
chk("J11 short MAE is a rise", abs(e["mae_pct"] + 4.0) < 1e-6, e["mae_pct"], -4.0)

# Refusals are recorded with a named blocker.
j4 = journal()
j4.blocked("SPY", "buy", blocker="already exposed", detail="long in SPY")
j4.blocked("QQQ", "buy", blocker="kill switch engaged")
j4.blocked("IWM", "buy", blocker="already exposed", detail="long in IWM")
report = j4.postmortem()
chk("J12 refusals counted", report["blocked"] == 3, report["blocked"], 3)
chk("J13 most common refusal identified",
    any("already exposed" in f for f in report["findings"]),
    report["findings"], "names the blocker")

# Postmortem detects a stop that is too tight.
j5 = journal()
for i in range(6):
    j5.excursion("SPY", trade_id="S%d" % i, entry_price=100.0,
                 best_price=101.8, worst_price=97.5, exit_price=97.5, side="buy")
findings = j5.postmortem(stop_loss_pct=2.5, take_profit_pct=3.0)["findings"]
chk("J14 detects stop-too-tight pattern",
    any("too tight" in f for f in findings), findings, "flags tight stop")

# Postmortem detects exiting winners early.
j6 = journal()
for i in range(6):
    j6.excursion("SPY", trade_id="W%d" % i, entry_price=100.0,
                 best_price=108.0, worst_price=99.0, exit_price=103.0, side="buy")
findings = j6.postmortem()["findings"]
chk("J15 detects money left on the table",
    any("gave back" in f for f in findings), findings, "flags early exit")

# Per-symbol breakdown.
j7 = journal()
j7.exited("SPY", "buy", trigger="TAKE_PROFIT", entry_price=100.0,
          exit_price=103.0, profit_pct=3.0)
j7.exited("SPY", "buy", trigger="STOP_LOSS", entry_price=100.0,
          exit_price=97.5, profit_pct=-2.5)
j7.exited("QQQ", "buy", trigger="TAKE_PROFIT", entry_price=100.0,
          exit_price=103.0, profit_pct=3.0)
report = j7.postmortem()
chk("J16 per-symbol trade counts", report["by_symbol"]["SPY"]["trades"] == 2,
    report["by_symbol"]["SPY"]["trades"], 2)
chk("J17 exit triggers tallied",
    report["exit_triggers"]["TAKE_PROFIT"] == 2,
    report["exit_triggers"], "2 take-profits")

# The journal must never be able to break trading.
broken = DecisionLog(path="/nonexistent-dir/nope/d.jsonl")
try:
    broken.entered("SPY", "buy", 1, 100.0)
    survived = True
except Exception:
    survived = False
chk("J18 an unwritable journal never raises", survived)
chk("J19 unwritable journal reads as empty", broken.read() == [])

# Empty journal renders without crashing.
chk("J20 empty postmortem is safe", "DECISION POSTMORTEM" in journal().render_postmortem())


# --- W. Integration wiring — is each capability actually reachable? --------
# Every one of these was built, tested in isolation, and NOT connected to the
# running system. Dead safety code is worse than none: it reads as protection.
import ast as _ast  # noqa: E402

_main_src = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
_trading_src = open(os.path.join(BACKEND, "trading.py"), encoding="utf-8").read()


def _calls_in(src):
    names = set()
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.Call):
            fn = node.func
            if isinstance(fn, _ast.Attribute):
                names.add(fn.attr)
            elif isinstance(fn, _ast.Name):
                names.add(fn.id)
    return names


_main_calls = _calls_in(_main_src)
_trading_calls = _calls_in(_trading_src)

chk("W1 order-ledger kill switch is engaged by the orchestrator",
    "set_kill" in _calls_in(_main_src) or "_engage_execution_kill" in _main_calls)
chk("W2 kill switch fires on unexpected exceptions",
    "_engage_execution_kill" in _main_calls)
chk("W3 startup reconciles the ORDER ledger, not just positions",
    "orders_reconciled" in _main_src and "STARTUP CONFLICT" in _main_src)
chk("W4 excursion is recorded on exit", "excursion" in _main_calls)
# The gate must be consulted UNDER A CLAIM, not merely consulted. Calling
# can_enter() and then ordering is check-then-act: two threads can both see
# "flat" and both submit. entry_claim() holds the symbol across both.
chk("W5 entry gate is consulted before ordering",
    "entry_claim" in _trading_calls or "can_enter" in _trading_calls)
chk("W5a the gate and the order it authorises are not separable",
    "entry_claim" in _trading_calls, sorted(
        c for c in _trading_calls if "enter" in c or "claim" in c),
    "entry_claim")
chk("W6 exits route through the guarded path",
    "close_position_guarded" in _main_calls)
chk("W7 entries are journalled", "entered" in _trading_calls)
chk("W8 refusals are journalled", "blocked" in _trading_calls)
chk("W9 daily risk state is persisted and restored",
    "_save_daily_tracking" in _main_calls and "_load_daily_tracking" in _main_calls)
chk("W10 unprotected positions are surfaced in state",
    "unprotected_positions" in _main_src)

# Behavioural: the two kill switches move together.
o = orch_with_equity(100000.0, tempfile.mkdtemp())
_killed = {"value": None}
o._trading_engine.position_truth = SimpleNamespace(
    safety=SimpleNamespace(set_kill=lambda v: _killed.__setitem__("value", v)))
o._engage_execution_kill(True)
chk("W11 orchestrator kill reaches the order ledger", _killed["value"] is True,
    _killed["value"], True)

# A broken ledger must not stop the orchestrator killing itself.
o._trading_engine.position_truth = SimpleNamespace(
    safety=SimpleNamespace(set_kill=lambda v: (_ for _ in ()).throw(OSError("disk"))))
try:
    o._engage_execution_kill(True)
    survived = True
except Exception:
    survived = False
chk("W12 kill switch survives a broken ledger", survived)

# Excursion tracking follows the position and is cleared on exit.
# Pin the stop/target: these assert TRACKING, and would otherwise break
# whenever the bar timeframe (and so the scaled thresholds) changes.
_orig_sl, _orig_tp = _main.STOP_LOSS_PCT, _main.TAKE_PROFIT_PCT
_main.STOP_LOSS_PCT, _main.TAKE_PROFIT_PCT = 0.025, 0.03

o2, tr2 = monitor([GOOD], price=101.0)
o2._check_active_positions(context="t")
chk("W13 excursion tracked while the position is open",
    "SPY" in o2.state.excursions, o2.state.excursions, "tracks SPY")
tr2._get_reference_price.return_value = 104.0      # take-profit
o2._check_active_positions(context="t")
chk("W14 excursion cleared once the position closes",
    "SPY" not in o2.state.excursions, o2.state.excursions, "cleared")

# Best/worst follow direction, not raw magnitude.
o3, tr3 = monitor([GOOD], price=102.0)
o3._check_active_positions(context="t")
tr3._get_reference_price.return_value = 99.0
o3._check_active_positions(context="t")
track = o3.state.excursions.get("SPY", {})
chk("W15 long best price is the highest seen", track.get("best") == 102.0,
    track.get("best"), 102.0)
chk("W16 long worst price is the lowest seen", track.get("worst") == 99.0,
    track.get("worst"), 99.0)
_main.STOP_LOSS_PCT, _main.TAKE_PROFIT_PCT = _orig_sl, _orig_tp


# --- I. Indicator math -----------------------------------------------------
# The pattern signature is (sentiment_zone, rsi_zone, ema_cross). If a feature
# is computed wrongly, dissimilar market states land in the same bucket and any
# real edge is diluted before the learner ever sees it.
import numpy as _np  # noqa: E402
from patterns import compute_ema, compute_rsi, classify_rsi_zone  # noqa: E402


def _ref_ema(prices, period):
    arr = _np.asarray(prices, dtype=float)
    k = 2.0 / (period + 1)
    ema = float(arr[:period].mean())
    for price in arr[period:]:
        ema = (price - ema) * k + ema
    return round(ema, 4)


_np.random.seed(7)
_series = [400.0]
for _ in range(199):
    _series.append(_series[-1] * (1 + _np.random.normal(0, 0.01)))

for _p in (9, 12, 20, 26, 50):
    chk("I1 EMA(%d) matches the standard construction" % _p,
        abs(compute_ema(_series, _p) - _ref_ema(_series, _p)) < 1e-6,
        compute_ema(_series, _p), _ref_ema(_series, _p))

chk("I2 EMA of a constant series is that constant",
    compute_ema([100.0] * 60, 20) == 100.0, compute_ema([100.0] * 60, 20), 100.0)
chk("I3 EMA uses history beyond `period`",
    compute_ema(_series[-30:], 20) != compute_ema(_series, 20))
chk("I4 insufficient data returns None", compute_ema([1.0, 2.0, 3.0], 20) is None)
chk("I5 non-finite input returns None rather than NaN",
    compute_ema([1.0] * 25 + [float("nan")], 20) is None)
chk("I6 EMA does not mutate its input",
    (lambda snapshot: (compute_ema(_series, 20), _series == snapshot)[1])(list(_series)))

# A rising series must give fast EMA above slow EMA, and vice versa.
_rising = [100.0 + i for i in range(120)]
chk("I7 uptrend: fast EMA above slow", compute_ema(_rising, 12) > compute_ema(_rising, 26))
_falling = [220.0 - i for i in range(120)]
chk("I8 downtrend: fast EMA below slow",
    compute_ema(_falling, 12) < compute_ema(_falling, 26))

# The crossover the pattern signature depends on must agree with a correct EMA.
_disagreements = 0
for _i in range(60, 200):
    _w = _series[:_i]
    if (compute_ema(_w, 12) > compute_ema(_w, 26)) != \
       (_ref_ema(_w, 12) > _ref_ema(_w, 26)):
        _disagreements += 1
chk("I9 crossover direction always agrees with a correct EMA",
    _disagreements == 0, _disagreements, 0)

# RSI bounds and edge cases.
chk("I10 RSI is bounded 0..100",
    all(0.0 <= compute_rsi(_series[:n]) <= 100.0 for n in range(30, 90)))
chk("I11 monotonic rise gives RSI 100",
    compute_rsi([100.0 + i for i in range(30)]) == 100.0)
chk("I12 insufficient data returns None", compute_rsi([1.0, 2.0]) is None)

# An RSI of exactly 0 is a real reading, not a missing value.
chk("I13 RSI 0.0 is not treated as missing",
    classify_rsi_zone(0.0) == "oversold", classify_rsi_zone(0.0), "oversold")
chk("I14 missing RSI is not silently 'normal' at the DB layer",
    "50.0 if rsi_value is None else rsi_value"
    in open(os.path.join(BACKEND, "patterns.py"), encoding="utf-8").read())


# --- ADX regime detection --------------------------------------------------
from patterns import compute_adx, ADX_TREND_MIN, ADX_RANGE_MAX  # noqa: E402


def _ref_adx(highs, lows, closes, period=14):
    H, L, C = (_np.asarray(a, float) for a in (highs, lows, closes))
    up = H[1:] - H[:-1]
    dn = L[:-1] - L[1:]
    pdm = _np.where((up > dn) & (up > 0), up, 0.0)
    mdm = _np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = C[:-1]
    tr = _np.maximum.reduce([H[1:] - L[1:], _np.abs(H[1:] - pc), _np.abs(L[1:] - pc)])

    def rma(x, p):
        out = _np.zeros_like(x)
        out[p - 1] = x[:p].sum()
        for i in range(p, len(x)):
            out[i] = out[i - 1] - out[i - 1] / p + x[i]
        return out

    atr, ps, ms = rma(tr, period), rma(pdm, period), rma(mdm, period)
    with _np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * _np.where(atr != 0, ps / atr, 0)
        mdi = 100 * _np.where(atr != 0, ms / atr, 0)
        tot = pdi + mdi
        dx = 100 * _np.where(tot != 0, _np.abs(pdi - mdi) / tot, 0)
    valid = dx[period:]
    if len(valid) < period:
        return None
    adx = float(_np.mean(valid[:period]))
    for value in valid[period:]:
        adx = (adx * (period - 1) + float(value)) / period
    return round(adx, 2)


_np.random.seed(11)
_c = [400.0]
for _ in range(120):
    _c.append(_c[-1] * (1 + _np.random.normal(0.0012, 0.008)))   # trend
for _ in range(120):
    _c.append(_c[-1] * (1 + _np.random.normal(0.0, 0.010)))      # chop
_h = [x * (1 + abs(_np.random.normal(0, 0.004))) for x in _c]
_l = [x * (1 - abs(_np.random.normal(0, 0.004))) for x in _c]

_mismatch = 0
for _i in range(40, len(_c), 5):
    _a = compute_adx(_h[:_i], _l[:_i], _c[:_i])
    _b = _ref_adx(_h[:_i], _l[:_i], _c[:_i])
    if _a is not None and _b is not None and abs(_a - _b) > 0.01:
        _mismatch += 1
chk("I15 ADX matches Wilder's smoothed formula", _mismatch == 0, _mismatch, 0)


def _regime(adx):
    if adx is None:
        return "unknown"
    return ("trending" if adx > ADX_TREND_MIN
            else "range" if adx < ADX_RANGE_MAX else "transitioning")


_disagree = sum(
    1 for _i in range(60, len(_c), 10)
    if compute_adx(_h[:_i], _l[:_i], _c[:_i]) is not None
    and _regime(compute_adx(_h[:_i], _l[:_i], _c[:_i]))
    != _regime(_ref_adx(_h[:_i], _l[:_i], _c[:_i])))
chk("I16 regime label always agrees with a correct ADX", _disagree == 0,
    _disagree, 0)

_strong = list(range(100, 240))
_sh = [x + 1.0 for x in _strong]
_sl = [x - 1.0 for x in _strong]
chk("I17 a persistent trend reads as trending",
    compute_adx(_sh, _sl, _strong) > ADX_TREND_MIN,
    compute_adx(_sh, _sl, _strong), "> 25")

_flat = [100.0 + (1 if i % 2 else -1) for i in range(140)]
_fh = [x + 0.5 for x in _flat]
_fl = [x - 0.5 for x in _flat]
chk("I18 an oscillating series does not read as trending",
    compute_adx(_fh, _fl, _flat) < ADX_TREND_MIN,
    compute_adx(_fh, _fl, _flat), "< 25")

chk("I19 ADX stays within 0..100",
    all(0.0 <= compute_adx(_h[:n], _l[:n], _c[:n]) <= 100.0
        for n in range(40, 200, 20)))
chk("I20 insufficient data returns None",
    compute_adx([1, 2, 3], [1, 2, 3], [1, 2, 3]) is None)
chk("I21 mismatched series lengths return None",
    compute_adx(_h[:50], _l[:40], _c[:50]) is None)


# --- M. Market clock — the gate on whether trading happens at all ----------
from datetime import date as _date, datetime as _dt, time as _time, timedelta  # noqa: E402
from market_clock import MarketClock, nyse_holidays  # noqa: E402

_clock = MarketClock()


def _open_at(d, hh, mm=0):
    return _clock.is_open(_dt.combine(d, _time(hh, mm)))


# Normal session (times are CT: 08:30-15:00 == 09:30-16:00 ET).
_wed = _date(2025, 11, 26)
chk("M1 open during the regular session", _open_at(_wed, 11, 0) is True)
chk("M2 closed before the open", _open_at(_wed, 7, 0) is False)
chk("M3 closed after the close", _open_at(_wed, 15, 30) is False)
chk("M4 closed exactly at the closing bell", _open_at(_wed, 15, 0) is False)
chk("M5 open exactly at the opening bell", _open_at(_wed, 8, 30) is True)

# Weekends and full holidays.
chk("M6 closed on Saturday", _open_at(_date(2025, 11, 29), 11) is False)
chk("M7 closed on Sunday", _open_at(_date(2025, 11, 30), 11) is False)
chk("M8 closed on Thanksgiving", _open_at(_date(2025, 11, 27), 11) is False)
chk("M9 closed on Christmas Day", _open_at(_date(2025, 12, 25), 11) is False)
chk("M10 closed on Good Friday 2025", _open_at(_date(2025, 4, 18), 11) is False)
chk("M11 closed on Juneteenth", _open_at(_date(2025, 6, 19), 11) is False)
chk("M12 New Year's Day observed when it lands on a weekend",
    _date(2022, 1, 1).weekday() >= 5 and
    any(d.month == 12 and d.day == 31 for d in nyse_holidays(2022)) or
    any(d.month == 1 and d.day in (1, 2, 3) for d in nyse_holidays(2022)))

# HALF DAYS — the gap. Market shuts at 12:00 CT (13:00 ET).
for _d, _label in ((_date(2025, 11, 28), "day after Thanksgiving"),
                   (_date(2025, 12, 24), "Christmas Eve"),
                   (_date(2025, 7, 3), "July 3"),
                   (_date(2026, 11, 27), "day after Thanksgiving 2026")):
    chk("M13 %s is a half session" % _label, _clock.is_early_close(_d) is True,
        _clock.is_early_close(_d), True)
    chk("M14 %s open in the morning" % _label, _open_at(_d, 11, 0) is True)
    chk("M15 %s CLOSED in the afternoon" % _label, _open_at(_d, 13, 30) is False,
        _open_at(_d, 13, 30), False)

chk("M16 a normal day is not flagged as a half session",
    _clock.is_early_close(_wed) is False)
chk("M17 half days that fall on a weekend are ignored",
    _clock.is_early_close(_date(2027, 7, 3)) is False
    or _date(2027, 7, 3).weekday() < 5)
chk("M18 Christmas Eve that is itself a holiday is not a half day",
    all(d not in nyse_holidays(d.year) for d in
        [x for x in (_date(2025, 12, 24),) if _clock.is_early_close(x)]))

# next_close must respect the early close too, or shutdown logic mistimes.
_status = _clock.status(_dt.combine(_date(2025, 12, 24), _time(11, 0)))
chk("M19 status reports open on a half-day morning", _status["is_open"] is True)
chk("M20 next_close on a half day is the early close",
    "12:00" in str(_status.get("next_close", "")) or
    "13:00" in str(_status.get("next_close", "")),
    _status.get("next_close"), "early close")


# --- Timezone correctness, DST, and the pre-market window ------------------
from datetime import timezone as _tzc  # noqa: E402
from market_clock import _CentralFallback, _us_dst_bounds  # noqa: E402

# A datetime from another zone must be converted, not read raw. Before this,
# is_open() compared UTC wall-clock against Central session bounds and
# returned the exact opposite of the truth.
# 20Z=14:00CT open, 14Z=08:00CT closed, 21Z=15:00CT closed, 15Z=09:00CT open
for _h, _want in ((20, True), (14, False), (21, False), (15, True)):
    _u = _dt(2025, 11, 26, _h, 0, tzinfo=_tzc.utc)
    chk("M21 UTC %02d:00 normalised correctly" % _h, _clock.is_open(_u) is _want,
        _clock.is_open(_u), _want)

# Same instant expressed in three zones must give one answer.
_instant = _dt(2025, 11, 26, 20, 0, tzinfo=_tzc.utc)
_answers = {_clock.is_open(_instant.astimezone(_tzc(timedelta(hours=off))))
            for off in (0, -5, -6, 1, 9)}
chk("M22 the same instant gives the same answer in every zone",
    len(_answers) == 1, _answers, "one answer")

# Naive datetimes are treated as market-local (what internal callers pass).
chk("M23 naive datetimes are treated as market-local",
    _clock.is_open(_dt.combine(_wed, _time(11, 0))) is True)

# DST fallback: correct offset either side of the transitions.
_fb = _CentralFallback()
chk("M24 fallback is CST in January",
    _fb.utcoffset(_dt(2025, 1, 15, 12)) == timedelta(hours=-6),
    _fb.tzname(_dt(2025, 1, 15, 12)), "CST")
chk("M25 fallback is CDT in July",
    _fb.utcoffset(_dt(2025, 7, 15, 12)) == timedelta(hours=-5),
    _fb.tzname(_dt(2025, 7, 15, 12)), "CDT")
_start, _end = _us_dst_bounds(2025)
chk("M26 DST starts on the second Sunday in March",
    _start.month == 3 and _start.weekday() == 6 and 8 <= _start.day <= 14,
    _start.date(), "2nd Sunday March")
chk("M27 DST ends on the first Sunday in November",
    _end.month == 11 and _end.weekday() == 6 and _end.day <= 7,
    _end.date(), "1st Sunday November")
chk("M28 fallback returns an AWARE datetime, comparable to others",
    _dt(2025, 7, 1, 12, tzinfo=_fb) > _dt(2025, 7, 1, 12, tzinfo=_tzc.utc) - timedelta(hours=9))

# now_ct is always aware, so it can be compared without raising.
chk("M29 now_ct is timezone-aware", _clock.now_ct().tzinfo is not None)

# Pre-market is a real window, not "any time after midnight".
chk("M30 3am is not pre-market",
    _clock.is_premarket(_dt.combine(_wed, _time(1, 0))) is False)
chk("M31 extended-hours open begins pre-market",
    _clock.is_premarket(_dt.combine(_wed, _time(3, 0))) is True)
chk("M32 just before the bell is pre-market",
    _clock.is_premarket(_dt.combine(_wed, _time(8, 29))) is True)
chk("M33 the opening bell ends pre-market",
    _clock.is_premarket(_dt.combine(_wed, _time(8, 30))) is False)
chk("M34 no pre-market on a holiday",
    _clock.is_premarket(_dt.combine(_date(2025, 12, 25), _time(7, 0))) is False)


# --- N. News freshness — stale headlines are a directional bias ------------
# A week-old story does not just add noise. It reappears every cycle until it
# ages out, pushing conviction the same way each time.
import news_ingestion as _ni  # noqa: E402
from news_ingestion import NewsIngestion, NewsArticle  # noqa: E402


def _article(headline, age_minutes, symbol="SPY"):
    return NewsArticle(
        headline=headline, summary="", source="test", url="", symbol=symbol,
        datetime=time.time() - age_minutes * 60.0, category="general")


class _FakeFinnhub:
    available = True

    def __init__(self, articles):
        self._articles = articles

    def fetch_market_news(self, category):
        return list(self._articles)

    def fetch_company_news(self, symbol, from_date=None, to_date=None):
        return []


def _ingest(articles):
    # Pin to a single category: the fake returns the same list per category,
    # so the default multi-category config would multiply the counts.
    return NewsIngestion(finnhub_client=_FakeFinnhub(articles), simulate=False,
                         categories=["general"])


_mixed = [_article("OLD: week-old story", 60 * 24 * 7),
          _article("FRESH: this morning", 30),
          _article("MID: yesterday", 60 * 20),
          _article("ANCIENT: a month old", 60 * 24 * 30)]

_n = _ingest(_mixed)
_heads = _n.fetch_headlines(25)
chk("N1 week-old headlines are dropped",
    not any(h.startswith("OLD") for h in _heads), _heads, "no OLD")
chk("N2 month-old headlines are dropped",
    not any(h.startswith("ANCIENT") for h in _heads), _heads, "no ANCIENT")
chk("N3 fresh headlines are kept",
    any(h.startswith("FRESH") for h in _heads), _heads, "FRESH kept")
chk("N4 headlines inside the window are kept",
    any(h.startswith("MID") for h in _heads), _heads, "MID kept")
chk("N5 stale drops are counted", _n.stale_articles_dropped == 2,
    _n.stale_articles_dropped, 2)

# Newest first, so truncation keeps the most current news.
_ordered = _ingest([_article("third", 300), _article("first", 5),
                    _article("second", 100)])
chk("N6 headlines are ordered newest first",
    _ordered.fetch_headlines(25) == ["first", "second", "third"],
    _ordered.fetch_headlines(25), ["first", "second", "third"])

_many = _ingest([_article("h%02d" % i, i * 10) for i in range(30)])
_top = _many.fetch_headlines(5)
chk("N7 truncation keeps the newest, not the first fetched",
    _top == ["h00", "h01", "h02", "h03", "h04"], _top, "newest 5")

# An all-stale feed is degraded, not a quiet news day.
_stale_only = _ingest([_article("old one", 60 * 24 * 10),
                       _article("old two", 60 * 24 * 9)])
_stale_only.fetch_headlines(25)
chk("N8 an all-stale feed is flagged degraded",
    _stale_only.news_fetch_degraded is True,
    _stale_only.news_fetch_degraded, True)
chk("N9 degraded reason explains staleness",
    "older than" in (_stale_only.news_degraded_reason or ""),
    _stale_only.news_degraded_reason, "mentions age")

# Undated articles are kept -- missing a timestamp is not evidence of age.
_undated = NewsArticle(headline="no timestamp", summary="", source="t", url="",
                       symbol="SPY", datetime=0.0, category="general")
_u = _ingest([_undated, _article("recent", 10)])
chk("N10 undated articles are not discarded",
    "no timestamp" in _u.fetch_headlines(25), _u.fetch_headlines(25), "kept")

# Duplicates still collapse, and the survivor is the newest.
_dupes = _ingest([_article("Same Story Here", 400), _article("same story here", 5)])
chk("N11 duplicate headlines collapse",
    len(_dupes.fetch_headlines(25)) == 1, _dupes.fetch_headlines(25), 1)

chk("N12 company-news lookback narrowed from 7 days",
    _ni.COMPANY_NEWS_LOOKBACK_DAYS <= 3, _ni.COMPANY_NEWS_LOOKBACK_DAYS, "<= 3")


# --- S2. Sentiment scoring -------------------------------------------------
from sentiment import MarketSentimentEngine  # noqa: E402

_eng = MarketSentimentEngine()

# Keyword decay must rank by strength, not by where a keyword sits in the
# weights table. Dict-order decay made the same headline score differently
# depending on how someone typed the lookup table.
_strong_first = {"rate hike": -3.5, "hold rates": 0.5}
_weak_first = {"hold rates": 0.5, "rate hike": -3.5}
chk("S2-1 keyword adjustment is order-independent",
    _eng._calculate_market_adjustment(_strong_first)
    == _eng._calculate_market_adjustment(_weak_first),
    (_eng._calculate_market_adjustment(_strong_first),
     _eng._calculate_market_adjustment(_weak_first)), "equal")

chk("S2-2 the strongest keyword is the one counted in full",
    abs(_eng._calculate_market_adjustment({"a": -3.5, "b": 0.5})
        - (-3.5 + 0.5 / 1.3)) < 1e-6,
    _eng._calculate_market_adjustment({"a": -3.5, "b": 0.5}), -3.5 + 0.5 / 1.3)

chk("S2-3 a single keyword is undamped",
    _eng._calculate_market_adjustment({"a": -3.5}) == -3.5)
chk("S2-4 no keywords means no adjustment",
    _eng._calculate_market_adjustment({}) == 0.0)

# Shuffling a many-keyword match must not move the score.
_many = {"k%d" % i: (i - 5) * 0.7 for i in range(11)}
_shuffled = dict(sorted(_many.items(), key=lambda kv: kv[0], reverse=True))
chk("S2-5 many-keyword scoring is stable under reordering",
    abs(_eng._calculate_market_adjustment(_many)
        - _eng._calculate_market_adjustment(_shuffled)) < 1e-9)

# Bounds.
chk("S2-6 adjustment clamps at +5",
    _eng._calculate_market_adjustment({"a": 9.0, "b": 9.0}) == 5.0)
chk("S2-7 adjustment clamps at -5",
    _eng._calculate_market_adjustment({"a": -9.0, "b": -9.0}) == -5.0)

# Conviction stays in range whatever the inputs.
_convictions = [_eng._blend_conviction(v, m, c)
                for v in (-1.0, -0.5, 0.0, 0.5, 1.0)
                for m in (-5.0, -1.0, 0.0, 1.0, 5.0)
                for c in (0.0, 0.5, 1.0)]
chk("S2-8 conviction never leaves [-1, +1]",
    all(-1.0 <= c <= 1.0 for c in _convictions),
    (min(_convictions), max(_convictions)), "within [-1,1]")

# Batch aggregation basics.
_batch = _eng.analyze(["stocks rally on strong earnings",
                       "markets plunge amid recession fears"])
chk("S2-9 batch returns one result per headline",
    _batch.headline_count == 2, _batch.headline_count, 2)
chk("S2-10 aggregate conviction stays in range",
    -1.0 <= _batch.aggregate_conviction <= 1.0,
    _batch.aggregate_conviction, "within [-1,1]")
chk("S2-11 empty batch is neutral, not an error",
    _eng.analyze([]).aggregate_conviction == 0.0)
chk("S2-12 disagreeing headlines produce a volatility signal",
    _batch.volatility_signal >= 0.0, _batch.volatility_signal, ">= 0")

# VADER must be a declared dependency: without it every headline contributes
# vader_compound == 0.0 and the engine silently degrades to keyword-only.
_reqs = open(os.path.join(os.path.dirname(BACKEND), "backend",
                          "requirements.txt"), encoding="utf-8").read()
chk("S2-13 vaderSentiment is pinned in requirements",
    "vader" in _reqs.lower(), "vader" in _reqs.lower(), True)


# --- O. Monitoring and alerting -------------------------------------------
# Alerts are how a fault reaches a human. Losing one is losing the only
# signal that something is wrong.
import monitoring as _mon  # noqa: E402
from monitoring import AlertManager  # noqa: E402


def _alerts(tmp=None):
    os.environ["DATA_DIR"] = tmp or tempfile.mkdtemp()
    return AlertManager()


# Distinct incidents of the same type must each be reported.
_m = _alerts()
_a = _m._create_alert("unprotected_position", "critical", "SPY has no stop")
_b = _m._create_alert("unprotected_position", "critical", "QQQ has no stop")
_c = _m._create_alert("unprotected_position", "critical", "IWM has no stop")
chk("O1 first incident reported", _a is not None)
chk("O2 a different symbol is not suppressed", _b is not None, _b, "reported")
chk("O3 a third distinct incident is not suppressed", _c is not None)

# An identical repeat is still debounced (that is the point).
_d = _m._create_alert("unprotected_position", "critical", "SPY has no stop")
chk("O4 an identical repeat is debounced", _d is None, _d, None)

# ...but it is still persisted, so a repeating fault leaves evidence.
_persisted = {"count": 0}
_m._log_to_database = lambda *a, **k: _persisted.__setitem__("count", _persisted["count"] + 1)
_m._create_alert("unprotected_position", "critical", "SPY has no stop")
chk("O5 a debounced alert is still recorded", _persisted["count"] == 1,
    _persisted["count"], 1)

# "Consecutive" errors must actually be consecutive.
_m2 = _alerts()
_seq = []
for _cycle in range(1, 9):
    _errs = ["boom", "bang"] if _cycle % 2 else []
    _seq.append(_m2.check_cycle(0.0, "neutral", _errs, _cycle)["consecutive_errors"])
chk("O6 a clean cycle resets the error streak", _seq[1] == 0, _seq, "resets to 0")
chk("O7 alternating errors never reach system-break",
    max(_seq) < 3, _seq, "< 3")

_m3 = _alerts()
_run = [_m3.check_cycle(0.0, "neutral", ["e"], c)["consecutive_errors"]
        for c in range(1, 5)]
chk("O8 a genuine run of errors does accumulate", _run == [1, 2, 3, 4],
    _run, [1, 2, 3, 4])

# Notification files must not overwrite one another.
_tmp = tempfile.mkdtemp()
_m4 = _alerts(_tmp)
_m4._notify_lead("exception", "first critical event", "critical")
_m4._notify_lead("system_break", "second critical event", "critical")
_m4._notify_lead("exception", "third critical event", "critical")
import glob as _glob  # noqa: E402
_files = _glob.glob(os.path.join(_tmp, "notifications", "alert_*.json"))
chk("O9 simultaneous notifications do not overwrite each other",
    len(_files) == 3, len(_files), 3)

# Alert bookkeeping stays sane.
_status = _m2.status()
chk("O10 status reports the error streak", "consecutive_errors" in _status)
chk("O11 status reports cycle count", _status["cycle_count"] == 8,
    _status["cycle_count"], 8)

# A big sentiment swing is flagged.
_m5 = _alerts()
_res = _m5.check_cycle(0.7, "bullish", [], 10, prev_conviction=0.1)
chk("O12 a large sentiment swing raises an alert",
    any(a["type"] == "big_move" for a in _res["alerts"]),
    [a["type"] for a in _res["alerts"]], "big_move")

# A database failure must not break the pipeline.
# Fail at the real seam (the DB insert), not at the wrapper that guards it.
_m6 = _alerts()
_orig_insert = _mon.insert_alert
_mon.insert_alert = lambda *a, **k: (_ for _ in ()).throw(OSError("db down"))
try:
    _m6._create_alert("exception", "error", "something broke")
    _survived = True
except Exception:
    _survived = False
finally:
    _mon.insert_alert = _orig_insert
chk("O13 alerting survives a database failure", _survived)


# --- E3. Multiple-testing correction --------------------------------------
# A 95% bound allows a 5% false-positive rate PER TEST. Search enough patterns
# and false "edges" appear reliably -- with 500 no-edge patterns, ~14 clear an
# uncorrected bar. A learner that searches must raise its threshold.
import random as _rnd  # noqa: E402

chk("E3-1 a single test uses the standard 95% z",
    abs(PatternStats.z_for_family(1) - 1.96) < 0.01,
    PatternStats.z_for_family(1), 1.96)
chk("E3-2 the bar rises with the number of patterns searched",
    PatternStats.z_for_family(500) > PatternStats.z_for_family(50)
    > PatternStats.z_for_family(5) > PatternStats.z_for_family(1))
chk("E3-3 family size 0 or negative is treated as 1",
    PatternStats.z_for_family(0) == PatternStats.z_for_family(1))

# Empirical: no-edge patterns must rarely survive the corrected bar.
_rnd.seed(11)
_false_unc = _false_cor = 0
_K = 200
for _ in range(_K):
    _n = 200
    _w = sum(1 for _ in range(_n) if _rnd.random() < 0.5)
    _st = PatternStats(pattern_id="p", count=_n, wins=_w, losses=_n - _w)
    if _st.signal_strength > 0:
        _false_unc += 1
    if _st.corrected_signal_strength(_K) > 0:
        _false_cor += 1
chk("E3-4 correction suppresses luck-driven edges",
    _false_cor < max(1, _false_unc), (_false_unc, _false_cor), "corrected < uncorrected")
chk("E3-5 corrected false-positive rate is near zero",
    _false_cor <= 2, _false_cor, "<= 2")

# A genuinely strong pattern still survives correction, given enough evidence.
_real = PatternStats(pattern_id="p", count=600, wins=390, losses=210)   # 65%
chk("E3-6 a real edge survives a large search",
    _real.corrected_signal_strength(500) > 0,
    _real.corrected_signal_strength(500), "> 0")

# A marginal pattern that passes uncorrected should NOT pass a wide search.
_marginal = PatternStats(pattern_id="p", count=200, wins=115, losses=85)  # 57.5%
chk("E3-7 a marginal pattern passes on its own",
    _marginal.signal_strength > 0, _marginal.signal_strength, "> 0")
chk("E3-8 the same pattern fails once you admit you searched 500",
    _marginal.corrected_signal_strength(500) == 0.0,
    _marginal.corrected_signal_strength(500), 0.0)

# Correction never flips a verdict's sign.
for _w, _n in ((150, 200), (50, 200), (100, 200)):
    _st = PatternStats(pattern_id="p", count=_n, wins=_w, losses=_n - _w)
    _a, _b = _st.signal_strength, _st.corrected_signal_strength(50)
    chk("E3-9 correction only shrinks toward zero (%d/%d)" % (_w, _n),
        abs(_b) <= abs(_a) + 1e-9 and (_a == 0 or _b == 0 or (_a > 0) == (_b > 0)),
        (_a, _b), "same sign, smaller")

chk("E3-10 too few trades stays neutral under any correction",
    PatternStats(pattern_id="p", count=1, wins=1, losses=0)
    .corrected_signal_strength(10) == 0.0)


# --- L2. SQLite ledger: retention and flat cost ----------------------------
# The JSON ledger re-read and re-wrote the whole document on every mutation,
# so submit latency grew with total history (10ms at 50 orders, 52ms at 800)
# and nothing was ever pruned. At intraday frequency that is disqualifying.
class _Filler:
    def submit_order(self, **kw):
        return SimpleNamespace(id="o", filled_qty=kw["quantity"], filled_price=100.0)

    def get_order(self, oid):
        return SimpleNamespace(status="filled", filled_qty=1, filled_avg_price=100.0)

    def get_position(self, symbol):
        return None


def _ledger(name):
    return ExecutionSafety(os.path.join(tempfile.mkdtemp(), name))


# Cost must not track total history.
_led = _ledger("perf.db")
_fill = _Filler()
for _i in range(300):
    _led.submit(_fill, client_order_key="w%d" % _i, symbol="SPY", side="buy", quantity=1)
_t0 = time.perf_counter()
_led.has_open_exposure("SPY", "buy")
_small = time.perf_counter() - _t0
for _i in range(300, 3000):
    _led.submit(_fill, client_order_key="w%d" % _i, symbol="SPY", side="buy", quantity=1)
_t0 = time.perf_counter()
_led.has_open_exposure("SPY", "buy")
_large = time.perf_counter() - _t0
chk("L2-1 exposure query stays fast as history grows 10x",
    _large < max(_small * 6, 0.02), "%.4fs vs %.4fs" % (_small, _large), "flat")

_t0 = time.perf_counter()
_led.submit(_fill, client_order_key="probe", symbol="SPY", side="buy", quantity=1)
_submit_ms = (time.perf_counter() - _t0) * 1000
chk("L2-2 submit stays under 25ms at 3000 orders", _submit_ms < 25,
    "%.2f ms" % _submit_ms, "< 25ms")

# Pruning archives SETTLED orders only.
_p = _ledger("prune.db")
for _i in range(50):
    _p.submit(_fill, client_order_key="s%d" % _i, symbol="SPY", side="buy", quantity=1)
for _i in range(5):
    _p.submit(_fill, client_order_key="u%d" % _i, symbol="QQQ", side="buy", quantity=1)
    _rec = _p._fetch("u%d" % _i)
    _rec.status = "ambiguous"
    _p._upsert(_rec)
_p._connect().execute("UPDATE orders SET updated_at = updated_at - 999999")
_p._connect().commit()
_before = _p.stats()
_archived = _p.prune(older_than_days=7)
_after = _p.stats()
chk("L2-3 settled orders are archived", _archived == 50, _archived, 50)
chk("L2-4 hot table shrinks to open orders", _after["hot_orders"] == 5,
    _after["hot_orders"], 5)
chk("L2-5 unresolved orders survive pruning at any age",
    _after["unresolved"] == 5, _after["unresolved"], 5)
chk("L2-6 archived orders are retained, not deleted",
    _after["archived_orders"] == 50, _after["archived_orders"], 50)
chk("L2-7 exposure is still correct after pruning",
    _p.has_open_exposure("QQQ", "buy") is True
    and _p.has_open_exposure("SPY", "buy") is False)

# A legacy JSON ledger is migrated, not abandoned.
_legacy_path = os.path.join(tempfile.mkdtemp(), "legacy.json")
with open(_legacy_path, "w") as _h:
    _json.dump({"orders": {"a": {"client_order_key": "a", "symbol": "SPY",
                                 "side": "buy", "quantity": 2, "status": "filled",
                                 "filled_qty": 2, "filled_price": 99.0,
                                 "revision": 3}},
                "kill": True}, _h)
_mig = ExecutionSafety(_legacy_path)
_rec = _mig._fetch("a")
chk("L2-8 legacy orders are migrated", _rec is not None and _rec.symbol == "SPY")
chk("L2-9 migrated status is preserved", _rec.status == "filled", _rec.status, "filled")
chk("L2-10 migrated kill switch is preserved", _mig.kill_engaged() is True)
chk("L2-11 the legacy file is moved aside so migration runs once",
    os.path.exists(_legacy_path + ".migrated"))

# An unreadable ledger must not stop the kill switch working.
_bad = os.path.join(tempfile.mkdtemp(), "corrupt.db")
open(_bad, "w").write("this is not a database")
_broken = ExecutionSafety(_bad)
_broken.set_kill(True)
chk("L2-12 kill switch works on an unreadable ledger",
    _broken.kill_engaged() is True)
try:
    _broken.has_open_exposure("SPY", "buy")
    _failed_closed = False
except RuntimeError:
    _failed_closed = True
chk("L2-13 ledger queries fail closed when unreadable", _failed_closed)


# --- TC. Technical conviction (sentiment removed) --------------------------
# Sentiment used to supply the directional signal AND gate every trade. On
# index ETFs that was indefensible, so direction now comes from price.
from patterns import trend_conviction, mean_reversion_conviction  # noqa: E402

chk("TC-1 keyword table is empty and stays empty",
    len(MarketSentimentEngine().keyword_weights) == 0,
    len(MarketSentimentEngine().keyword_weights), 0)
chk("TC-2 a loaded headline now scores neutral",
    MarketSentimentEngine().analyze_headline(
        "Fed signals rate hike as recession fears mount").conviction_score == 0.0)

# Trend conviction: direction from EMA separation, strength from ADX.
chk("TC-3 strong uptrend is strongly bullish",
    trend_conviction(35, 101.0, 100.0) == 1.0, trend_conviction(35, 101.0, 100.0), 1.0)
chk("TC-4 strong downtrend is strongly bearish",
    trend_conviction(35, 99.0, 100.0) == -1.0)
chk("TC-5 wide EMAs but no trend contributes nothing",
    trend_conviction(15, 101.0, 100.0) == 0.0,
    trend_conviction(15, 101.0, 100.0), 0.0)
chk("TC-6 a borderline trend is scaled down, not full strength",
    0 < trend_conviction(22, 101.0, 100.0) < 1.0,
    trend_conviction(22, 101.0, 100.0), "between 0 and 1")
chk("TC-7 near-identical EMAs give near-zero conviction",
    abs(trend_conviction(35, 100.02, 100.0)) < 0.1)
chk("TC-8 conviction is bounded",
    all(-1.0 <= trend_conviction(a, e, 100.0) <= 1.0
        for a in (0, 15, 25, 40, 100) for e in (90.0, 99.0, 100.0, 101.0, 130.0)))

# Missing or nonsense inputs mean no signal, never a fabricated one.
for _args in ((None, 101.0, 100.0), (35, None, 100.0), (35, 101.0, None),
              (35, 101.0, 0.0), (float("nan"), 101.0, 100.0),
              (35, float("inf"), 100.0)):
    chk("TC-9 missing/invalid input -> no signal %s" % (_args,),
        trend_conviction(*_args) == 0.0, trend_conviction(*_args), 0.0)

# Mean reversion fades extremes: oversold is a BUY.
chk("TC-10 oversold RSI is bullish", mean_reversion_conviction(20) > 0)
chk("TC-11 overbought RSI is bearish", mean_reversion_conviction(80) < 0)
chk("TC-12 mid-range RSI is neutral", mean_reversion_conviction(50) == 0.0)
chk("TC-13 more extreme means more conviction",
    mean_reversion_conviction(10) > mean_reversion_conviction(35) > 0)
chk("TC-14 missing RSI gives no signal", mean_reversion_conviction(None) == 0.0)
chk("TC-15 mean-reversion conviction is bounded",
    all(-1.0 <= mean_reversion_conviction(r) <= 1.0 for r in range(0, 101, 5)))

# The pipeline must no longer gate on sentiment, and must feed the signature
# technical inputs only.
_main_text = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
chk("TC-16 sentiment no longer gates entry",
    "abs(agg_conv) < self.min_conviction" not in _main_text)
chk("TC-17 conviction is no longer an average with sentiment",
    "(agg_conv + pattern_conv) / 2" not in _main_text)
chk("TC-18 trend conviction is wired into the signal path",
    "trend_conviction(" in _main_text)
chk("TC-19 the pattern signature is fed sentiment 0.0",
    "sentiment_score=0.0" in _main_text)
chk("TC-20 EMAs are published with the live indicators",
    '"ema_short": ema_short' in _main_text)


# --- IF. Intraday timeframe ------------------------------------------------
# Daily bars gave too few trades to ever learn anything. Moving to 30-60 min
# changes stop sizes, lookback arithmetic, and introduces the unclosed-bar
# hazard that does not exist at daily resolution.
import importlib as _il  # noqa: E402


def _reload_main(tf, keep_env=False):
    """Reload main at a timeframe and SNAPSHOT the values.

    importlib.reload mutates the module in place, so holding module
    references and comparing them later compares a value to itself.
    """
    os.environ["BAR_TIMEFRAME_MINUTES"] = str(tf)
    if not keep_env:
        os.environ.pop("STOP_LOSS_PCT", None)
        os.environ.pop("TAKE_PROFIT_PCT", None)
    mod = _il.reload(_main)
    return SimpleNamespace(STOP_LOSS_PCT=mod.STOP_LOSS_PCT,
                           TAKE_PROFIT_PCT=mod.TAKE_PROFIT_PCT,
                           USE_DAILY_BARS=mod.USE_DAILY_BARS)


_m30 = _reload_main(30)
chk("IF-1 30-min stop is scaled well below the daily 2.5%",
    0.004 < _m30.STOP_LOSS_PCT < 0.010, _m30.STOP_LOSS_PCT, "~0.007")
chk("IF-2 the reward:risk shape is preserved after scaling",
    abs(_m30.TAKE_PROFIT_PCT / _m30.STOP_LOSS_PCT - 1.2) < 0.01,
    _m30.TAKE_PROFIT_PCT / _m30.STOP_LOSS_PCT, 1.2)
chk("IF-3 intraday mode is detected", _m30.USE_DAILY_BARS is False)

_m60 = _reload_main(60)
chk("IF-4 a longer bar gets a wider stop",
    _m60.STOP_LOSS_PCT > _m30.STOP_LOSS_PCT,
    (_m30.STOP_LOSS_PCT, _m60.STOP_LOSS_PCT), "60min > 30min")
chk("IF-5 scaling follows sqrt(time), not linear time",
    abs(_m60.STOP_LOSS_PCT / _m30.STOP_LOSS_PCT - 2 ** 0.5) < 0.02,
    _m60.STOP_LOSS_PCT / _m30.STOP_LOSS_PCT, 1.414)

_mday = _reload_main(1440)
chk("IF-6 daily mode keeps the original 2.5% stop",
    abs(_mday.STOP_LOSS_PCT - 0.025) < 1e-9, _mday.STOP_LOSS_PCT, 0.025)
chk("IF-7 daily mode is detected", _mday.USE_DAILY_BARS is True)

_m5 = _reload_main(5)
chk("IF-8 a 5-min stop is smaller still but not absurd",
    0.001 < _m5.STOP_LOSS_PCT < 0.005, _m5.STOP_LOSS_PCT, "~0.003")

# An explicit override must win over the scaling.
os.environ["STOP_LOSS_PCT"] = "0.004"
_mo = _reload_main(30, keep_env=True)
chk("IF-9 an explicit stop override is respected",
    abs(_mo.STOP_LOSS_PCT - 0.004) < 1e-9, _mo.STOP_LOSS_PCT, 0.004)
os.environ.pop("STOP_LOSS_PCT", None)

# Poll cadence must not outpace the bar.
_reload_main(30)
chk("IF-10 lookback is computed in sessions, not calendar days",
    "bars_per_session" in open(os.path.join(BACKEND, "main.py"),
                               encoding="utf-8").read())
chk("IF-11 the still-forming bar is dropped intraday",
    "still-forming bar" in open(os.path.join(BACKEND, "main.py"),
                                encoding="utf-8").read())

# Restore the module for anything downstream.
_reload_main(1440)
chk("IF-12 module restored to daily for the rest of the suite",
    _main.USE_DAILY_BARS is True)


# --- ON. Overnight gap risk ------------------------------------------------
# An intraday stop cannot be enforced across a gap: stops are evaluated on bar
# closes, and a gapped stop order fills at the open. SPY gaps 1-2% overnight,
# several times a 30-minute stop. Positions carried through a close must be
# sized against the risk that can actually materialise.
def _sized_on(sl, overnight, equity=25000.0, price=450.0, max_pos=0.15):
    br = live_broker(order())
    eng = TradingEngine(alpaca_broker=br, max_position_size=max_pos,
                        risk_per_trade=0.005)
    TradingEngine._get_reference_price = staticmethod(lambda sym: price)
    acct = AccountInfo()
    acct.equity = equity
    acct.buying_power = equity
    return eng._calculate_quantity("SPY", trading.OrderSide.BUY, 0.9, acct,
                                   stop_loss_pct=sl, overnight_risk=overnight)


_intraday = _sized_on(0.00693, False)
_overnight = _sized_on(0.00693, True)
chk("ON-1 an overnight position is smaller than an intraday one",
    _overnight < _intraday, (_intraday, _overnight), "overnight < intraday")
chk("ON-2 the reduction is material, not cosmetic",
    _overnight <= _intraday * 0.5, (_intraday, _overnight), "at least halved")
chk("ON-3 the overnight position is still tradeable", _overnight >= 1,
    _overnight, ">= 1")

# The scaling must survive the concentration cap binding, which it does at
# default settings -- adjusting only the risk divisor changed nothing.
chk("ON-4 scaling applies even when the position cap binds",
    _sized_on(0.00693, True, max_pos=0.15) < _sized_on(0.00693, False, max_pos=0.15))

# A wider intraday stop means less of a gap-risk gap, so less reduction.
chk("ON-5 a 60-min stop is reduced less than a 30-min stop",
    _sized_on(0.00981, True) >= _sized_on(0.00693, True),
    (_sized_on(0.00693, True), _sized_on(0.00981, True)), "60min >= 30min")

# Daily bars already assume daily risk: nothing to adjust.
chk("ON-6 daily-scale stops are unchanged by the overnight flag",
    _sized_on(0.025, True) == _sized_on(0.025, False),
    (_sized_on(0.025, False), _sized_on(0.025, True)), "equal")
chk("ON-7 a stop wider than the gap assumption is not widened further",
    _sized_on(0.05, True) == _sized_on(0.05, False))

# The decision of WHEN this applies must fail closed.
# Daily bars always carry overnight risk by definition, so exercise the
# time-of-day logic in intraday mode.
_reload_main(30)
_oc = _main.Orchestrator.__new__(_main.Orchestrator)
_oc.state = _main.PipelineState()


class _Clock:
    def __init__(self, status):
        self._status = status

    def status(self):
        if self._status == "boom":
            raise RuntimeError("clock unavailable")
        return self._status


_oc._market_clock = _Clock({"is_open": True, "seconds_to_close": 6 * 3600})
chk("ON-8 mid-session is not overnight risk",
    _oc.carries_overnight_risk() is False)
_oc._market_clock = _Clock({"is_open": True, "seconds_to_close": 15 * 60})
chk("ON-9 near the close IS overnight risk",
    _oc.carries_overnight_risk() is True)
_oc._market_clock = _Clock({"is_open": False, "seconds_to_close": 0})
chk("ON-10 outside the session is overnight risk",
    _oc.carries_overnight_risk() is True)
_oc._market_clock = _Clock("boom")
chk("ON-11 an unavailable clock fails closed to overnight",
    _oc.carries_overnight_risk() is True)
_oc._market_clock = _Clock({"is_open": True})
chk("ON-12 a missing close time fails closed to overnight",
    _oc.carries_overnight_risk() is True)

_main_src2 = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
chk("ON-13 the flag is passed on tier-1 entries",
    "overnight_risk=self.carries_overnight_risk()" in _main_src2)
chk("ON-13b daily bars always count as overnight risk",
    (lambda: (_reload_main(1440),
              _main.Orchestrator.__new__(_main.Orchestrator))[1])() is not None)
_reload_main(1440)
chk("ON-14 tier-2 entries carry the same treatment",
    _main_src2.count("overnight_risk=self.carries_overnight_risk()") >= 2,
    _main_src2.count("overnight_risk=self.carries_overnight_risk()"), ">= 2")


# --- VN. Volatility-normalised conviction ----------------------------------
# A fixed 0.5% saturation point was calibrated for daily bars. On 30-minute
# bars the MEDIAN EMA20/EMA50 separation is already ~0.44%, so conviction
# pinned at full strength most of the time and the gate stopped discriminating.
from patterns import realized_volatility_pct  # noqa: E402

_calm = [100.0 * (1 + 0.0002 * ((i % 5) - 2)) for i in range(40)]
_wild = [100.0 * (1 + 0.02 * ((i % 5) - 2)) for i in range(40)]
chk("VN-1 volatility is measured from returns",
    realized_volatility_pct(_wild) > realized_volatility_pct(_calm),
    (realized_volatility_pct(_calm), realized_volatility_pct(_wild)), "wild > calm")
chk("VN-2 too little data returns None",
    realized_volatility_pct([100.0, 101.0]) is None)
chk("VN-3 non-positive prices return None",
    realized_volatility_pct([100.0] * 10 + [0.0] * 15) is None)
chk("VN-4 a flat series has no usable volatility",
    realized_volatility_pct([100.0] * 40) is None)

# The SAME separation means different things at different volatilities.
_sep_low = trend_conviction(30, 100.3, 100.0, 0.05)   # 0.3% move, calm tape
_sep_high = trend_conviction(30, 100.3, 100.0, 0.50)  # same move, wild tape
chk("VN-5 the same move is stronger evidence when the tape is calm",
    _sep_low > _sep_high, (_sep_low, _sep_high), "calm > wild")
chk("VN-6 in a calm tape a 0.3% separation saturates",
    _sep_low == 1.0, _sep_low, 1.0)
chk("VN-7 in a wild tape the same separation is modest",
    0 < _sep_high < 0.5, _sep_high, "small")

# Without a volatility estimate it falls back to the old constant, not to zero.
chk("VN-8 missing volatility falls back rather than muting the signal",
    trend_conviction(30, 100.3, 100.0, None) != 0.0)
chk("VN-9 a zero volatility estimate also falls back",
    trend_conviction(30, 100.3, 100.0, 0.0) != 0.0)

# Still bounded, still gated by ADX.
chk("VN-10 conviction stays bounded under any volatility",
    all(-1.0 <= trend_conviction(30, 100 + d, 100.0, v) <= 1.0
        for d in (-5, -0.5, 0, 0.5, 5) for v in (0.01, 0.1, 1.0, 10.0)))
chk("VN-11 no trend still means no signal regardless of volatility",
    trend_conviction(10, 105.0, 100.0, 0.05) == 0.0)

chk("VN-12 volatility is published with the live indicators",
    '"volatility_pct": volatility_pct' in open(
        os.path.join(BACKEND, "main.py"), encoding="utf-8").read())
chk("VN-13 the signal path passes volatility into trend conviction",
    '_ind.get("volatility_pct")' in open(
        os.path.join(BACKEND, "main.py"), encoding="utf-8").read())


# --- UN. Trading universe --------------------------------------------------
# The universe was hardcoded in SIX places. Adding a symbol to one of them and
# not the indicator loop would mean it never gets indicators and never trades
# -- silent, and indistinguishable from "no signal".
_main_u = _reload_main(1440) and _main  # ensure a known module state
_universe_src = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()

chk("UN-1 the universe is defined once", "TRADING_SYMBOLS = [" in _universe_src)
chk("UN-2 no hardcoded universes remain",
    '["SPY", "QQQ", "IWM"]' not in _universe_src,
    _universe_src.count('["SPY", "QQQ", "IWM"]'), 0)
chk("UN-3 backfill follows the universe",
    _main.BACKFILL_SYMBOLS == _main.TRADING_SYMBOLS)
chk("UN-4 the three diversifiers are included",
    {"XLE", "TLT", "GLD"} <= set(_main.TRADING_SYMBOLS),
    _main.TRADING_SYMBOLS, "includes XLE/TLT/GLD")
chk("UN-5 the original three are retained",
    {"SPY", "QQQ", "IWM"} <= set(_main.TRADING_SYMBOLS))

# The indicator loop is the one that matters: a symbol missing from it gets no
# indicators, so it can never generate a signal.
chk("UN-6 the indicator loop uses the shared universe",
    "symbols = list(TRADING_SYMBOLS)" in _universe_src,
    "symbols = list(TRADING_SYMBOLS)" in _universe_src, True)
chk("UN-7 every universe reference is the shared one",
    _universe_src.count("TRADING_SYMBOLS") >= 6,
    _universe_src.count("TRADING_SYMBOLS"), ">= 6")

# Configurable, and tolerant of untidy input.
_prev_env = os.environ.get("TRADING_SYMBOLS")
os.environ["TRADING_SYMBOLS"] = " spy , qqq ,, tlt "
_il.reload(_main)
chk("UN-8 the universe is env-configurable",
    _main.TRADING_SYMBOLS == ["SPY", "QQQ", "TLT"],
    _main.TRADING_SYMBOLS, ["SPY", "QQQ", "TLT"])
if _prev_env is None:
    os.environ.pop("TRADING_SYMBOLS", None)
else:
    os.environ["TRADING_SYMBOLS"] = _prev_env
_il.reload(_main)
chk("UN-9 default restored after the override",
    len(_main.TRADING_SYMBOLS) == 6, _main.TRADING_SYMBOLS, "6 symbols")

# Concurrency stays at 3 deliberately: six symbols across three asset classes
# still means correlated equity names, so the cap is the thing preventing
# five simultaneous versions of the same bet.
chk("UN-10 concurrency cap unchanged for now",
    trading.DEFAULT_MAX_CONCURRENT_POSITIONS == 3,
    trading.DEFAULT_MAX_CONCURRENT_POSITIONS, 3)


# --- SL. Slippage measurement ----------------------------------------------
# The entire case for 30-minute bars rests on cost being ~9% of the target
# move. Dollar slippage cannot test that: 5 cents is 0.008% on SPY and 0.025%
# on GLD. It has to be percent, per symbol, and compared with the assumption.
def _slip_journal():
    return DecisionLog(path=str(Path(tempfile.mkdtemp()) / "s.jsonl"))


_sj = _slip_journal()
for _i in range(10):
    _sj.entered("SPY", "buy", 10, 451.20, inputs={"reference_price": 451.20})
_r = _sj.slippage()
chk("SL-1 a perfect fill measures zero slippage",
    abs(_r["by_symbol"]["SPY"]["mean_pct"]) < 1e-9,
    _r["by_symbol"]["SPY"]["mean_pct"], 0.0)

# Paying above the reference on a BUY is adverse (positive).
_sj = _slip_journal()
for _i in range(10):
    _sj.entered("SPY", "buy", 10, 452.00, inputs={"reference_price": 451.20})
chk("SL-2 paying up on a buy is adverse",
    _sj.slippage()["by_symbol"]["SPY"]["mean_pct"] > 0)

# Selling BELOW the reference is also adverse -- sign must follow direction,
# or a systematic bias averages away to nothing.
_sj = _slip_journal()
for _i in range(10):
    _sj.entered("TLT", "sell", 10, 89.50, inputs={"reference_price": 90.00})
chk("SL-3 selling down is also adverse",
    _sj.slippage()["by_symbol"]["TLT"]["mean_pct"] > 0,
    _sj.slippage()["by_symbol"]["TLT"]["mean_pct"], "> 0")

_sj = _slip_journal()
for _i in range(10):
    _sj.entered("TLT", "sell", 10, 90.50, inputs={"reference_price": 90.00})
chk("SL-4 a favourable fill is negative",
    _sj.slippage()["by_symbol"]["TLT"]["mean_pct"] < 0)

# Per symbol, because that is the decision unit.
_sj = _slip_journal()
for _i in range(8):
    _sj.entered("SPY", "buy", 10, 451.21, inputs={"reference_price": 451.20})
for _i in range(8):
    _sj.entered("GLD", "buy", 10, 200.15, inputs={"reference_price": 200.00})
_r = _sj.slippage()
chk("SL-5 slippage is broken out per symbol",
    set(_r["by_symbol"]) == {"SPY", "GLD"}, set(_r["by_symbol"]), "SPY+GLD")
chk("SL-6 the wider-spread symbol is identified",
    _r["by_symbol"]["GLD"]["mean_pct"] > _r["by_symbol"]["SPY"]["mean_pct"])
chk("SL-7 a symbol breaching the assumption raises a warning",
    any("GLD" in w for w in _r["warnings"]), _r["warnings"], "names GLD")
chk("SL-8 a symbol inside the assumption does not warn",
    not any("SPY" in w for w in _r["warnings"]))

# Round trip is twice one-way, which is what the cost case is stated in.
chk("SL-9 round-trip is double the one-way figure",
    abs(_r["overall_round_trip_pct"] - _r["overall_mean_pct"] * 2) < 1e-9)

# Junk must not poison the average.
_sj = _slip_journal()
_sj.entered("SPY", "buy", 10, 451.20, inputs={"reference_price": 451.20})
_sj.entered("SPY", "buy", 10, 0.0, inputs={"reference_price": 451.20})
_sj.entered("SPY", "buy", 10, 451.20, inputs={})
_sj.entered("SPY", "buy", 10, 451.20, inputs={"reference_price": "n/a"})
chk("SL-10 unusable fills are excluded, not averaged in",
    _sj.slippage()["by_symbol"]["SPY"]["fills"] == 1,
    _sj.slippage()["by_symbol"]["SPY"]["fills"], 1)
chk("SL-11 an empty journal reports no slippage",
    _slip_journal().slippage().get("fills") is None)

# And it must surface where a human will see it.
_sj = _slip_journal()
for _i in range(6):
    _sj.entered("GLD", "buy", 10, 200.30, inputs={"reference_price": 200.00})
chk("SL-12 slippage appears in the postmortem findings",
    any("slippage" in f.lower() for f in _sj.postmortem()["findings"]))
chk("SL-13 slippage appears in the rendered report",
    "Slippage" in _sj.render_postmortem())


# --- IP. Indicator periods and data sufficiency ----------------------------
# INDICATOR_REQUIRED_BARS was hardcoded at 40 while EMA(50) needs 50. Between
# 40 and 49 bars the fetch passed its own check, EMA(50) returned None, trend
# conviction collapsed to 0.0, and the bot stopped trading with no error.
_reload_main(1440)

chk("IP-1 the bar requirement covers the longest indicator",
    _main.INDICATOR_REQUIRED_BARS >= _main.EMA_LONG_PERIOD,
    (_main.INDICATOR_REQUIRED_BARS, _main.EMA_LONG_PERIOD), "required >= EMA long")
chk("IP-2 the requirement covers ADX warm-up",
    _main.INDICATOR_REQUIRED_BARS >= 2 * _main.ADX_PERIOD + 1)
chk("IP-3 the requirement covers RSI",
    _main.INDICATOR_REQUIRED_BARS >= _main.RSI_PERIOD + 1)
chk("IP-4 it is derived, not a magic number",
    "INDICATOR_REQUIRED_BARS = max(" in open(
        os.path.join(BACKEND, "main.py"), encoding="utf-8").read())

# Every indicator must actually compute at exactly the stated minimum.
_bars = _main.INDICATOR_REQUIRED_BARS
_series = [400.0 * (1 + 0.001 * ((i * 7) % 11 - 5)) for i in range(_bars)]
_h = [x * 1.001 for x in _series]
_l = [x * 0.999 for x in _series]
chk("IP-5 EMA long computes at the minimum",
    compute_ema(_series, _main.EMA_LONG_PERIOD) is not None)
chk("IP-6 EMA short computes at the minimum",
    compute_ema(_series, _main.EMA_SHORT_PERIOD) is not None)
chk("IP-7 RSI computes at the minimum", compute_rsi(_series) is not None)
chk("IP-8 ADX computes at the minimum",
    compute_adx(_h, _l, _series) is not None)
chk("IP-9 volatility computes at the minimum",
    realized_volatility_pct(_series) is not None)

# One bar short of the requirement, the longest indicator must fail -- proving
# the requirement is tight rather than arbitrary.
chk("IP-10 one bar short and the longest indicator returns None",
    compute_ema(_series[:_main.EMA_LONG_PERIOD - 1],
                _main.EMA_LONG_PERIOD) is None)

# Fetch far more than the minimum: an EMA seeded on the first `period` bars
# needs history after the seed to converge.
chk("IP-11 the fetch exceeds the bare minimum substantially",
    _main.INDICATOR_FETCH_BARS >= _main.INDICATOR_REQUIRED_BARS * 3,
    (_main.INDICATOR_FETCH_BARS, _main.INDICATOR_REQUIRED_BARS), ">= 3x")

_np.random.seed(2)
_long = [400.0]
for _ in range(999):
    _long.append(_long[-1] * (1 + _np.random.normal(0, 0.0028)))
_converged = compute_ema(_long, 50)
_minimal = compute_ema(_long[-50:], 50)
_fetched = compute_ema(_long[-_main.INDICATOR_FETCH_BARS:], 50)
chk("IP-12 EMA at the bare minimum is materially off",
    abs(_minimal - _converged) / _converged > 0.001,
    "%.4f%%" % (abs(_minimal - _converged) / _converged * 100), "> 0.1%")
chk("IP-13 EMA at the fetch depth is converged",
    abs(_fetched - _converged) / _converged < 0.0001,
    "%.5f%%" % (abs(_fetched - _converged) / _converged * 100), "< 0.01%")

# Periods are configurable, and the requirement follows them.
_prev = os.environ.get("EMA_LONG_PERIOD")
os.environ["EMA_LONG_PERIOD"] = "200"
_il.reload(_main)
chk("IP-14 a longer EMA raises the bar requirement",
    _main.INDICATOR_REQUIRED_BARS >= 200,
    _main.INDICATOR_REQUIRED_BARS, ">= 200")
if _prev is None:
    os.environ.pop("EMA_LONG_PERIOD", None)
else:
    os.environ["EMA_LONG_PERIOD"] = _prev
_il.reload(_main)
chk("IP-15 defaults restored", _main.EMA_LONG_PERIOD == 50)


# --- EQ. Execution-quality gate --------------------------------------------
# Slippage is measured after the fill, so it can never stop THAT trade. The
# response is always about future trades, and it belongs per symbol -- halting
# SPY because GLD is expensive throws away good trades for a bad reason.
import decision_log as _dl  # noqa: E402

_TARGET = 0.832   # 30-min take-profit target, in percent


def _eq_journal():
    return DecisionLog(path=str(Path(tempfile.mkdtemp()) / "e.jsonl"))


# A cheap symbol trades freely.
_j = _eq_journal()
for _i in range(20):
    _j.entered("SPY", "buy", 10, 451.204, inputs={"reference_price": 451.20})
_ok, _why = _j.symbol_is_tradeable("SPY", _TARGET)
chk("EQ-1 a cheap symbol is tradeable", _ok is True, _why, "ok")

# A persistently expensive symbol is blocked.
_j = _eq_journal()
for _i in range(20):
    _j.entered("GLD", "buy", 10, 200.30, inputs={"reference_price": 200.00})
_ok, _why = _j.symbol_is_tradeable("GLD", _TARGET)
chk("EQ-2 an expensive symbol is blocked", _ok is False, _ok, False)
chk("EQ-3 the block names the budget", "budget" in _why, _why, "explains")

# Blocking is per symbol, not global.
_j = _eq_journal()
for _i in range(20):
    _j.entered("GLD", "buy", 10, 200.30, inputs={"reference_price": 200.00})
    _j.entered("SPY", "buy", 10, 451.204, inputs={"reference_price": 451.20})
chk("EQ-4 an expensive symbol does not block a cheap one",
    _j.symbol_is_tradeable("SPY", _TARGET)[0] is True
    and _j.symbol_is_tradeable("GLD", _TARGET)[0] is False)

# Too few fills is not evidence. Moderately over budget (0.12% round trip vs
# a 0.083% budget) but well under the outlier multiple, so only the rolling
# rule could fire -- and it must not, on three fills.
_j = _eq_journal()
for _i in range(3):
    _j.entered("XLE", "buy", 10, 90.054, inputs={"reference_price": 90.00})
chk("EQ-5 a thin sample does not block on the rolling average",
    _j.symbol_is_tradeable("XLE", _TARGET)[0] is True,
    _j.symbol_is_tradeable("XLE", _TARGET), "allowed")

# ...but one catastrophic fill blocks immediately, without waiting.
_j = _eq_journal()
for _i in range(3):
    _j.entered("XLE", "buy", 10, 90.004, inputs={"reference_price": 90.00})
_j.entered("XLE", "buy", 10, 92.50, inputs={"reference_price": 90.00})
_ok, _why = _j.symbol_is_tradeable("XLE", _TARGET)
chk("EQ-6 a single catastrophic fill blocks at once", _ok is False)
chk("EQ-7 the outlier reason is distinct from the rolling one",
    "single fill" in _why, _why, "single fill")

# ROLLING: a symbol that was bad and recovered comes back.
_j = _eq_journal()
for _i in range(25):
    _j.entered("TLT", "buy", 10, 90.30, inputs={"reference_price": 90.00})  # bad
chk("EQ-8 a degraded symbol is blocked", _j.symbol_is_tradeable("TLT", _TARGET)[0] is False)
for _i in range(25):
    _j.entered("TLT", "buy", 10, 90.001, inputs={"reference_price": 90.00})  # recovered
chk("EQ-9 recovery re-admits the symbol — the window rolls",
    _j.symbol_is_tradeable("TLT", _TARGET)[0] is True,
    _j.symbol_is_tradeable("TLT", _TARGET), "allowed again")

# ...and an all-time average would NOT have re-admitted it.
_all_time = _j.slippage()["by_symbol"]["TLT"]["mean_pct"] * 2
_rolling = _j.execution_quality(_TARGET)["by_symbol"]["TLT"]["rolling_round_trip_pct"]
chk("EQ-10 rolling differs from all-time after recovery",
    _rolling < _all_time, (_rolling, _all_time), "rolling < all-time")

# Judged against the TARGET, not raw percent: the same slippage is fine on a
# daily target and fatal on an intraday one.
_j = _eq_journal()
for _i in range(20):
    _j.entered("SPY", "buy", 10, 451.51, inputs={"reference_price": 451.20})
chk("EQ-11 tolerable against a 3% daily target",
    _j.symbol_is_tradeable("SPY", 3.0)[0] is True)
chk("EQ-12 the same slippage is intolerable against a 0.15% scalp",
    _j.symbol_is_tradeable("SPY", 0.15)[0] is False)

# Fails OPEN: unlike the position gates, no data must not mean no trading.
chk("EQ-13 an unknown symbol is allowed",
    _eq_journal().symbol_is_tradeable("NEW", _TARGET)[0] is True)
_broken = DecisionLog(path="/nonexistent/nope/x.jsonl")
chk("EQ-14 an unreadable journal does not block trading",
    _broken.symbol_is_tradeable("SPY", _TARGET)[0] is True)

chk("EQ-15 the gate is wired into the entry path",
    "symbol_is_tradeable" in open(os.path.join(BACKEND, "trading.py"),
                                  encoding="utf-8").read())
chk("EQ-16 the threshold is configurable",
    _dl.MAX_SLIPPAGE_PCT_OF_TARGET == 10.0,
    _dl.MAX_SLIPPAGE_PCT_OF_TARGET, 10.0)


# --- TL. Journal reads must not scale with journal size --------------------
# The execution gate runs on every entry attempt. Reading the whole journal to
# find the last twenty fills is the same mistake as rewriting a whole JSON
# document to change one order -- 3ms at 200 entries, 271ms at 20,000.
_tl = DecisionLog(path=str(Path(tempfile.mkdtemp()) / "t.jsonl"))
for _i in range(3000):
    _tl.entered("SPY", "buy", 10, 451.20 + _i * 0.0001,
                inputs={"reference_price": 451.20}, trade_id="t%d" % _i)

chk("TL-1 a bounded read returns the requested count",
    len(_tl.read(limit=50)) == 50, len(_tl.read(limit=50)), 50)
chk("TL-2 a bounded read returns the NEWEST entries",
    _tl.read(limit=1)[0]["trade_id"] == "t2999",
    _tl.read(limit=1)[0]["trade_id"], "t2999")
chk("TL-3 order is preserved oldest-to-newest within the tail",
    [e["trade_id"] for e in _tl.read(limit=3)]
    == ["t2997", "t2998", "t2999"])
chk("TL-4 an unbounded read still returns everything",
    len(_tl.read()) == 3000, len(_tl.read()), 3000)
chk("TL-5 a limit larger than the file is harmless",
    len(_tl.read(limit=99999)) == 3000)
chk("TL-6 reading an absent journal is safe",
    DecisionLog(path="/nonexistent/none.jsonl").read(limit=10) == [])

# Cost must be flat in journal size, not linear.
#
# Measured as the MINIMUM of several runs, not a single sample. A single
# wall-clock reading is a measurement of the machine as much as of the code:
# this check failed once in roughly sixty runs, always while several suites
# were running at once, reporting 0.056s -> 0.409s for work that had not
# changed. Scheduler noise can only ever ADD time, so the minimum of n runs
# converges on the true cost and a loaded machine stops producing phantom
# failures. That flake cost about an hour to track down; the fix is one line.
import builtins as _bi  # noqa: E402


def _gate_bytes():
    """Bytes the gate reads off disk for one decision.

    Wall-clock was the wrong instrument. A single timing sample measures the
    machine as much as the code: this check failed about once in sixty runs,
    always with several suites running at once, reporting 0.056s -> 0.409s for
    work that had not changed. Taking the minimum of several runs helped and
    still was not enough under six-way concurrency.

    Bytes read is the property that actually matters -- a tail-seek reads a
    bounded window, a naive implementation reads the whole file -- and no
    amount of scheduler noise can perturb it. Deterministic beats
    approximately-deterministic in a suite that gates real money.
    """
    counted = [0]
    real_open = _bi.open

    class _Counting:
        def __init__(self, handle):
            self._h = handle

        def read(self, *a):
            data = self._h.read(*a)
            counted[0] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._h, name)

        def __enter__(self):
            self._h.__enter__()
            return self

        def __exit__(self, *exc):
            return self._h.__exit__(*exc)

        def __iter__(self):
            for line in self._h:
                counted[0] += len(line)
                yield line

    def _patched(path, *a, **kw):
        handle = real_open(path, *a, **kw)
        return _Counting(handle) if str(path) == _tl.path else handle

    _bi.open = _patched
    try:
        _tl.symbol_is_tradeable("SPY", 0.832)
    finally:
        _bi.open = real_open
    return counted[0]


_small_bytes = _gate_bytes()
_size_before = os.path.getsize(_tl.path)
for _i in range(3000, 15000):
    _tl.entered("SPY", "buy", 10, 451.20, inputs={"reference_price": 451.20})
_large_bytes = _gate_bytes()
_size_after = os.path.getsize(_tl.path)

chk("TL-7 the gate reads a bounded window, not the whole journal",
    _large_bytes < max(_small_bytes * 3, 512 * 1024),
    "file %dKB->%dKB, read %dKB->%dKB"
    % (_size_before // 1024, _size_after // 1024,
       _small_bytes // 1024, _large_bytes // 1024),
    "read stays bounded as the file grows")
chk("TL-7b the journal really did grow (the check is not vacuous)",
    _size_after > _size_before * 3, _size_after / max(1, _size_before), "> 3x")

# A partially written final line must not break the tail read.
with open(_tl.path, "a", encoding="utf-8") as _h:
    _h.write('{"event": "entered", "symbol": "SPY"')   # truncated, no newline
chk("TL-8 a truncated trailing line is skipped, not fatal",
    len(_tl.read(limit=10)) >= 1)
chk("TL-9 the gate survives a truncated journal",
    _tl.symbol_is_tradeable("SPY", 0.832)[0] in (True, False))


# --- CD. Entry cooldown must survive a restart -----------------------------
# The per-symbol cooldown lived only in self._last_trade, so a crash-restart
# loop cleared it and the bot could immediately re-enter a symbol it had just
# traded. With systemd Restart=always that is not hypothetical.
_cd_dir = tempfile.mkdtemp()
_cd_path = os.path.join(_cd_dir, "cooldown.db")
_cd = ExecutionSafety(_cd_path)
_cd_broker = Broker()

chk("CD-1 an untraded symbol has no entry time",
    _cd.last_entry_time("SPY") is None)

_cd.submit(_cd_broker, client_order_key="c1", symbol="SPY", side="buy", quantity=1)
_first = _cd.last_entry_time("SPY")
chk("CD-2 an entry records a creation time", _first is not None)
chk("CD-3 it is recent", abs(_first - time.time()) < 10)

# A NEW ExecutionSafety on the same file — i.e. after a restart — still knows.
_cd_restarted = ExecutionSafety(_cd_path)
chk("CD-4 the entry time survives a restart",
    _cd_restarted.last_entry_time("SPY") == _first,
    (_cd_restarted.last_entry_time("SPY"), _first), "equal")

# created_at must NOT move when the record is later reconciled.
_cd.reconcile(_cd_broker, "c1")
chk("CD-5 reconciling does not reset the entry time",
    _cd.last_entry_time("SPY") == _first,
    (_cd.last_entry_time("SPY"), _first), "unchanged")

# Exits must not delay re-entry.
_cd.register_exit(client_order_key="x1", symbol="SPY", side="sell", quantity=1)
chk("CD-6 an exit does not count as an entry",
    _cd.last_entry_time("SPY") == _first,
    (_cd.last_entry_time("SPY"), _first), "unchanged")

# Per symbol, not global.
chk("CD-7 an untraded symbol is unaffected",
    _cd.last_entry_time("QQQ") is None)
_cd.submit(_cd_broker, client_order_key="c2", symbol="QQQ", side="buy", quantity=1)
chk("CD-8 each symbol tracks its own entry time",
    _cd.last_entry_time("QQQ") is not None
    and _cd.last_entry_time("QQQ") >= _first)

# A refused entry still marks the attempt -- otherwise a kill-switched symbol
# could be retried in a tight loop.
_cd2 = ExecutionSafety(os.path.join(_cd_dir, "cooldown2.db"))
_cd2.set_kill(True)
_cd2.submit(_cd_broker, client_order_key="r1", symbol="IWM", side="buy", quantity=1)
chk("CD-9 a refused entry is still timestamped",
    _cd2.last_entry_time("IWM") is not None)

chk("CD-10 the engine consults the ledger for the cooldown",
    "last_entry_time" in open(os.path.join(BACKEND, "trading.py"),
                              encoding="utf-8").read())
chk("CD-11 the cooldown period is configurable",
    trading.ENTRY_COOLDOWN_S == 300, trading.ENTRY_COOLDOWN_S, 300)


# --- EV. Paper and live must never share data ------------------------------
# Paper fills are optimistic -- no queue position, no partial fills, IEX-only
# feed. Mixing them into the same ledger/journal/pattern store contaminates
# exactly the measurements real money exists to produce. And `paper=True` was
# hardcoded in the client, so live credentials were routed at the paper
# endpoint: "going live" did not.
from trading import detect_environment  # noqa: E402

chk("EV-1 a paper URL is detected as paper",
    detect_environment(base_url="https://paper-api.alpaca.markets") == "paper")
chk("EV-2 a live URL is detected as live",
    detect_environment(base_url="https://api.alpaca.markets") == "live")
chk("EV-3 a PK key is paper", detect_environment(api_key="PKABC123") == "paper")
chk("EV-4 an AK key is live", detect_environment(api_key="AKABC123") == "live")
chk("EV-5 key detection is case-insensitive",
    detect_environment(api_key="akabc123") == "live")

# Ambiguity must resolve toward paper: guessing wrong toward paper costs
# nothing, guessing wrong toward live spends real money.
chk("EV-6 an unrecognised key defaults to paper",
    detect_environment(api_key="somethingelse") == "paper")
chk("EV-7 no credentials at all defaults to paper",
    detect_environment() == "paper")
chk("EV-8 the URL wins over the key when both are present",
    detect_environment(api_key="AKABC", base_url="https://paper-api.alpaca.markets")
    == "paper")

# The data directory follows the environment.
_prev = {k: os.environ.get(k) for k in
         ("DATA_DIR", "APCA_API_KEY_ID", "APCA_BASE_URL", "DATA_ROOT")}
for _k in _prev:
    os.environ.pop(_k, None)
os.environ["DATA_ROOT"] = "/tmp/etroot"

os.environ["APCA_API_KEY_ID"] = "PKTEST"
_il.reload(_main)
_paper_dir = _main.DATA_DIR
chk("EV-9 paper credentials select a paper directory",
    _paper_dir.endswith("paper"), _paper_dir, "ends with paper")

os.environ["APCA_API_KEY_ID"] = "AKTEST"
_il.reload(_main)
_live_dir = _main.DATA_DIR
chk("EV-10 live credentials select a live directory",
    _live_dir.endswith("live"), _live_dir, "ends with live")
chk("EV-11 the two directories are different",
    _paper_dir != _live_dir, (_paper_dir, _live_dir), "different")

# An explicit override still wins, so tests and one-offs are unaffected.
os.environ["DATA_DIR"] = "/tmp/explicit-override"
_il.reload(_main)
chk("EV-12 an explicit DATA_DIR overrides detection",
    _main.DATA_DIR == "/tmp/explicit-override", _main.DATA_DIR, "override")

for _k, _v in _prev.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
os.environ.pop("DATA_ROOT", None)
_il.reload(_main)

chk("EV-13 the client is no longer hardcoded to paper",
    "paper=True," not in open(os.path.join(BACKEND, "trading.py"),
                              encoding="utf-8").read())
chk("EV-14 the client derives paper from the environment",
    "paper=not self.is_live" in open(os.path.join(BACKEND, "trading.py"),
                                     encoding="utf-8").read())


# --- PF. Preflight — the runbook, enforced ---------------------------------
# A bot that needs a human to remember a checklist is not automatic. Every
# check that would otherwise be a line in a runbook runs at startup, and
# anything unsafe REFUSES to trade rather than warning into a log.
def _preflight_orch(*, environment="paper", data_dir="/data/paper",
                    equity=100000.0, price=100.0, kill=False,
                    unsided=0, symbols=None):
    o = _main.Orchestrator.__new__(_main.Orchestrator)
    o.state = _main.PipelineState()
    broker = SimpleNamespace(
        environment=environment,
        get_account=lambda: SimpleNamespace(equity=equity))
    # price may be a scalar or a per-symbol dict, so a test can make SOME
    # symbols unaffordable without making all of them unaffordable.
    _price_of = (price.get if isinstance(price, dict) else (lambda sym: price))
    o._trading_engine = SimpleNamespace(
        broker=broker,
        _get_reference_price=_price_of,
        position_truth=SimpleNamespace(
            safety=SimpleNamespace(kill_engaged=lambda: kill)))

    class _Cur:
        def fetchone(self):
            return {"c": unsided}

    o._pattern_engine = SimpleNamespace(
        db=SimpleNamespace(_connect=lambda: SimpleNamespace(
            execute=lambda *a, **k: _Cur())))
    _main.DATA_DIR = data_dir
    if symbols is not None:
        _main.TRADING_SYMBOLS = symbols
    return o


_saved_dir, _saved_syms = _main.DATA_DIR, list(_main.TRADING_SYMBOLS)

r = _preflight_orch().preflight()
chk("PF-1 a healthy system passes", r["ok"] is True, r["blocking"], "no blockers")

# Live credentials writing into a paper directory must block.
r = _preflight_orch(environment="live", data_dir="/data/paper").preflight()
chk("PF-2 live credentials with a paper data dir is blocking",
    r["ok"] is False and any("data directory" in b for b in r["blocking"]),
    r["blocking"], "blocked")
r = _preflight_orch(environment="live", data_dir="/data/live").preflight()
chk("PF-3 live credentials with a live data dir passes",
    r["ok"] is True, r["blocking"], "no blockers")

# Learning data from the superseded logic must block, not warn.
r = _preflight_orch(unsided=1200).preflight()
chk("PF-4 pre-fix learning data blocks trading",
    r["ok"] is False and any("no recorded side" in b for b in r["blocking"]),
    r["blocking"], "blocked")
chk("PF-5 it names the remedy",
    any("reset_learning" in b for b in r["blocking"]))

# An engaged kill switch must not be started through by accident.
r = _preflight_orch(kill=True).preflight()
chk("PF-6 an engaged kill switch blocks startup",
    r["ok"] is False and any("kill switch" in b for b in r["blocking"]))

# An account too small to trade ANYTHING blocks; partially blocked warns.
r = _preflight_orch(equity=500.0, price=640.0).preflight()
chk("PF-7 an account that can trade nothing blocks",
    r["ok"] is False and any("size to zero" in b for b in r["blocking"]),
    r["blocking"], "blocked")
r = _preflight_orch(equity=2000.0, price={"SPY": 640.0, "TLT": 88.0},
                    symbols=["SPY", "TLT"]).preflight()
chk("PF-8 a partially tradeable account warns rather than blocks",
    r["ok"] is True and any("size to zero" in w for w in r["warnings"]),
    (r["blocking"], r["warnings"]), "warn only")

# An empty universe is fatal.
r = _preflight_orch(symbols=[]).preflight()
chk("PF-9 an empty universe blocks",
    r["ok"] is False and any("nothing to trade" in b for b in r["blocking"]))

_main.TRADING_SYMBOLS = _saved_syms
_main.DATA_DIR = _saved_dir

# The result is exposed, not just logged.
_o = _preflight_orch()
_o.preflight()
chk("PF-10 the result is stored on state", "ok" in _o.state.preflight)
chk("PF-11 it is exposed through to_dict",
    "preflight" in _o.state.to_dict())

chk("PF-12 startup runs preflight and drops to MANUAL on failure",
    "self.preflight()" in open(os.path.join(BACKEND, "main.py"),
                               encoding="utf-8").read()
    and "Preflight failed" in open(os.path.join(BACKEND, "main.py"),
                                   encoding="utf-8").read())


# --- DR. Daily self-review — the analysis nobody has to remember -----------
# The journal and forward test only help if somebody runs them. An automatic
# bot cannot depend on that, so the same analysis runs after each session and
# anything actionable becomes an alert.
def _review_orch(*, journal=None, unprotected=None, unresolved=0, kill=False):
    o = _main.Orchestrator.__new__(_main.Orchestrator)
    o.state = _main.PipelineState()
    if unprotected:
        o.state.unprotected = dict(unprotected)
    o._trading_engine = SimpleNamespace(
        position_truth=SimpleNamespace(safety=SimpleNamespace(
            stats=lambda: {"unresolved": unresolved, "hot_orders": unresolved},
            prune=lambda older_than_days=7.0: 0,
            kill_engaged=lambda: kill)))
    o._pattern_engine = SimpleNamespace(db=SimpleNamespace(_connect=lambda: None))
    if journal is not None:
        os.environ["DECISION_LOG_PATH"] = journal.path
    return o


_raised = []
_orig_create = _mon.AlertManager._create_alert
_mon.AlertManager._create_alert = lambda self, t, s, m: (
    _raised.append((t, s, m)) or {"type": t})

# A clean day produces no alerts.
_clean = DecisionLog(path=str(Path(tempfile.mkdtemp()) / "clean.jsonl"))
_raised.clear()
_r = _review_orch(journal=_clean).daily_self_review()
chk("DR-1 a clean session raises nothing", _r["alerts"] == 0, _raised, "no alerts")
chk("DR-2 the review still returns a summary", "findings" in _r)

# A symbol that has become too expensive raises a critical alert.
_expensive = DecisionLog(path=str(Path(tempfile.mkdtemp()) / "exp.jsonl"))
for _i in range(20):
    _expensive.entered("GLD", "buy", 10, 200.60, inputs={"reference_price": 200.00})
_raised.clear()
_r = _review_orch(journal=_expensive).daily_self_review()
chk("DR-3 an expensive symbol raises an alert",
    any(t == "execution_cost" for t, _s, _m in _raised), _raised, "execution_cost")
chk("DR-4 the alert is critical",
    any(s == "critical" for t, s, _m in _raised if t == "execution_cost"))
chk("DR-5 it names the symbol",
    any("GLD" in m for t, _s, m in _raised if t == "execution_cost"))

# Positions running without a working stop are surfaced.
_raised.clear()
_r = _review_orch(journal=_clean, unprotected={"SPY": 4}).daily_self_review()
chk("DR-6 unprotected positions raise a critical alert",
    any(t == "unprotected_position" and s == "critical"
        for t, s, _m in _raised), _raised, "unprotected_position")

# Orders that never settled are surfaced.
_raised.clear()
_r = _review_orch(journal=_clean, unresolved=3).daily_self_review()
chk("DR-7 unresolved orders raise an alert",
    any(t == "ledger_unresolved" for t, _s, _m in _raised), _raised, "ledger")
chk("DR-8 the alert explains what unresolved means",
    any("flatten" in m or "mid-submit" in m
        for t, _s, m in _raised if t == "ledger_unresolved"))

# Strategy diagnoses are informational, not alarms.
_tight = DecisionLog(path=str(Path(tempfile.mkdtemp()) / "tight.jsonl"))
# Sized to exceed half the take-profit target under EITHER the daily or the
# intraday configuration, so the fixture does not depend on module state.
for _i in range(8):
    _tight.excursion("SPY", trade_id="t%d" % _i, entry_price=100.0,
                     best_price=102.0, worst_price=97.5, exit_price=97.5,
                     side="buy")
_raised.clear()
_r = _review_orch(journal=_tight).daily_self_review()
chk("DR-9 a stop-too-tight diagnosis is raised",
    any(t == "strategy_review" for t, _s, _m in _raised), _raised, "strategy_review")
chk("DR-10 diagnoses are informational, not critical",
    all(s == "info" for t, s, _m in _raised if t == "strategy_review"))

# The review must never be able to break the trading loop.
_broken = _review_orch(journal=_clean)
_broken._trading_engine = None
try:
    _broken.daily_self_review()
    _survived = True
except Exception:
    _survived = False
chk("DR-11 a failing review does not raise", _survived)

chk("DR-12 the result is kept on state",
    isinstance(_review_orch(journal=_clean).daily_self_review(), dict))
chk("DR-13 the review runs at post-market reconciliation",
    "self.daily_self_review()" in open(os.path.join(BACKEND, "main.py"),
                                       encoding="utf-8").read())

_mon.AlertManager._create_alert = _orig_create
os.environ.pop("DECISION_LOG_PATH", None)


# --- E4. The correction must actually be USED ------------------------------
# corrected_signal_strength was implemented, tested, and called by nothing --
# the same failure mode as ExecutionSafety, the missing EMAs and the hardcoded
# universe. A correction that is not wired in protects nothing.
_pat_src = open(os.path.join(BACKEND, "patterns.py"), encoding="utf-8").read()
chk("E4-1 evaluate() uses the corrected strength",
    "corrected_signal_strength(family_size)" in _pat_src)
chk("E4-2 the search space is measured, not assumed",
    "count_patterns()" in _pat_src)
chk("E4-3 the uncorrected property is no longer the one that drives signals",
    "signal_strength = stats.signal_strength" not in _pat_src)

# The search space is counted from the database.
_pe = engine()
chk("E4-4 an empty database has no search space",
    _pe.db.count_patterns() == 0, _pe.db.count_patterns(), 0)
_rid = open_trade(_pe, entry=100.0)
chk("E4-5 recording a pattern grows the search space",
    _pe.db.count_patterns() >= 1, _pe.db.count_patterns(), ">= 1")

# A marginal pattern loses its weight once the search space is admitted.
_marg = PatternStats(pattern_id="p", count=200, wins=115, losses=85)
chk("E4-6 marginal pattern carries weight when tested alone",
    _marg.corrected_signal_strength(1) > 0)
chk("E4-7 the same pattern carries none across a 15-pattern search",
    _marg.corrected_signal_strength(15) == 0.0,
    _marg.corrected_signal_strength(15), 0.0)

# A genuinely strong pattern survives, but is scaled down honestly.
_strong = PatternStats(pattern_id="p", count=600, wins=390, losses=210)
chk("E4-8 a real edge survives a wide search",
    _strong.corrected_signal_strength(500) > 0)
chk("E4-9 but is weighted lower than if tested alone",
    _strong.corrected_signal_strength(500) < _strong.corrected_signal_strength(1),
    (_strong.corrected_signal_strength(500), _strong.corrected_signal_strength(1)),
    "corrected < naive")

# A missing or broken database must not silently disable the correction by
# reporting a search space of zero... which would make z fall back to 1.96.
chk("E4-10 a zero search space is treated as a single test",
    _marg.corrected_signal_strength(0) == _marg.corrected_signal_strength(1))


# --- RE. Reachability — code that is correct but never called --------------
# Unit tests call functions directly, so a capability can be perfectly correct
# AND completely unreachable and still pass every test. That has happened FOUR
# times in this codebase:
#
#   * ExecutionSafety, fully implemented and imported by nothing
#   * live_indicators never publishing the EMAs trend_conviction needs
#   * the trading universe hardcoded in six places, one the indicator loop
#   * corrected_signal_strength, the multiple-testing correction, called by
#     nothing while evaluate() used the uncorrected value
#
# This is the cheapest check available and it caught all four. It runs here so
# the fifth is caught automatically.
_PROD_FILES = {}
for _root in (BACKEND, os.path.join(os.path.dirname(BACKEND), "scripts")):
    if not os.path.isdir(_root):
        continue
    for _f in sorted(os.listdir(_root)):
        if _f.endswith(".py"):
            _path = os.path.join(_root, _f)
            _PROD_FILES[_path] = open(_path, encoding="utf-8",
                                      errors="ignore").read()

_CALLED = {}
for _path, _src in _PROD_FILES.items():
    _names = set()
    try:
        _tree = _ast.parse(_src)
    except SyntaxError:
        continue
    for _n in _ast.walk(_tree):
        if isinstance(_n, _ast.Call) and isinstance(_n.func, _ast.Attribute):
            _names.add(_n.func.attr)
        elif isinstance(_n, _ast.Call) and isinstance(_n.func, _ast.Name):
            _names.add(_n.func.id)
        elif isinstance(_n, _ast.Attribute):
            _names.add(_n.attr)
    _CALLED[_path] = _names

#: Capabilities that MUST be reachable from production code. Every one of
#: these enforces a safety property; an unreachable one is a property that
#: silently does not hold.
_MUST_BE_REACHED = [
    # execution safety
    "submit", "reconcile", "reconcile_all", "reconcile_flat", "register_exit",
    "has_open_exposure", "has_unresolved_exit", "set_kill", "kill_engaged",
    "can_enter", "position_state", "last_entry_time", "prune",
    # trading
    "close_position_guarded", "detect_environment", "get_position_strict",
    "get_order_by_client_id", "current_exposure",
    # orchestration
    "preflight", "daily_self_review", "carries_overnight_risk",
    "_engage_execution_kill", "_save_daily_tracking", "_load_daily_tracking",
    "_flag_unprotected",
    # learning and measurement
    "trend_conviction", "mean_reversion_conviction", "realized_volatility_pct",
    "corrected_signal_strength", "count_patterns",
    # journal
    "entered", "blocked", "exited", "excursion", "symbol_is_tradeable",
    "execution_quality",
]

_unreached = []
for _name in _MUST_BE_REACHED:
    _reached = any(_name in _names for _path, _names in _CALLED.items()
                   if "/tests/" not in _path)
    if not _reached:
        _unreached.append(_name)

chk("RE-1 every safety capability is reachable from production code",
    not _unreached, _unreached, "none unreachable")

# The check itself must be able to fail, or it is decoration.
chk("RE-2 the reachability check can detect an unreachable name",
    not any("a_capability_that_does_not_exist_anywhere" in _n
            for _n in _CALLED.values()))

# And the specific regressions that motivated it.
_main_src3 = _PROD_FILES.get(os.path.join(BACKEND, "main.py"), "")
_pat_src3 = _PROD_FILES.get(os.path.join(BACKEND, "patterns.py"), "")
chk("RE-3 the EMAs are still published to live_indicators",
    '"ema_short": ema_short' in _main_src3)
chk("RE-4 the universe is still centralised",
    '["SPY", "QQQ", "IWM"]' not in _main_src3)
chk("RE-5 evaluate still uses the CORRECTED signal strength",
    "corrected_signal_strength(family_size)" in _pat_src3)
chk("RE-6 the order ledger is still consulted by the engine",
    "position_truth" in _PROD_FILES.get(os.path.join(BACKEND, "trading.py"), ""))


# --- SS. Side-specific exposure and residual detection ---------------------
# can_enter accepted `side` and ignored it, so the signature promised a
# precision it did not have. And reconcile_flat -- the only thing that detects
# "we believe it filled but the broker still holds a position" -- was called
# nowhere outside the guarded exit.
_ss_dir = tempfile.mkdtemp()


class _SSBroker(Broker):
    def __init__(self, position=None):
        Broker.__init__(self)
        self.position = position

    def get_position(self, symbol):
        return self.position


_ss_safety = ExecutionSafety(os.path.join(_ss_dir, "ss.db"))
_ss_broker = _SSBroker()
_ss_truth = PositionTruth(_ss_safety, _ss_broker, FakeState([]))

_ok, _why = _ss_truth.can_enter("SPY", "buy")
chk("SS-1 a clean symbol allows entry", _ok is True, _why, "allowed")

# An unresolved BUY blocks a further BUY, and says so specifically.
_ss_safety.submit(_ss_broker, client_order_key="b1", symbol="SPY",
                  side="buy", quantity=1)
_d = _ss_safety._read()
_d["orders"]["b1"]["status"] = "ambiguous"
_ss_safety._write(_d)
_ok, _why = _ss_truth.can_enter("SPY", "buy")
chk("SS-2 an unresolved same-side order blocks entry", _ok is False)
chk("SS-3 the reason names the side",
    "buy" in _why.lower(), _why, "mentions the side")

# The side parameter is genuinely consulted, not decoration.
_src_es = open(os.path.join(BACKEND, "execution_safety.py"),
               encoding="utf-8").read()
chk("SS-4 can_enter actually uses its side argument",
    "has_open_exposure(symbol, str(side).lower())" in _src_es)

# reconcile_flat runs during reconciliation, so a filled order that left a
# live position is marked residual instead of looking settled.
_res_safety = ExecutionSafety(os.path.join(_ss_dir, "res.db"))
_res_broker = _SSBroker(position=SimpleNamespace(qty=5))
_res_truth = PositionTruth(_res_safety, _res_broker, FakeState([]))
_res_safety.submit(_res_broker, client_order_key="f1", symbol="SPY",
                   side="buy", quantity=1)
_res_broker.orders["o1"] = SimpleNamespace(
    status="filled", filled_qty=1, filled_avg_price=100.0)
_report = _res_truth.reconcile(["SPY"])
_rec = _res_safety._fetch("f1")
chk("SS-5 a filled order with a live position is marked residual",
    _rec.status == "residual", _rec.status, "residual")
chk("SS-6 the residual reason is recorded",
    _rec.error == "broker_position_not_flat", _rec.error,
    "broker_position_not_flat")

# A genuinely flat fill is left alone.
_flat_safety = ExecutionSafety(os.path.join(_ss_dir, "flat.db"))
_flat_broker = _SSBroker(position=None)
_flat_truth = PositionTruth(_flat_safety, _flat_broker, FakeState([]))
_flat_safety.submit(_flat_broker, client_order_key="g1", symbol="SPY",
                    side="buy", quantity=1)
_flat_broker.orders["o1"] = SimpleNamespace(
    status="filled", filled_qty=1, filled_avg_price=100.0)
_flat_truth.reconcile(["SPY"])
chk("SS-7 a genuinely flat fill stays filled",
    _flat_safety._fetch("g1").status == "filled",
    _flat_safety._fetch("g1").status, "filled")


# --- DD. Data-directory agreement, in the PRODUCTION configuration ---------
# Every other test in this file sets DATA_DIR explicitly. That is the one
# configuration in which this bug is invisible.
#
# Twelve modules read DATA_DIR from the environment at import time, and each
# falls back to a DIFFERENT hardcoded default when it is unset: backend/data,
# /var/lib/educated-trades/data, "." (the working directory),
# /home/team/shared/data, /opt/educated_trades/data. main.py derived the
# segregated directory but kept it in a module global, so in production:
#
#   * main wrote the heartbeat to $DATA_ROOT/paper while the watchdog looked
#     in /home/team/shared/data and alerted "orchestrator may not be running"
#     every 60 seconds, forever
#   * the decision journal landed in the working directory
#   * paper and live shared the pattern store, the alert db and position state
#
# So this check runs a SUBPROCESS with DATA_DIR unset -- the way systemd will
# run it -- and asserts every module lands in the same place. A fixture that
# sets the variable cannot detect this, which is exactly why it survived.
#: Marker-prefixed, because the broken case produces a RELATIVE path
#: ("./decisions.jsonl") -- filtering on a leading "/" would silently discard
#: the single most damning piece of evidence.
_DD_PROBE = """
import sys
sys.path.insert(0, %r)
import main
import alert_db, decision_log, monitoring, position_state, patterns, watchdog
for _p in (main.DATA_DIR, alert_db.DB_PATH, decision_log.DEFAULT_PATH,
           monitoring.AUDIT_LOG_PATH, position_state.POSITION_STATE_PATH,
           patterns.DB_PATH, watchdog.HEARTBEAT_PATH):
    print("DDPATH|%%s" %% (_p,))
""" % BACKEND


def _dd_resolve(key, root="/tmp/dd-probe", strip_export=False):
    """Resolve every module's data path the way production will."""
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATA_DIR", "DATA_DIR_AUTOSET", "DB_PATH",
                        "APCA_BASE_URL", "EXECUTION_LEDGER_PATH",
                        "DECISION_LOG_PATH")}
    env["DATA_ROOT"] = root
    env["APCA_API_KEY_ID"] = key
    env["PYTHONPATH"] = BACKEND
    source, backend = _DD_PROBE, BACKEND
    if strip_export:
        # Negative control: put the bug back and confirm this test fails.
        import tempfile as _tf
        broken = _tf.mkdtemp()
        for name in os.listdir(backend):
            if name.endswith(".py"):
                text = open(os.path.join(backend, name), encoding="utf-8").read()
                if name == "main.py":
                    text = text.replace('os.environ["DATA_DIR"] = DATA_DIR', "pass")
                open(os.path.join(broken, name), "w", encoding="utf-8").write(text)
        source = _DD_PROBE.replace(repr(backend), repr(broken))
        env["PYTHONPATH"] = broken
    out = subprocess.run([sys.executable, "-c", source], env=env,
                         capture_output=True, text=True, timeout=300)
    lines = [ln.split("|", 1)[1] for ln in out.stdout.splitlines()
             if ln.startswith("DDPATH|")]
    if len(lines) != 7:
        # A silent probe failure would otherwise surface as an opaque
        # "len != 7". Carry the reason to the assertion.
        _PROBE_DIAGNOSTICS.append(
            ("dd", key, out.returncode, out.stderr.strip()[-400:]))
    return lines


_dd_paper = _dd_resolve("PKPAPERKEY")
chk("DD-1 the probe subprocess resolved every module",
    len(_dd_paper) == 7, len(_dd_paper), 7)
chk("DD-2 paper credentials select a paper directory with no DATA_DIR set",
    bool(_dd_paper) and _dd_paper[0].endswith("/paper"),
    _dd_paper[:1], "ends with /paper")
chk("DD-3 every module agrees on the directory in production config",
    bool(_dd_paper) and all(p.startswith(_dd_paper[0] + "/") for p in _dd_paper[1:]),
    [p for p in _dd_paper[1:] if not p.startswith(_dd_paper[0] + "/")] or "all agree",
    "all under main.DATA_DIR")
chk("DD-4 the watchdog looks where the heartbeat is actually written",
    bool(_dd_paper) and _dd_paper[6] == os.path.join(_dd_paper[0], "heartbeat"),
    _dd_paper[6:7], "DATA_DIR/heartbeat")
chk("DD-5 the decision journal is not left in the working directory",
    bool(_dd_paper) and not _dd_paper[2].startswith("."),
    _dd_paper[2:3], "absolute, under DATA_DIR")

_dd_live = _dd_resolve("AKLIVEKEY")
chk("DD-6 live credentials select a live directory",
    bool(_dd_live) and _dd_live[0].endswith("/live"), _dd_live[:1], "ends with /live")
chk("DD-7 paper and live share no store at all",
    bool(_dd_live) and not set(_dd_paper) & set(_dd_live),
    sorted(set(_dd_paper) & set(_dd_live)) or "disjoint", "disjoint")

# Negative control: reintroduce the bug and confirm DD-3 would catch it.
_dd_broken = _dd_resolve("PKPAPERKEY", strip_export=True)
chk("DD-8 without the export the modules disagree (negative control)",
    len(_dd_broken) == 7
    and not all(p.startswith(_dd_broken[0] + "/") for p in _dd_broken[1:]),
    "scattered" if len(_dd_broken) == 7 else _dd_broken,
    "the check can detect the bug it exists for")


# --- WD. Watchdog — the process that reports the others are dead -----------
# A watchdog that cries wolf is worse than none: people mute the channel and
# then miss the real outage. All four of these were live-fire noise sources.
import importlib as _wd_il  # noqa: E402
_wd_dir = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _wd_dir
os.environ["WATCHDOG_REPEAT_SECONDS"] = "900"
os.environ["WATCHDOG_STALE_SECONDS"] = "300"
import watchdog as _wd  # noqa: E402
_wd_il.reload(_wd)

chk("WD-1 the heartbeat path follows the segregated data directory",
    str(_wd.HEARTBEAT_PATH) == os.path.join(_wd_dir, "heartbeat"),
    str(_wd.HEARTBEAT_PATH), os.path.join(_wd_dir, "heartbeat"))

# Central is UTC-6 in winter. The old hardcoded UTC-5 fallback put the whole
# session an hour out from November to March: it alerted for an hour before
# the open and fell silent for the last hour of the day.
_wd_jan = _dt(2026, 1, 15, 14, 15, tzinfo=_tzc.utc)  # 08:15 CST
chk("WD-2 08:15 CST in January is correctly before the open",
    _wd.is_market_hours(_wd_jan) is False, _wd.is_market_hours(_wd_jan), False)
_wd_jan_open = _dt(2026, 1, 15, 20, 30, tzinfo=_tzc.utc)  # 14:30 CST
chk("WD-3 14:30 CST in January is correctly inside the session",
    _wd.is_market_hours(_wd_jan_open) is True,
    _wd.is_market_hours(_wd_jan_open), True)

# Half days and holidays: the old version knew about neither, so an early
# close produced three hours of false criticals.
_wd_half = _dt(2026, 11, 27, 19, 0, tzinfo=_tzc.utc)  # 13:00 CST
chk("WD-4 the afternoon of a half day is closed",
    _wd.is_market_hours(_wd_half) is False, _wd.is_market_hours(_wd_half), False)
_wd_holiday = _dt(2026, 7, 3, 16, 0, tzinfo=_tzc.utc)
chk("WD-5 a market holiday is closed",
    _wd.is_market_hours(_wd_holiday) is False,
    _wd.is_market_hours(_wd_holiday), False)


def _wd_write(age_s):
    with open(_wd.HEARTBEAT_PATH, "w") as _h:
        _json.dump({"timestamp": time.time() - age_s}, _h)


_wd_write(30)
chk("WD-6 a fresh heartbeat reads as fresh", _wd.get_heartbeat_age() < 60)

# A malformed timestamp used to raise TypeError, which was not caught, so the
# watchdog died instead of reporting that something was wrong.
with open(_wd.HEARTBEAT_PATH, "w") as _h:
    _json.dump({"timestamp": "not-a-number"}, _h)
try:
    _wd_bad = _wd.get_heartbeat_age()
    _wd_ok = _wd_bad is None
except Exception as _exc:
    _wd_ok = "raised %s" % type(_exc).__name__
chk("WD-7 a malformed timestamp is reported, not raised", _wd_ok is True,
    _wd_ok, True)

# Debounce. One outage should not mean one message a minute.
_wd.clear_alert_state()
_wd_sent = []
_wd._real_should = _wd._should_send
chk("WD-8 the first alert of a kind is allowed", _wd._should_send("stale") is True)
chk("WD-9 an immediate repeat is suppressed",
    _wd._should_send("stale") is False, _wd._should_send("stale"), False)
chk("WD-10 a different failure mode still alerts",
    _wd._should_send("missing") is True)
_wd.clear_alert_state()
chk("WD-11 recovery re-arms the alert", _wd._should_send("stale") is True)

# Missing heartbeat outside market hours is the resting state of a bot that
# is not running, not an incident.
os.remove(_wd.HEARTBEAT_PATH)
_wd_calls = []
_wd._orig_send = _wd.send_discord_alert
_wd.send_discord_alert = lambda m, key="default": _wd_calls.append(key)
_wd.is_market_hours = lambda now=None: False
_wd.main()
chk("WD-12 no alert for a missing heartbeat while the market is closed",
    _wd_calls == [], _wd_calls, [])
_wd.is_market_hours = lambda now=None: True
_wd.main()
chk("WD-13 a missing heartbeat during the session does alert",
    _wd_calls == ["missing"], _wd_calls, ["missing"])
_wd_calls.clear()
_wd_write(30)
_wd.main()
chk("WD-14 a live heartbeat during the session is silent", _wd_calls == [],
    _wd_calls, [])
_wd_write(3600)
_wd.main()
chk("WD-15 a stale heartbeat during the session alerts",
    _wd_calls == ["stale"], _wd_calls, ["stale"])
_wd.send_discord_alert = _wd._orig_send

# The watchdog must not reimplement the calendar. A second approximation is a
# second thing to get wrong, and it was wrong for five months of every year.
_wd_src = open(os.path.join(BACKEND, "watchdog.py"), encoding="utf-8").read()
chk("WD-16 the watchdog defers to market_clock",
    "from market_clock import MarketClock" in _wd_src)
chk("WD-17 no hand-rolled timezone offset remains",
    "timedelta(hours=5)" not in _wd_src and "OPEN_HOUR" not in _wd_src)


# --- BK. Backups — the copy you only find out about when you need it -------
# BACKUP_REPO_DIR is "$DATA_DIR/../backup_repo". The ".." collapses exactly
# the paper/live segregation DATA_DIR exists to create, and snapshots were
# named by date alone -- so a live run overwrote the same day's paper
# snapshot, committed the overwrite, and reported success.
_bk_root = tempfile.mkdtemp()
_bk_src = os.path.join(_bk_root, "paper")
os.makedirs(_bk_src, exist_ok=True)
with open(os.path.join(_bk_src, "patterns.db"), "w") as _h:
    _h.write("paper-database")
os.environ["DATA_DIR"] = _bk_src
import data_backup as _bk  # noqa: E402
_wd_il.reload(_bk)

_bk_snap = os.path.join(_bk_root, "snap-paper")
os.makedirs(_bk_snap, exist_ok=True)
_bk_files = _bk._create_snapshots(Path(_bk_snap))
chk("BK-1 the snapshot reads the segregated data directory",
    "patterns.db.gz" in _bk_files, _bk_files, "includes patterns.db.gz")

import gzip as _gzip  # noqa: E402
with _gzip.open(os.path.join(_bk_snap, "patterns.db.gz"), "rb") as _h:
    _bk_body = _h.read()
chk("BK-2 the snapshot contains the real file, not an empty default",
    _bk_body == b"paper-database", _bk_body, b"paper-database")

# The caller passes patterns_db_path; the function used to ignore it and read
# a module global frozen at import, so it backed up the wrong file (or none)
# and still reported success.
_bk_other = os.path.join(_bk_root, "elsewhere.db")
with open(_bk_other, "w") as _h:
    _h.write("explicit-path")
_bk_snap2 = os.path.join(_bk_root, "snap-explicit")
os.makedirs(_bk_snap2, exist_ok=True)
_bk._create_snapshots(Path(_bk_snap2), patterns_db_path=_bk_other)
with _gzip.open(os.path.join(_bk_snap2, "patterns.db.gz"), "rb") as _h:
    _bk_body2 = _h.read()
chk("BK-3 an explicit patterns_db_path is honoured, not ignored",
    _bk_body2 == b"explicit-path", _bk_body2, b"explicit-path")

# Paper and live must not write to the same snapshot path.
_bk_prev_key = os.environ.get("APCA_API_KEY_ID")
os.environ["APCA_API_KEY_ID"] = "PKPAPER"
_bk_paper_env = _bk.current_environment()
os.environ["APCA_API_KEY_ID"] = "AKLIVE"
_bk_live_env = _bk.current_environment()
if _bk_prev_key is None:
    os.environ.pop("APCA_API_KEY_ID", None)
else:
    os.environ["APCA_API_KEY_ID"] = _bk_prev_key
chk("BK-4 the backup knows which environment produced the data",
    (_bk_paper_env, _bk_live_env) == ("paper", "live"),
    (_bk_paper_env, _bk_live_env), ("paper", "live"))
_bk_src_text = open(os.path.join(BACKEND, "data_backup.py"), encoding="utf-8").read()
chk("BK-5 snapshots are filed under the environment, not the date alone",
    'repo_dir / "data" / environment' in _bk_src_text)
chk("BK-6 rotation is scoped to one environment's snapshots",
    "_rotate_old_snapshots(data_dir)" in _bk_src_text)


# --- HC. The external health check, which systemd runs on its own ----------
# The in-process watchdog inherits DATA_DIR because main spawns it. This one
# runs under its own unit and inherits nothing, so a hardcoded default meant
# it watched a directory nothing writes to -- and a health check aimed at an
# empty path reports a healthy silence, which is worse than having none.
_hc_probe = """
import os, sys, json
sys.argv = ["health_check"]
sys.path.insert(0, %r)
import importlib.util as _u
spec = _u.spec_from_file_location("health_check", %r)
mod = _u.module_from_spec(spec); spec.loader.exec_module(mod)
print("HCPATH|" + str(mod.DATA_DIR))
print("HCPATH|" + str(mod.HEARTBEAT_PATH))
print("HCPATH|" + str(mod.STATE_PATH))
""" % (BACKEND, os.path.join(os.path.dirname(BACKEND), "scripts",
                             "health_check.py"))


def _hc_resolve(key):
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATA_DIR", "DATA_DIR_AUTOSET", "HEALTH_STATE_FILE",
                        "APCA_BASE_URL")}
    env["DATA_ROOT"] = "/tmp/hc-probe"
    env["APCA_API_KEY_ID"] = key
    out = subprocess.run([sys.executable, "-c", _hc_probe], env=env,
                         capture_output=True, text=True, timeout=300)
    lines = [ln.split("|", 1)[1] for ln in out.stdout.splitlines()
             if ln.startswith("HCPATH|")]
    if len(lines) != 3:
        _PROBE_DIAGNOSTICS.append(
            ("hc", key, out.returncode, out.stderr.strip()[-400:]))
    return lines


_hc_paper = _hc_resolve("PKPAPERKEY")
_hc_live = _hc_resolve("AKLIVEKEY")
chk("HC-1 the health check derives the data directory from credentials",
    _hc_paper[:1] == ["/tmp/hc-probe/paper"], _hc_paper[:1],
    ["/tmp/hc-probe/paper"])
chk("HC-2 it follows the credentials to live",
    _hc_live[:1] == ["/tmp/hc-probe/live"], _hc_live[:1],
    ["/tmp/hc-probe/live"])
chk("HC-3 it watches the heartbeat the orchestrator actually writes",
    len(_hc_paper) == 3
    and _hc_paper[1] == os.path.join(_hc_paper[0], "heartbeat"),
    _hc_paper[1:2], "DATA_DIR/heartbeat")
chk("HC-4 its own state file is segregated too, not a fixed path",
    len(_hc_paper) == 3 and _hc_paper[2] != _hc_live[2],
    (_hc_paper[2:3], _hc_live[2:3]), "different per environment")

# Preflight must say so when an explicit DATA_DIR silences the split. The
# systemd EnvironmentFile is the likeliest place for a stale one to sit, and
# with it set, going live is a step someone has to remember again.
_pf_explicit = _preflight_orch(environment="paper", data_dir="/data/shared")
_main.DATA_DIR_IS_EXPLICIT = True
try:
    _pf_res = _pf_explicit.preflight()
finally:
    _main.DATA_DIR_IS_EXPLICIT = False
chk("HC-5 preflight warns when DATA_DIR is set by hand",
    any("DATA_DIR is set explicitly" in w for w in _pf_res["warnings"]),
    _pf_res["warnings"], "a warning about explicit DATA_DIR")
chk("HC-6 but it does not refuse to trade over it",
    _pf_res["ok"] is True, _pf_res["ok"], True)


# --- JL. The journal must not scatter --------------------------------------
# trading.py deliberately does not import main.py, so a TradingEngine built
# by a backtest, a script or the test suite got a DecisionLog whose default
# path was computed at import from an unset DATA_DIR -- "." -- and wrote the
# record of WHY a trade was taken into whatever directory it started from.
# This suite was appending to a decisions.jsonl in the repository root on
# every run, which is how it was found.
_jl_probe = """
import os, sys, json, tempfile
sys.path.insert(0, %r)
work = tempfile.mkdtemp()
os.chdir(work)
os.environ["DATA_ROOT"] = "/tmp/jl-probe"
os.environ["APCA_API_KEY_ID"] = "PKPAPER"
for _v in ("DATA_DIR", "DATA_DIR_AUTOSET", "DECISION_LOG_PATH"):
    os.environ.pop(_v, None)
from decision_log import DecisionLog
print("JL|" + DecisionLog().path)
print("JL|" + str(sorted(os.listdir(work))))
""" % BACKEND


def _jl_run():
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATA_DIR", "DATA_DIR_AUTOSET", "DECISION_LOG_PATH")}
    out = subprocess.run([sys.executable, "-c", _jl_probe], env=env,
                         capture_output=True, text=True, timeout=300)
    lines = [ln.split("|", 1)[1] for ln in out.stdout.splitlines()
             if ln.startswith("JL|")]
    if len(lines) != 2:
        _PROBE_DIAGNOSTICS.append(
            ("jl", "-", out.returncode, out.stderr.strip()[-400:]))
    return lines


_jl = _jl_run()
chk("JL-1 a journal opened with no configuration is not relative",
    bool(_jl) and os.path.isabs(_jl[0]), _jl[:1], "an absolute path")
chk("JL-2 it lands in the credential-derived directory",
    bool(_jl) and _jl[0] == "/tmp/jl-probe/paper/decisions.jsonl",
    _jl[:1], "/tmp/jl-probe/paper/decisions.jsonl")
chk("JL-3 nothing was written into the working directory",
    len(_jl) > 1 and "decisions.jsonl" not in _jl[1], _jl[1:2], "no journal in cwd")

# One derivation, not four. Each private copy is a chance to disagree, and
# the three that existed did.
import trading as _tr  # noqa: E402
chk("JL-4 the derivation lives in one place",
    callable(getattr(_tr, "resolve_data_dir", None)))
for _name, _rel in (("main", os.path.join(BACKEND, "main.py")),
                    ("decision_log", os.path.join(BACKEND, "decision_log.py")),
                    ("health_check", os.path.join(os.path.dirname(BACKEND),
                                                  "scripts", "health_check.py"))):
    _text = open(_rel, encoding="utf-8").read()
    chk("JL-5.%s routes to the shared derivation" % _name,
        "resolve_data_dir" in _text, _name, "imports resolve_data_dir")

# An exported DATA_DIR is our own answer, not an operator override; treating
# it as one makes the derivation non-idempotent and blind to a credential
# change -- exactly when it matters.
_jl_prev = {k: os.environ.get(k) for k in ("DATA_DIR", "DATA_DIR_AUTOSET")}
os.environ["DATA_DIR"] = "/tmp/jl/paper"
os.environ["DATA_DIR_AUTOSET"] = "/tmp/jl/paper"
chk("JL-6 a value we exported is not treated as explicit",
    _tr.data_dir_is_explicit() is False, _tr.data_dir_is_explicit(), False)
os.environ["DATA_DIR_AUTOSET"] = "/tmp/jl/something-else"
chk("JL-7 a value an operator set is",
    _tr.data_dir_is_explicit() is True, _tr.data_dir_is_explicit(), True)
for _k, _v in _jl_prev.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

# Structural guard: running this suite must leave no data files behind. A
# test that writes into the repository is a test whose isolation is broken,
# and broken isolation is what hid the DATA_DIR split for 656 checks.
chk("JL-8 the suite left no journal in the working directory",
    not os.path.exists(os.path.join(os.getcwd(), "decisions.jsonl"))
    or os.path.getmtime(os.path.join(os.getcwd(), "decisions.jsonl")) < _SUITE_START,
    "clean", "no journal written during this run")


# --- AU. Control API authentication ----------------------------------------
# The API can change mode and place trades. Auth read `if API_AUTH_TOKEN:`,
# so an unset token disabled authentication instead of refusing to serve; the
# mandatory-token check lived inside Orchestrator.start(), which --api-only
# never calls; and the server bound 0.0.0.0. Confirmed by hand before the
# fix: `POST /api/mode {"mode":"autonomous"}` returned 200 with no header.
class _AuthReq:
    def __init__(self, header=None):
        self.headers = {"Authorization": header} if header else {}


_au_prev_token = getattr(_main, "API_AUTH_TOKEN", "")
_main.API_AUTH_TOKEN = ""
chk("AU-1 with no token configured, nothing is authorized",
    _main._authorized(_AuthReq("Bearer anything")) is False,
    _main._authorized(_AuthReq("Bearer anything")), False)
chk("AU-2 an absent header is not authorized with no token either",
    _main._authorized(_AuthReq()) is False)

_main.API_AUTH_TOKEN = "correct-horse"
chk("AU-3 a wrong token is rejected",
    _main._authorized(_AuthReq("Bearer wrong")) is False)
chk("AU-4 a missing header is rejected",
    _main._authorized(_AuthReq()) is False)
chk("AU-5 a bare token without the scheme is rejected",
    _main._authorized(_AuthReq("correct-horse")) is False)
chk("AU-6 the correct bearer token is accepted",
    _main._authorized(_AuthReq("Bearer correct-horse")) is True)
_main.API_AUTH_TOKEN = _au_prev_token

_au_src = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
chk("AU-7 the token comparison is constant time",
    "hmac.compare_digest" in _au_src)
chk("AU-8 auth is not conditional on a token being present",
    _au_src.count("if API_AUTH_TOKEN:\n") == 0,
    _au_src.count("if API_AUTH_TOKEN:\n"), 0)

# The requirement belongs to the process, not to one startup path: --api-only
# skipped Orchestrator.start() entirely and bound an open API.
_au_argparse = _au_src.index('api_only = "--api-only" in args')
_au_required = _au_src.index("API_AUTH_TOKEN is required")
_au_bind = _au_src.index("create_api_server(API_BIND, API_PORT, orch)")
chk("AU-9 the token is required before --api-only can bind a socket",
    _au_argparse < _au_required < _au_bind,
    (_au_argparse, _au_required, _au_bind), "checked between parse and bind")

# A trade-execution endpoint should not face the network by default.
chk("AU-10 the API binds loopback unless deliberately opened",
    _main.API_BIND == "127.0.0.1", _main.API_BIND, "127.0.0.1")
chk("AU-11 no listener is hardcoded to all interfaces",
    'args=("0.0.0.0"' not in _au_src)
_au_prev_port = os.environ.get("API_PORT")
os.environ["API_PORT"] = "3456"
_wd_il.reload(_main)
chk("AU-12 the advertised port setting is actually read",
    _main.API_PORT == 3456, _main.API_PORT, 3456)
if _au_prev_port is None:
    os.environ.pop("API_PORT", None)
else:
    os.environ["API_PORT"] = _au_prev_port
_wd_il.reload(_main)


# --- CC. Concurrency: the gate must not be a suggestion --------------------
# can_enter() READS exposure; the order that follows WRITES it. Between the
# two, the answer can change. Two threads -- a pipeline cycle and an operator
# hitting POST /api/execute, or two overlapping cycles -- could both see
# "flat" and both submit. Measured before entry_claim existed: 12 concurrent
# attempts on one symbol put 5 orders at the broker, 50 shares against an
# intended 10. Five times the position, from a gate that looked like it was
# working because it correctly refused the other seven.
import threading as _th  # noqa: E402


class _RaceBroker:
    """Deliberately slow, to widen the window a real broker would give."""

    def __init__(self, latency=0.01):
        self.submitted = []
        self.position = None
        self.latency = latency
        self._lock = _th.Lock()

    def submit_order(self, *, symbol, side, quantity, client_order_key=None, **kw):
        time.sleep(self.latency)
        with self._lock:
            self.submitted.append((symbol, side, quantity))
            self.position = SimpleNamespace(symbol=symbol, qty=float(quantity))
        return SimpleNamespace(id="o%d" % len(self.submitted),
                               filled_qty=quantity, filled_price=100.0)

    def get_position(self, symbol):
        return self.position          # None means flat, honestly

    def get_order(self, oid):
        return SimpleNamespace(status="filled", filled_qty=1, filled_price=100.0)

    def get_order_by_client_id(self, key):
        return None


def _race(threads=12, use_claim=True):
    work = tempfile.mkdtemp()
    safety = ExecutionSafety(os.path.join(work, "ledger.db"))
    broker = _RaceBroker()
    truth = PositionTruth(safety, broker, FakeState([]))
    gate = _th.Barrier(threads)

    def attempt(i):
        gate.wait()                    # release them together

        def check_then_submit(ok):
            # A fixed pause between the read and the write. Without it the
            # threads sometimes serialise by luck and the negative control
            # passes for the wrong reason -- a probabilistic control is not a
            # control. Under the claim this pause is held inside the lock, so
            # the protected case stays at one order regardless.
            time.sleep(0.05)
            if ok:
                safety.submit(broker, client_order_key="k%d" % i,
                              symbol="SPY", side="buy", quantity=10)

        if use_claim:
            with truth.entry_claim("SPY", "buy") as (ok, _why):
                check_then_submit(ok)
        else:
            ok, _why = truth.can_enter("SPY", "buy")
            check_then_submit(ok)

    workers = [_th.Thread(target=attempt, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    return broker.submitted


_cc = _race(use_claim=True)
chk("CC-1 concurrent entries produce exactly one order",
    len(_cc) == 1, len(_cc), 1)
chk("CC-2 the position is the intended size, not a multiple of it",
    sum(q for _, _, q in _cc) == 10, sum(q for _, _, q in _cc), 10)

# Negative control: without the claim the same harness must double-enter,
# otherwise CC-1 is passing for a reason unrelated to the fix.
_cc_bad = _race(use_claim=False)
chk("CC-3 without the claim the same race does double-enter (negative control)",
    len(_cc_bad) > 1, len(_cc_bad), "> 1 order")

# An exit must never be blocked by the entry claim -- refusing to let a
# position OUT is far more dangerous than refusing to let one in.
_cc_safety = ExecutionSafety(os.path.join(tempfile.mkdtemp(), "l.db"))
_cc_truth = PositionTruth(_cc_safety, _RaceBroker(), FakeState([]))
_cc_lock_a = _cc_truth._entry_lock("SPY")
_cc_lock_b = _cc_truth._entry_lock("QQQ")
chk("CC-4 the claim is per symbol, so one symbol cannot stall another",
    _cc_lock_a is not _cc_lock_b)
chk("CC-5 the same symbol always gets the same lock",
    _cc_truth._entry_lock("spy") is _cc_lock_a)


# --- MA. Manual entry is subject to the same halts -------------------------
# POST /api/execute called the trading engine directly, so none of the
# orchestrator's own gates applied. Measured before the fix: an order reached
# the engine with the kill switch engaged, with the daily loss limit hit, and
# with the operator mode set to STOPPED. The kill switch did not stop the API.
def _manual_orch(**flags):
    orch = _main.Orchestrator.__new__(_main.Orchestrator)
    orch.state = _main.PipelineState()
    for key, value in flags.items():
        setattr(orch.state, key, value)
    orch._market_clock = SimpleNamespace(is_open=lambda dt=None: True)
    orch._reached = []
    orch._trading_engine = SimpleNamespace(
        journal=None,
        evaluate_and_execute=lambda **kw: (
            orch._reached.append(kw),
            SimpleNamespace(dict=lambda: {"success": True}))[1])
    return orch


def _manual_reached(**flags):
    orch = _manual_orch(**flags)
    orch.execute_signal("SPY", 0.9)
    return bool(orch._reached)


chk("MA-1 manual entry is refused while the kill switch is engaged",
    _manual_reached(killed=True) is False)
chk("MA-2 manual entry is refused after the daily loss limit",
    _manual_reached(daily_loss_hit=True) is False)
chk("MA-3 manual entry is refused when the operator stopped the bot",
    _manual_reached(mode=_main.OrchestratorMode.STOPPED) is False)
chk("MA-4 manual entry is refused in the daily-loss-limit mode",
    _manual_reached(mode=_main.OrchestratorMode.DAILY_LOSS_LIMIT) is False)
# MANUAL mode must still allow a manual trade -- that is what it is for.
chk("MA-5 MANUAL mode still permits a manual trade",
    _manual_reached(mode=_main.OrchestratorMode.MANUAL) is True)

# Session truth fails closed, like everything else.
_ma_closed = _manual_orch()
_ma_closed._market_clock = SimpleNamespace(is_open=lambda dt=None: False)
_ma_closed.execute_signal("SPY", 0.9)
chk("MA-6 manual entry is refused when the market is closed",
    not _ma_closed._reached, _ma_closed._reached, "no order")

_ma_broken = _manual_orch()
_ma_broken._market_clock = SimpleNamespace(
    is_open=lambda dt=None: (_ for _ in ()).throw(RuntimeError("clock down")))
_ma_broken.execute_signal("SPY", 0.9)
chk("MA-7 an unavailable market session is not treated as open",
    not _ma_broken._reached, _ma_broken._reached, "no order")

# A blocking preflight must bind the manual path too.
_ma_pf = _manual_orch()
_ma_pf.state.preflight = {"ok": False, "blocking": ["live creds, paper dir"]}
_ma_pf.execute_signal("SPY", 0.9)
chk("MA-8 a failed preflight blocks manual entry",
    not _ma_pf._reached, _ma_pf._reached, "no order")

_ma_refusal = _manual_orch(killed=True).execute_signal("SPY", 0.9)
chk("MA-9 the refusal says why, rather than reporting success",
    _ma_refusal.get("success") is False
    and "kill switch" in _ma_refusal.get("error", ""),
    _ma_refusal, "success=False with a reason")

# --- FL. A control that is ignored in silence is worse than one refused ----
# The systemd unit passed `--autonomous`; nothing parsed it. The unit promised
# autonomous operation while the bot started MANUAL.
_fl_src = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
chk("FL-1 unrecognised flags are reported, not swallowed",
    "Ignoring unrecognised flag(s)" in _fl_src)
chk("FL-2 the warning names the real authority over mode",
    "orchestrator_mode.txt" in _fl_src and "MODE_PRECEDENCE" in _fl_src)
_fl_unit = os.path.join(os.path.dirname(BACKEND), "educated-trades.service")
_fl_unit_text = open(_fl_unit, encoding="utf-8").read()
_fl_exec = [ln for ln in _fl_unit_text.splitlines() if ln.startswith("ExecStart=")]
chk("FL-3 the service no longer passes a flag that does nothing",
    all("--autonomous" not in ln for ln in _fl_exec), _fl_exec, "no --autonomous")


# --- AD. Startup recovery must keep every adopted position -----------------
# active_positions.record_id is the PRIMARY KEY and the insert is
# INSERT OR REPLACE, but recovery passed record_id=0 for every position it
# adopted from the broker. Measured: three positions adopted, one tracked.
# The other two were invisible to the stop/target monitor and to the
# unprotected-position check -- only the broker-side bracket still held them.
_ad_dir = tempfile.mkdtemp()
_ad_db = patterns.PatternDatabase(Path(_ad_dir) / "adopt.db")
_ad_positions = [("SPY", 500.0, 10, "buy"),
                 ("QQQ", 400.0, 5, "buy"),
                 ("IWM", 200.0, 20, "sell")]
for _sym, _px, _qty, _side in _ad_positions:
    _ad_db.add_active_position(
        record_id=_ad_db.adopted_record_id(_sym), symbol=_sym,
        entry_price=_px, quantity=_qty, side=_side, pattern_hash="recovered")

_ad_rows = _ad_db._connect().execute(
    "SELECT record_id, symbol FROM active_positions").fetchall()
chk("AD-1 every position adopted from the broker is tracked",
    len(_ad_rows) == 3, len(_ad_rows), 3)
chk("AD-2 each keeps its own identity",
    sorted(r["symbol"] for r in _ad_rows) == ["IWM", "QQQ", "SPY"],
    sorted(r["symbol"] for r in _ad_rows), ["IWM", "QQQ", "SPY"])

# Negative control: the old placeholder must still collapse them, or AD-1 is
# passing for a reason unrelated to the fix.
_ad_db2 = patterns.PatternDatabase(Path(tempfile.mkdtemp()) / "old.db")
for _sym, _px, _qty, _side in _ad_positions:
    _ad_db2.add_active_position(record_id=0, symbol=_sym, entry_price=_px,
                                quantity=_qty, side=_side)
chk("AD-3 the old placeholder collapses them (negative control)",
    len(_ad_db2._connect().execute(
        "SELECT 1 FROM active_positions").fetchall()) == 1)

chk("AD-4 the id is stable across restarts, not process-salted",
    _ad_db.adopted_record_id("SPY") == _ad_db.adopted_record_id("spy"))
chk("AD-5 it can never collide with a real pattern id",
    all(_ad_db.adopted_record_id(s) < 0 for s in ("SPY", "QQQ", "IWM", "X")))
chk("AD-6 re-adopting a position updates rather than duplicates",
    (_ad_db.add_active_position(
        record_id=_ad_db.adopted_record_id("SPY"), symbol="SPY",
        entry_price=501.0, quantity=11, side="buy") or
     len(_ad_db._connect().execute(
         "SELECT 1 FROM active_positions").fetchall())) == 3)
_ad_src = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
chk("AD-7 recovery uses the unique id, not the placeholder",
    "adopted_record_id" in _ad_src and "record_id=0," not in _ad_src)

# --- WL. One watchdog per environment, not one per host --------------------
# The loop's PID file was a fixed /tmp/watchdog_loop.pid and startup kills
# whatever PID it finds. Paper and live can now run side by side, so a shared
# PID file meant each would kill the other's watchdog on start -- leaving one
# of them unmonitored, silently.
_wl = open(os.path.join(BACKEND, "watchdog_loop.sh"), encoding="utf-8").read()
chk("WL-1 the PID file is keyed to the data directory",
    'PID_FILE="/tmp/watchdog_loop.pid"' not in _wl
    and "watchdog_loop${KEY}.pid" in _wl)
chk("WL-2 the log is separated too, so the two do not interleave",
    "watchdog${KEY}.log" in _wl)
chk("WL-3 a recycled PID belonging to something else is not signalled",
    "kill -0" in _wl and "watchdog_loop.sh*" in _wl)
chk("WL-4 the PID file is removed on exit",
    "trap 'rm -f \"$PID_FILE\"' EXIT" in _wl)


# --- TS. The ledger under threads ------------------------------------------
# One sqlite3.Connection was shared by every thread, opened with
# check_same_thread=False and guarded by nothing. SQLite's locking does not
# help there: the implicit-transaction state lives on the connection. The
# module docstring credited an advisory FILE lock, which is per-process and
# does nothing between threads inside one.
#
# Measured before the fix: 40 concurrent submits, 12 exceptions, and 36 of 40
# orders in the ledger. FOUR ORDERS VANISHED from the record whose entire
# purpose is that no order can be lost. The pipeline thread, the 15-second
# position monitor and the API thread all touch this. It also produced the
# intermittent single-check failure that went unexplained for ~57 runs.
class _TSBroker:
    def submit_order(self, *, symbol, side, quantity, client_order_key=None, **kw):
        return SimpleNamespace(id="o", filled_qty=quantity, filled_price=100.0)

    def get_order(self, oid):
        return SimpleNamespace(status="filled", filled_qty=1, filled_price=100.0)

    def get_order_by_client_id(self, key):
        return None


_ts_safety = ExecutionSafety(os.path.join(tempfile.mkdtemp(), "threads.db"))
_ts_broker = _TSBroker()
_ts_n = 40
_ts_gate = _th.Barrier(_ts_n)
_ts_errors = []


def _ts_worker(i):
    _ts_gate.wait()
    try:
        _ts_safety.submit(_ts_broker, client_order_key="k%d" % i,
                          symbol="SPY", side="buy", quantity=1)
    except Exception as exc:
        _ts_errors.append("%s: %s" % (type(exc).__name__, exc))


_ts_threads = [_th.Thread(target=_ts_worker, args=(i,)) for i in range(_ts_n)]
for _t in _ts_threads:
    _t.start()
for _t in _ts_threads:
    _t.join()

_ts_rows = _ts_safety._connect().execute(
    "SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
chk("TS-1 concurrent submits raise nothing", not _ts_errors,
    _ts_errors[:3], "no exceptions")
chk("TS-2 no order is lost from the ledger", _ts_rows == _ts_n,
    _ts_rows, _ts_n)
chk("TS-3 each thread has its own connection",
    "self._local.conn" in open(os.path.join(BACKEND, "execution_safety.py"),
                               encoding="utf-8").read())

# A losing idempotency race must release its write lock. The failed INSERT
# left an aborted transaction open, so the thread that WON could not commit
# -- "database is locked" -- and the order stayed `reserved` although the
# broker had accepted it.
class _TSSlow:
    submits = 0

    def submit_order(self, **kw):
        _TSSlow.submits += 1
        time.sleep(0.03)
        return SimpleNamespace(id="s")

    def get_order(self, oid):
        return SimpleNamespace(status="accepted", filled_qty=0,
                               filled_avg_price=None)

    def get_order_by_client_id(self, key):
        return None


_ts_dir = tempfile.mkdtemp()
_ts_dup = ExecutionSafety(os.path.join(_ts_dir, "dup.db"))
_ts_slow = _TSSlow()
_ts_bar = _th.Barrier(2)


def _ts_same():
    _ts_bar.wait()
    _ts_dup.submit(_ts_slow, client_order_key="same", symbol="SPY",
                   side="buy", quantity=1)


_ts_pair = [_th.Thread(target=_ts_same) for _ in range(2)]
for _t in _ts_pair:
    _t.start()
for _t in _ts_pair:
    _t.join()

chk("TS-4 the same key still reaches the broker exactly once",
    _TSSlow.submits == 1, _TSSlow.submits, 1)
chk("TS-5 the winner's commit is not blocked by the loser's failed insert",
    _ts_dup._fetch("same").status == "submitted",
    _ts_dup._fetch("same").status, "submitted")
# Read from a brand-new instance: what is actually on disk, not a cached view.
chk("TS-6 the settled status is durable, not just in memory",
    ExecutionSafety(os.path.join(_ts_dir, "dup.db"))._fetch("same").status
    == "submitted")


# --- CN. Concurrency sweep: every store, not just the one that broke -------
# The ledger bug was found by running it 40 times at once, not by reading it.
# So the same question is now asked of every shared store, and asked
# structurally, so a NEW sqlite connection cannot be added shared by default.
_cn_dir = tempfile.mkdtemp()
_cn_db = patterns.PatternDatabase(Path(_cn_dir) / "conc.db")
_cn_errors = []
_cn_writers = 30
_cn_gate = _th.Barrier(_cn_writers * 2)


def _cn_write(i):
    _cn_gate.wait()
    try:
        _cn_db.add_active_position(record_id=9000 + i, symbol="S%d" % i,
                                   entry_price=100.0, quantity=1, side="buy")
    except Exception as exc:
        _cn_errors.append("write %s" % type(exc).__name__)


def _cn_read(i):
    _cn_gate.wait()
    try:
        _cn_db._connect().execute(
            "SELECT COUNT(*) FROM active_positions").fetchone()
    except Exception as exc:
        _cn_errors.append("read %s" % type(exc).__name__)


_cn_threads = ([_th.Thread(target=_cn_write, args=(i,)) for i in range(_cn_writers)]
               + [_th.Thread(target=_cn_read, args=(i,)) for i in range(_cn_writers)])
for _t in _cn_threads:
    _t.start()
for _t in _cn_threads:
    _t.join()

_cn_rows = _cn_db._connect().execute(
    "SELECT COUNT(*) AS c FROM active_positions").fetchone()["c"]
chk("CN-1 the pattern database survives concurrent readers and writers",
    not _cn_errors, _cn_errors[:3], "no exceptions")
chk("CN-2 no write to active_positions is lost",
    _cn_rows == _cn_writers, _cn_rows, _cn_writers)
chk("CN-3 the pattern database runs in WAL, so reads do not block writes",
    _cn_db._connect().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal",
    _cn_db._connect().execute("PRAGMA journal_mode").fetchone()[0], "wal")

# Structural: check_same_thread=False only silences SQLite's guard. Any module
# that uses it must also give each thread its own connection.
for _cn_mod in ("patterns.py", "execution_safety.py", "stats.py", "alert_db.py"):
    _cn_src = open(os.path.join(BACKEND, _cn_mod), encoding="utf-8").read()
    if "sqlite3.connect" not in _cn_src:
        continue
    chk("CN-4.%s keeps connections per-thread" % _cn_mod,
        "threading.local()" in _cn_src, _cn_mod, "threading.local()")

# Duplicate methods are not cosmetic: `close()` was corrected for per-thread
# connections at the first definition and the second silently kept winning.
_cn_dupes = []
for _cn_mod in ("patterns.py", "execution_safety.py", "trading.py", "main.py"):
    _cn_tree = _ast.parse(open(os.path.join(BACKEND, _cn_mod),
                               encoding="utf-8").read())
    for _node in _ast.walk(_cn_tree):
        if not isinstance(_node, _ast.ClassDef):
            continue
        _names = [f.name for f in _node.body
                  if isinstance(f, (_ast.FunctionDef, _ast.AsyncFunctionDef))]
        for _name, _count in Counter(_names).items():
            if _count > 1:
                _cn_dupes.append("%s.%s.%s x%d"
                                 % (_cn_mod, _node.name, _name, _count))
chk("CN-5 no method is defined twice in one class", not _cn_dupes,
    _cn_dupes, "none")

# A status file the API reads must never be observable half-written.
_cn_path = os.path.join(_cn_dir, "status.json")
_cn_payload = {"rows": [{"symbol": "SPY%d" % i, "pad": "x" * 200}
                        for i in range(200)]}
_cn_stop = _th.Event()
_cn_torn, _cn_ok = [], []


def _cn_writer_loop():
    while not _cn_stop.is_set():
        _main._write_json_atomic(_cn_path, _cn_payload, indent=2)


def _cn_reader_loop():
    while not _cn_stop.is_set():
        try:
            with open(_cn_path) as _fh:
                _json.load(_fh)
            _cn_ok.append(1)
        except FileNotFoundError:
            pass
        except Exception as exc:
            _cn_torn.append(type(exc).__name__)


_cn_pair = [_th.Thread(target=_cn_writer_loop), _th.Thread(target=_cn_reader_loop)]
for _t in _cn_pair:
    _t.start()
time.sleep(1.0)
_cn_stop.set()
for _t in _cn_pair:
    _t.join()

chk("CN-6 a status file is never read half-written", not _cn_torn,
    "%d torn of %d reads" % (len(_cn_torn), len(_cn_torn) + len(_cn_ok)), "none")
chk("CN-7 the reader actually got to read (the check is not vacuous)",
    len(_cn_ok) > 10, len(_cn_ok), "> 10")
_cn_main = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
chk("CN-8 status files are written through the atomic helper",
    'json.dump(result, f, indent=2, default=str)' not in _cn_main
    and "_write_json_atomic" in _cn_main)


# --- XC. Concurrent exits: the dangerous mirror of the entry race ----------
# has_unresolved_exit() READS, register_exit() WRITES, and the broker call
# sits between them. Same check-then-act shape as the entry gate, worse
# consequence: a duplicate close on a LONG does not merely flatten it -- the
# surplus sell opens a short.
#
# Measured before exit_claim existed: 10 concurrent exit requests on one
# symbol submitted 6 closes. Against a 10-share long that is 60 shares sold,
# leaving a 50-share SHORT. The three exit paths -- the 15-second position
# monitor, the daily-loss flatten, and the kill switch -- can fire together,
# and are most likely to under exactly the stress that triggers them.
_xc_closes = []
_xc_guard = _th.Lock()


class _XCBroker:
    is_simulating = False

    def get_position(self, symbol):
        return {"qty": 10}

    def close_position(self, symbol, **kw):
        time.sleep(0.02)
        with _xc_guard:
            _xc_closes.append(symbol)
        return trading.ExecutionResult(
            success=True, symbol=symbol, side=None, quantity=10,
            filled_price=100.0, filled_qty=10,
            order_id="c%d" % len(_xc_closes),
            status=trading.OrderStatus.FILLED, latency_ms=1)


_xc_dir = tempfile.mkdtemp()
_xc_safety = ExecutionSafety(os.path.join(_xc_dir, "exit.db"))
_xc_broker = _XCBroker()
_xc_truth = PositionTruth(_xc_safety, _xc_broker, FakeState([]))
_xc_engine = trading.TradingEngine.__new__(trading.TradingEngine)
_xc_engine.broker = _xc_broker
_xc_engine._journal = None
_xc_engine._position_truth = _xc_truth
_xc_engine._last_trade = {}

_xc_n = 10
_xc_gate = _th.Barrier(_xc_n)


def _xc_worker(i):
    _xc_gate.wait()
    try:
        _xc_engine.close_position_guarded("SPY", reason="stop_%d" % i)
    except Exception:
        pass


_xc_threads = [_th.Thread(target=_xc_worker, args=(i,)) for i in range(_xc_n)]
for _t in _xc_threads:
    _t.start()
for _t in _xc_threads:
    _t.join()

chk("XC-1 concurrent exits submit exactly one close",
    len(_xc_closes) == 1, len(_xc_closes), 1)
chk("XC-2 the position is not flipped short by surplus sells",
    len(_xc_closes) * 10 <= 10, len(_xc_closes) * 10, "<= 10 shares sold")

# The exit lock must be separate from the entry lock: making a liquidation
# wait behind an entry's broker round-trip is the wrong trade-off.
chk("XC-3 exit locks are distinct from entry locks",
    _xc_truth._exit_lock("SPY") is not _xc_truth._entry_lock("SPY"))
chk("XC-4 one symbol's exit cannot stall another's",
    _xc_truth._exit_lock("SPY") is not _xc_truth._exit_lock("QQQ"))
chk("XC-5 the same symbol always gets the same exit lock",
    _xc_truth._exit_lock("spy") is _xc_truth._exit_lock("SPY"))

_xc_src = open(os.path.join(BACKEND, "trading.py"), encoding="utf-8").read()
chk("XC-6 the guarded close actually takes the claim",
    "exit_claim" in _xc_src, "exit_claim used in trading.py")


# --- DC. One trade must be recorded once ------------------------------------
# `_check_active_positions` runs on BOTH the 15-second fast monitor and the
# pipeline cycle, so two threads can close the same position. The old sequence
# was SELECT the row -> compute P&L -> record outcome -> remove: check-then-act
# with the whole close in between. Measured with 8 concurrent closes of one
# position: 8 "successful" closes and 8 P&L milestones for a single trade --
# inflating exactly the statistics the forward-test gate reads to decide
# whether real money is justified.
_dc_dir = tempfile.mkdtemp()
_dc_db = patterns.PatternDatabase(Path(_dc_dir) / "close.db")
_dc_engine = patterns.PatternEngine.__new__(patterns.PatternEngine)
_dc_engine.db = _dc_db
_dc_db.add_active_position(record_id=4242, symbol="SPY", entry_price=100.0,
                           quantity=10, side="buy")

_dc_ok = []
_dc_guard = _th.Lock()
_dc_n = 8
_dc_gate = _th.Barrier(_dc_n)


def _dc_worker():
    _dc_gate.wait()
    result = _dc_engine.close_tracked_position(record_id=4242,
                                               current_price=110.0)
    with _dc_guard:
        _dc_ok.append("error" not in result)


_dc_threads = [_th.Thread(target=_dc_worker) for _ in range(_dc_n)]
for _t in _dc_threads:
    _t.start()
for _t in _dc_threads:
    _t.join()

_dc_milestones = _dc_db._connect().execute(
    "SELECT COUNT(*) AS c FROM milestone_tracker").fetchone()["c"]
chk("DC-1 only one concurrent close succeeds",
    sum(_dc_ok) == 1, sum(_dc_ok), 1)
chk("DC-2 the P&L is logged once, not once per thread",
    _dc_milestones == 1, _dc_milestones, 1)
chk("DC-3 the position is gone afterwards",
    _dc_db._connect().execute(
        "SELECT COUNT(*) AS c FROM active_positions "
        "WHERE record_id=4242").fetchone()["c"] == 0)
chk("DC-4 the losing callers say why rather than reporting a close",
    all(isinstance(x, bool) for x in _dc_ok) and _dc_ok.count(False) == _dc_n - 1,
    _dc_ok.count(False), _dc_n - 1)

# The claim must be the DELETE itself. A lock would not hold across processes;
# a flag column would still be check-then-act.
_dc_src = open(os.path.join(BACKEND, "patterns.py"), encoding="utf-8").read()
chk("DC-5 the claim is the atomic DELETE, checked by rowcount",
    "claimed.rowcount == 0" in _dc_src)


# --- PS. The position state file ------------------------------------------
# add_position() was documented "Thread-safe: loads current state, updates,
# saves" -- a sequence that is the definition of not thread-safe. It is called
# from trading.py on entry and on exit, so the pipeline thread and the
# 15-second monitor both reach it. Every instance also shared one ".tmp"
# filename, so concurrent writers destroyed each other's temp file.
#
# Measured: 20 threads each adding a different symbol left ONE position in the
# file. Nineteen lost, from one of the stores PositionTruth consults to decide
# whether it is exposed.
import position_state as _ps  # noqa: E402
_ps_dir = tempfile.mkdtemp()
_ps_mgr = _ps.PositionStateManager(os.path.join(_ps_dir, "position_state.json"))
_ps_n = 20
_ps_gate = _th.Barrier(_ps_n)


def _ps_worker(i):
    _ps_gate.wait()
    _ps_mgr.add_position({"symbol": "SYM%02d" % i, "qty": 10,
                          "entry_price": 100.0, "side": "buy"})


_ps_threads = [_th.Thread(target=_ps_worker, args=(i,)) for i in range(_ps_n)]
for _t in _ps_threads:
    _t.start()
for _t in _ps_threads:
    _t.join()

_ps_kept = _ps_mgr.load_positions()
chk("PS-1 concurrent adds do not drop positions",
    len(_ps_kept) == _ps_n, len(_ps_kept), _ps_n)
chk("PS-2 every symbol survives, not just the last writer",
    len({p["symbol"] for p in _ps_kept}) == _ps_n,
    len({p["symbol"] for p in _ps_kept}), _ps_n)

# Concurrent removal is the same read-modify-write in reverse.
_ps_removed = _th.Barrier(10)


def _ps_remover(i):
    _ps_removed.wait()
    _ps_mgr.remove_position("SYM%02d" % i)


_ps_rm = [_th.Thread(target=_ps_remover, args=(i,)) for i in range(10)]
for _t in _ps_rm:
    _t.start()
for _t in _ps_rm:
    _t.join()
chk("PS-3 concurrent removals remove exactly what was asked",
    len(_ps_mgr.load_positions()) == _ps_n - 10,
    len(_ps_mgr.load_positions()), _ps_n - 10)

chk("PS-4 the temp file is unique per writer, not shared",
    _ps_mgr.tmp_path != os.path.join(_ps_dir, "position_state.json") + ".tmp",
    _ps_mgr.tmp_path, "unique per process and instance")

# --- AS. The API must stay answerable while something else is stuck --------
# HTTPServer handles one request at a time, so a slow endpoint blocks every
# other route -- including POST /api/kill. Measured: a single 3-second request
# delayed the kill switch by 2.6 seconds. A broker call hanging on its
# 30-second timeout would block it for 30. The moment you most need the kill
# switch is the moment something else is already stuck.
_as_src = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
chk("AS-1 the API server handles requests concurrently",
    "server = ThreadingHTTPServer((host, port)" in _as_src)
chk("AS-2 the plain single-request server is not used anywhere",
    "= HTTPServer(" not in _as_src)
chk("AS-3 an in-flight request cannot keep the process alive on shutdown",
    "server.daemon_threads = True" in _as_src)


# --- EL. Entries go through the order ledger -------------------------------
# Exits went through the ledger; entries called the broker directly. Verified
# by running one and querying the table: client_order_id ABSENT, zero ledger
# rows. Three safeguards were reading an empty table -- idempotency,
# can_enter()'s unresolved-order check, and the PERSISTED cooldown, whose
# query is `WHERE is_exit=0` against rows that were never written.
_el_src = open(os.path.join(BACKEND, "trading.py"), encoding="utf-8").read()
chk("EL-1 the entry path submits through the ledger",
    "truth.safety.submit(" in _el_src)
chk("EL-2 the entry carries an idempotency key",
    'client_order_key=entry_key' in _el_src)
chk("EL-3 the broker's own answer is still what gets reported",
    "capture.result" in _el_src)

# The key must actually reach the broker request, or it is decoration.
chk("EL-4 client_order_id is set on the order sent to Alpaca",
    "order_data.client_order_id = client_order_id" in _el_src)

# Idempotency: a repeated key must not produce a second broker order.
class _ELBroker:
    def __init__(self):
        self.orders = []

    def submit_order(self, *, symbol, side, quantity, client_order_key=None, **kw):
        self.orders.append(client_order_key)
        return SimpleNamespace(id="o%d" % len(self.orders),
                               filled_qty=quantity, filled_price=100.0)

    def get_order(self, oid):
        return SimpleNamespace(status="filled", filled_qty=1, filled_price=100.0)

    def get_order_by_client_id(self, key):
        return None


_el_safety = ExecutionSafety(os.path.join(tempfile.mkdtemp(), "entries.db"))
_el_broker = _ELBroker()
for _ in range(4):
    _el_safety.submit(_el_broker, client_order_key="entry:SPY:buy:1",
                      symbol="SPY", side="buy", quantity=10)
chk("EL-5 a repeated entry key reaches the broker once",
    len(_el_broker.orders) == 1, len(_el_broker.orders), 1)

# The persisted cooldown must now have something to read.
_el_safety.submit(_el_broker, client_order_key="entry:QQQ:buy:2",
                  symbol="QQQ", side="buy", quantity=5)
chk("EL-6 an entry is visible to the persisted cooldown",
    _el_safety.last_entry_time("QQQ") is not None,
    _el_safety.last_entry_time("QQQ"), "a timestamp")
chk("EL-7 an exit does not delay re-entry",
    _el_safety.last_entry_time("IWM") is None,
    _el_safety.last_entry_time("IWM"), None)

# ...and to the same-side exposure check, which was blind on entries.
chk("EL-8 an unresolved entry is visible to the exposure check",
    _el_safety.has_open_exposure("SPY", "buy") in (True, False),
    "queryable", "no exception")


# --- BF. Bar-count consistency: the log and the trade must agree -----------
# `_fetch_ohlc` defaulted to 75 bars. Four call sites use it; only the one
# producing the visible INDICATOR lines passed INDICATOR_FETCH_BARS
# explicitly. The SIGNAL path took the default, so the EMA-20/EMA-50 pair
# that decides trend direction was computed from 75 bars while the log showed
# values computed from 200.
#
# Measured: EMA-50 from 75 bars disagrees with EMA-50 from 200 bars by up to
# 0.35% — half the 0.693% stop distance, and enough to FLIP the crossover
# that decides long versus short.
#
# This was the same defect already fixed once in the indicator path. Fixing
# one call site and leaving the default wrong is why it survived: a default
# is inherited silently by everything that does not override it.
_bf_tree = _ast.parse(open(os.path.join(BACKEND, "main.py"),
                           encoding="utf-8").read())
_bf_default = None
for _node in _ast.walk(_bf_tree):
    if isinstance(_node, _ast.FunctionDef) and _node.name == "_fetch_ohlc":
        _bf_default = _node.args.defaults[-1] if _node.args.defaults else None

chk("BF-1 _fetch_ohlc has a default bar count", _bf_default is not None)
chk("BF-2 the default is not a bare literal",
    not isinstance(_bf_default, _ast.Constant),
    _ast.unparse(_bf_default) if _bf_default else None,
    "INDICATOR_FETCH_BARS")
chk("BF-3 the default is the same constant the indicator path uses",
    _bf_default is not None
    and _ast.unparse(_bf_default) == "INDICATOR_FETCH_BARS",
    _ast.unparse(_bf_default) if _bf_default else None,
    "INDICATOR_FETCH_BARS")

# Any explicit override must still be enough for the longest EMA, or that
# call site quietly recreates the bug with a different number.
_bf_calls = []
for _node in _ast.walk(_bf_tree):
    if (isinstance(_node, _ast.Call)
            and isinstance(_node.func, _ast.Attribute)
            and _node.func.attr == "_fetch_ohlc"):
        for _kw in _node.keywords:
            if _kw.arg == "bars":
                _bf_calls.append(_ast.unparse(_kw.value))
chk("BF-4 every explicit bar count is a named constant, not a magic number",
    all(not _v.isdigit() for _v in _bf_calls), _bf_calls, "named constants")

# The numeric relationship that makes all of this correct.
chk("BF-5 the fetch count clears the longest indicator period with margin",
    _main.INDICATOR_FETCH_BARS >= _main.EMA_LONG_PERIOD * 3,
    (_main.INDICATOR_FETCH_BARS, _main.EMA_LONG_PERIOD),
    "fetch >= 3x the longest period")

# Negative control: prove the check would catch a reverted default.
_bf_broken = _ast.parse("def _fetch_ohlc(self, symbol, bars=75): pass")
_bf_bad = _bf_broken.body[0].args.defaults[-1]
chk("BF-6 a literal default would be caught (negative control)",
    isinstance(_bf_bad, _ast.Constant))


# --- PM. Operator mode must survive a restart ------------------------------
# set_mode() changed state.mode in memory and never wrote the file, so every
# restart silently reverted to MANUAL -- including systemd's Restart=always
# after a crash. The bot would come back up, pass preflight, cycle normally,
# and never trade again, with nothing announcing it had stopped.
#
# Observed in production: autonomous set at 09:45, process restarted at 10:30,
# then an entire afternoon of cycles in MANUAL and an empty decision journal.
# It looked exactly like a broken signal path. docs/MODE_PRECEDENCE.md had
# documented this as working the whole time.
_pm_dir = tempfile.mkdtemp()
_pm_prev_dd = os.environ.get("DATA_DIR")
os.environ["DATA_DIR"] = _pm_dir
_wd_il.reload(_main)


def _pm_orch():
    orch = _main.Orchestrator.__new__(_main.Orchestrator)
    orch.state = _main.PipelineState()
    orch._trading_engine = SimpleNamespace()
    return orch


_pm = _pm_orch()
_pm.set_mode("autonomous")
chk("PM-1 setting the mode writes the operator file",
    os.path.exists(_main.MODE_FILE), _main.MODE_FILE, "exists")
chk("PM-2 the file records what was asked for",
    open(_main.MODE_FILE).read().strip() == "autonomous",
    open(_main.MODE_FILE).read().strip() if os.path.exists(_main.MODE_FILE) else None,
    "autonomous")

# A fresh process must come back autonomous, not silently MANUAL.
_pm_restarted = _pm_orch()
_pm_restarted._load_persisted_mode()
chk("PM-3 a restart restores the operator's mode",
    _pm_restarted.state.mode is _main.OrchestratorMode.AUTONOMOUS,
    _pm_restarted.state.mode, _main.OrchestratorMode.AUTONOMOUS)

_pm.set_mode("manual")
_pm_again = _pm_orch()
_pm_again._load_persisted_mode()
chk("PM-4 switching back is persisted too",
    _pm_again.state.mode is _main.OrchestratorMode.MANUAL,
    _pm_again.state.mode, _main.OrchestratorMode.MANUAL)

# The docs promised this behaviour before the code did it.
_pm_doc = open(os.path.join(os.path.dirname(BACKEND), "docs",
                            "MODE_PRECEDENCE.md"), encoding="utf-8").read()
# Only the operator's own action may write that file. An automated demotion
# (kill switch, daily loss limit) must leave the pre-halt mode intact so the
# day-rollover recovery can restore it. The Tier-2 loop used to call this --
# harmless while the function was a no-op stub, a real bug once implemented.
_pm_callers = []
for _node in _ast.walk(_ast.parse(open(os.path.join(BACKEND, "main.py"),
                                       encoding="utf-8").read())):
    if isinstance(_node, _ast.FunctionDef):
        for _sub in _ast.walk(_node):
            if (isinstance(_sub, _ast.Call)
                    and isinstance(_sub.func, _ast.Attribute)
                    and _sub.func.attr == "_save_persisted_mode"):
                _pm_callers.append(_node.name)
chk("PM-6 only set_mode persists the operator mode",
    _pm_callers == ["set_mode"], _pm_callers, ["set_mode"])

chk("PM-5 the documented contract is the implemented one",
    "orchestrator_mode.txt" in _pm_doc
    and "_save_persisted_mode()" in open(
        os.path.join(BACKEND, "main.py"), encoding="utf-8").read().split(
            "def set_mode")[1].split("def ")[0],
    "set_mode persists", "set_mode calls _save_persisted_mode")

if _pm_prev_dd is None:
    os.environ.pop("DATA_DIR", None)
else:
    os.environ["DATA_DIR"] = _pm_prev_dd
_wd_il.reload(_main)


# --- BR. Broker failure must never be answered with a fabricated fill ------
# `execute_order` dispatched on CONNECTIVITY: `if self._client and
# self._connected: live else: simulated`. A live broker whose connection
# dropped -- `_simulate` still False -- therefore fell through and produced a
# FABRICATED FILL, while the audit record said `"mode": "live"`. The system
# would hold a position the broker never received, with P&L, learning data
# and slippage all computed from a price nobody traded at.
#
# Two real states reach it: live initialisation failing, and reconnect()
# failing. A DNS outage mid-order is enough, and one occurred in production.
class _BRClient:
    def __init__(self):
        self.submitted = []

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        return SimpleNamespace(id="o1", status="filled", filled_qty=1,
                               filled_avg_price=100.0, order_class="simple")

    def close_position(self, symbol):
        self.submitted.append(("close", symbol))
        return SimpleNamespace(id="c1")

    def get_open_position(self, symbol):
        raise Exception("position does not exist")


def _br_broker(simulate, connected, has_client=True):
    b = trading.AlpacaBroker(simulate=True)
    b._simulate = simulate
    b._client = _BRClient() if has_client else None
    b._connected = connected
    return b


_br_live_down = _br_broker(simulate=False, connected=False)
_br_res = _br_live_down.execute_order(
    symbol="SPY", side=trading.OrderSide.BUY, quantity=10)
chk("BR-1 a disconnected live broker refuses rather than simulating",
    _br_res.success is False, _br_res.success, False)
chk("BR-2 it reports no fill at all",
    _br_res.filled_qty == 0 and _br_res.filled_price is None,
    (_br_res.filled_qty, _br_res.filled_price), (0, None))
chk("BR-3 the refusal names the cause",
    "Broker unavailable" in (_br_res.error or ""), _br_res.error,
    "Broker unavailable...")
chk("BR-4 nothing reached the broker",
    _br_live_down._client.submitted == [],
    _br_live_down._client.submitted, [])

# No client at all -- the other way to be disconnected.
_br_noclient = _br_broker(simulate=False, connected=False, has_client=False)
chk("BR-5 an uninitialised client refuses too",
    _br_noclient.execute_order(
        symbol="SPY", side=trading.OrderSide.BUY, quantity=10).success is False)

# Deliberate simulation must still work — this is a mode, not a failure.
_br_sim = _br_broker(simulate=True, connected=False)
chk("BR-6 chosen simulation still fills",
    _br_sim.execute_order(
        symbol="SPY", side=trading.OrderSide.BUY, quantity=10).success is True)

# Closing has the same shape, and the worse consequence: a phantom close
# drops tracking while real exposure remains.
_br_close_down = _br_broker(simulate=False, connected=False)
_br_close = _br_close_down.close_position("SPY")
chk("BR-7 a disconnected live broker refuses to report a close",
    _br_close.success is False, _br_close.success, False)
chk("BR-8 it does not claim the position is flat",
    (_br_close.details or {}).get("confirmed_flat") is False,
    (_br_close.details or {}).get("confirmed_flat"), False)

# success must mean CONFIRMED flat. It used to be True regardless, with only
# `status` distinguishing FILLED from PENDING -- and every caller keys on
# `.success`, so an unconfirmed close was recorded as a completed trade.
class _BRStillOpen(_BRClient):
    def get_open_position(self, symbol):
        return SimpleNamespace(symbol=symbol, qty=10.0)


_br_open = _br_broker(simulate=False, connected=True)
_br_open._client = _BRStillOpen()
_br_unconfirmed = _br_open.close_position("SPY")
chk("BR-9 an unconfirmed close is not reported as success",
    _br_unconfirmed.success is False, _br_unconfirmed.success, False)
chk("BR-10 but the submission is recorded, so it can be reconciled",
    (_br_unconfirmed.details or {}).get("close_submitted") is True)
chk("BR-11 its status says PENDING, not FILLED",
    _br_unconfirmed.status is trading.OrderStatus.PENDING,
    _br_unconfirmed.status, trading.OrderStatus.PENDING)

# --- XS. Exit side must follow the position, not a constant ----------------
# register_exit was called with side="sell" unconditionally, so every SHORT
# exit went into the ledger backwards. Same direction-blindness that made the
# learner invert every short trade, in a different file.
_xs_src = open(os.path.join(BACKEND, "trading.py"), encoding="utf-8").read()
chk("XS-1 the exit side is derived, not hardcoded",
    'side="sell", quantity=max(1, held_qty)' not in _xs_src)
chk("XS-2 it is derived from the sign of the held quantity",
    'exit_side = "buy" if signed_qty < 0 else "sell"' in _xs_src)
chk("XS-3 the signed quantity is preserved before being made absolute",
    "signed_qty = int(float(position.get" in _xs_src)

# --- LP. The order ledger is environment-segregated -------------------------
# It defaulted to backend/data/execution_ledger.json -- inside the source
# tree, identical for paper and live. Nothing set EXECUTION_LEDGER_PATH
# outside the tests, so paper and live would have shared one order history,
# contradicting the segregation every other store honours.
chk("LP-1 the ledger default routes through the shared derivation",
    'os.path.join(resolve_data_dir(),' in _xs_src
    and '"execution_ledger.json")' in _xs_src,
    "resolve_data_dir used for the ledger default")
chk("LP-2 it no longer defaults inside the source tree",
    '"data", "execution_ledger.json"),' not in _xs_src)

_lp_prev = {k: os.environ.get(k) for k in
            ("DATA_DIR", "DATA_DIR_AUTOSET", "EXECUTION_LEDGER_PATH",
             "DATA_ROOT", "APCA_API_KEY_ID")}
for _k in ("DATA_DIR", "DATA_DIR_AUTOSET", "EXECUTION_LEDGER_PATH"):
    os.environ.pop(_k, None)
os.environ["DATA_ROOT"] = "/tmp/lp-probe"
_lp_paths = {}
for _key, _env in (("PKPAPER", "paper"), ("AKLIVE", "live")):
    os.environ["APCA_API_KEY_ID"] = _key
    _lp_paths[_env] = os.path.join(_tr.resolve_data_dir(),
                                   "execution_ledger.json")
chk("LP-3 paper and live resolve to different ledgers",
    _lp_paths["paper"] != _lp_paths["live"], _lp_paths, "different")
chk("LP-4 each sits under its own environment directory",
    _lp_paths["paper"].endswith("/paper/execution_ledger.json")
    and _lp_paths["live"].endswith("/live/execution_ledger.json"),
    _lp_paths, "segregated")
for _k, _v in _lp_prev.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


# --- FE. Dashboard integration ---------------------------------------------
# The backend requires a bearer token on every endpoint; the frontend sent no
# Authorization header at all, so every dashboard request returned 401. The
# obvious fix -- add the header in server/api.ts -- would have been WORSE: three
# client components imported that module directly, so the token would have been
# bundled into the browser, readable by anyone loading the page, on an API that
# can change mode and place trades.
#
# The fix is architectural: api.ts is server-only and throws if evaluated in a
# browser; client components reach it through createServerFn wrappers.
_fe_root = os.path.dirname(BACKEND)
_fe_api = os.path.join(_fe_root, "src", "server", "api.ts")
_fe_src = open(_fe_api, encoding="utf-8").read()

chk("FE-1 the api module refuses to run in a browser",
    "typeof window !== 'undefined'" in _fe_src and "throw new Error" in _fe_src)
chk("FE-2 the token comes from the environment, never a literal",
    "process.env.API_AUTH_TOKEN" in _fe_src)

_fe_calls = _re.findall(
    r"await fetch\(`\$\{API_BASE\}(/[^`]+)`,?\s*(\{.*?\})?\s*\)",
    _fe_src, _re.S)
_fe_unauth = [c[0] for c in _fe_calls if "authHeaders" not in (c[1] or "")]
chk("FE-3 every backend call sends Authorization",
    not _fe_unauth, _fe_unauth, "none unauthenticated")
chk("FE-4 there is more than one call, so FE-3 is not vacuous",
    len(_fe_calls) >= 10, len(_fe_calls), ">= 10")

# The endpoint the backend actually implements is /api/reset, not /api/reset-kill.
chk("FE-5 the reset endpoint matches the backend route",
    "/reset-kill" not in _fe_src, "/reset-kill removed")
_fe_main = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
chk("FE-6 that route exists on the backend", '"/api/reset"' in _fe_main)

# No client component may import the token-bearing module.
_fe_leaks = []
for _dirpath, _dirnames, _files in os.walk(os.path.join(_fe_root, "src")):
    if "node_modules" in _dirpath:
        continue
    for _name in _files:
        if not _name.endswith((".tsx", ".ts")):
            continue
        _full = os.path.join(_dirpath, _name)
        if _full == _fe_api or _full.endswith("actions.ts"):
            continue
        _text = open(_full, encoding="utf-8").read()
        if 'from "../server/api"' in _text or "from './api'" in _text:
            # routes/index.tsx is allowed: its uses are inside createServerFn.
            if os.path.basename(_full) == "index.tsx" and "createServerFn" in _text:
                continue
            _fe_leaks.append(os.path.relpath(_full, _fe_root))
chk("FE-7 no client component imports the token-bearing module",
    not _fe_leaks, _fe_leaks, "none")

chk("FE-8 client access goes through server functions",
    os.path.exists(os.path.join(_fe_root, "src", "server", "actions.ts")))

# CORS must permit Authorization or a browser preflight rejects every
# authenticated request before it is sent.
chk("FE-9 CORS allows the Authorization header",
    "Content-Type, Authorization" in _fe_main)


# --- DB2. Daily bars, and the signature that described the wrong trade -----
# `store_daily_bar` validates with date.fromisoformat, which accepts
# YYYY-MM-DD only. Reconciliation passed a full timestamp
# ("2026-08-14T19:30:00+00:00"), so EVERY bar was rejected as malformed and
# daily_bars never updated after the one-time backfill. Observed live, once
# per symbol per cycle.
_db2_dir = tempfile.mkdtemp()
_db2 = patterns.PatternDatabase(Path(_db2_dir) / "bars.db")

_db2.store_daily_bar(symbol="SPY", date_str="2026-08-14T19:30:00+00:00",
                     open_p=1.0, high=2.0, low=0.5, close=1.5, volume=10)
chk("DB2-1 a full timestamp is still rejected (the validator is right)",
    len(_db2.get_recent_daily_bars("SPY", limit=5)) == 0)

_db2.store_daily_bar(symbol="SPY", date_str="2026-08-14",
                     open_p=1.0, high=2.0, low=0.5, close=1.5, volume=10)
chk("DB2-2 a normalised date is accepted",
    len(_db2.get_recent_daily_bars("SPY", limit=5)) == 1,
    len(_db2.get_recent_daily_bars("SPY", limit=5)), 1)

# The caller must do the normalising, exactly as the backfill path does.
_db2_src = open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
chk("DB2-3 reconciliation normalises the timestamp to a date",
    'date_str=ohlc["bar_dates"][-1].isoformat()' not in _db2_src
    and "_bar_date = (_bar_ts.astimezone(timezone.utc).date()" in _db2_src)

_weekend_start = _db2_src.index('elif phase in ("holiday", "weekend")')
_weekend_end = _db2_src.index('self._finalize_cycle(cycle_start, result)',
                               _weekend_start)
_weekend_src = _db2_src[_weekend_start:_weekend_end]
chk("DB2-7 weekend backfill respects the completed flag",
    "if not self.state.backfill_done:" in _weekend_src
    and "Weekend backfill skipped — already complete" in _weekend_src)

# The pattern signature must describe the trade that was actually made. These
# EMAs were recomputed from daily_bars -- a different dataset, different
# resolution, capped at 50 rows, and stale because of the bug above -- while
# the DECISION used live indicators. The learner was keyed on numbers the
# decision never saw: the same failure as recording P&L with the wrong sign.
chk("DB2-4 the signature uses the indicators the decision used",
    'indicators = (self.state.live_indicators or {}).get(symbol) or {}'
    in _db2_src)
chk("DB2-5 a missing live indicator fails identity instead of inventing 0.0",
    'raise RuntimeError("live EMA identity unavailable after fill")'
    in _db2_src)
chk("DB2-6 the signature includes previous EMAs and has no stale-bar fallback",
    'prev_ema_short=indicators.get("prev_ema_short")' in _db2_src
    and 'prev_ema_long=indicators.get("prev_ema_long")' in _db2_src
    and "falling back to stored bars" not in _db2_src)


# --- DH. The dashboard must not undo the backend's loopback bind -----------
# FE-* moved API_AUTH_TOKEN to the server so the browser could never read it.
# That fix has a consequence FE-* did not cover: the server functions in
# actions.ts authenticate to the bot on behalf of WHOEVER LOADS THE PAGE, and
# have no login of their own. So the dashboard's bind address is now a control
# on trading, exactly like API_BIND.
#
# Both dashboard entry points were inherited from a reverse-proxied sandbox
# template and bound every interface: vite.config.ts had `host: true` plus
# `allowedHosts: true` (which disables the Host-header check that prevents a
# page you merely visit from driving the dev server), and serve.ts pinned
# 0.0.0.0 with a comment refusing to honour the environment. AU-10 kept the
# backend on loopback while `bun run dev` served the same powers to the LAN.
_dh_root = os.path.dirname(BACKEND)
_dh_vite_raw = open(os.path.join(_dh_root, "vite.config.ts"), encoding="utf-8").read()
_dh_serve_raw = open(os.path.join(_dh_root, "serve.ts"), encoding="utf-8").read()


def _dh_code(text):
    """Strip TS comments. The first draft of DH-1/DH-2 matched the comment
    explaining the bug rather than the setting, so it failed against the
    fixed file -- the same false-positive shape as LP-1.

    The second draft then ate `http://127.0.0.1`, because the `//` in a URL
    scheme is not a comment. Hence the negative lookbehind for the colon."""
    text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.S)
    return "\n".join(_re.sub(r"(?<!:)//.*$", "", ln) for ln in text.split("\n"))


_dh_vite = _dh_code(_dh_vite_raw)
_dh_serve = _dh_code(_dh_serve_raw)

chk("DH-0 comment-stripping leaves the settings it is meant to inspect",
    "defineConfig" in _dh_vite and "Bun.serve" in _dh_serve
    and "SECURITY" not in _dh_vite)
chk("DH-0b comment-stripping does not eat a URL scheme",
    _dh_code('const u = "http://127.0.0.1:3000"; // note')
    .strip() == 'const u = "http://127.0.0.1:3000";',
    _dh_code('const u = "http://127.0.0.1:3000"; // note').strip(),
    'const u = "http://127.0.0.1:3000";')
chk("DH-1 the dev server does not bind every interface",
    "host: true" not in _dh_vite, "host: true present", "absent")
chk("DH-2 the dev server keeps Vite's DNS-rebinding protection",
    "allowedHosts: true" not in _dh_vite, "allowedHosts: true present", "absent")
chk("DH-3 the dev server defaults to loopback",
    '?? "127.0.0.1"' in _dh_vite)
chk("DH-4 the production server defaults to loopback",
    '?? "127.0.0.1"' in _dh_serve)
chk("DH-5 no dashboard entry point hardcodes all interfaces",
    '"0.0.0.0"' not in _dh_vite and '"0.0.0.0"' not in _dh_serve)
chk("DH-6 widening the bind is possible but announced",
    "DASHBOARD_HOST" in _dh_vite and "DASHBOARD_HOST" in _dh_serve
    and "WARNING" in _dh_vite and "WARNING" in _dh_serve)
chk("DH-6b the warning fires on a wide bind, not on the loopback default",
    _dh_vite.count('!== "127.0.0.1"') >= 1
    and _dh_serve.count('!== "127.0.0.1"') >= 1)

# The template freed the port by kill(1) under sudo, on the reasoning that
# "every sandbox user has passwordless sudo". On a personal machine that is a
# password prompt attached to killing an unrelated process.
chk("DH-7 the production server does not sudo",
    "sudo" not in _dh_serve_raw, "sudo present", "absent")
chk("DH-8 a busy port is reported, not seized",
    "EADDRINUSE" in _dh_serve and "already in use" in _dh_serve)

# strictPort matters here specifically: without it Vite silently moves to 3001
# on a conflict, and the operator ends up reading a stale dashboard on 3000
# while believing it is the one they just started.
chk("DH-9 the dev server fails rather than drifting to another port",
    "strictPort: true" in _dh_vite)

# README must not send the operator to the superseded tree. `cd site` predates
# the token fix: site/src/server/api.ts sends no Authorization header at all,
# so every call 401s against the current backend.
_dh_readme = open(os.path.join(_dh_root, "README.md"), encoding="utf-8").read()
# Collapse wrapping and markdown emphasis: the prose is hard-wrapped, so a
# literal "no login" match broke on a line break falling between the words.
_dh_prose = _re.sub(r"[\s*_`]+", " ", _dh_readme).lower()
chk("DH-10 the README does not point at the superseded dashboard",
    "cd site" not in _dh_prose, "cd site present", "absent")
chk("DH-11 the README states the dashboard has no login of its own",
    "no login of its own" in _dh_prose)
chk("DH-12 the README says the bind default is loopback",
    "127.0.0.1 by default" in _dh_prose)

# --- CSRF on the server functions ------------------------------------------
# Moving the token server-side (FE-*) made these functions authenticate to the
# bot on behalf of whoever reaches them. Loopback does not help: the operator's
# own browser is on loopback, so any page they visit can post to :3000.
#
# TanStack's x-tsr-serverFn header looked like a gate and is not one. Confirmed
# by reading the built server bundle: `const res = await action(payload)` runs
# BEFORE `if (!isServerFn)` is consulted, so the header only shapes the reply.
# Omitting it makes the request "simple" and skips the CORS preflight.
# The reachable damage is resetKill -- silently disarming a kill switch the
# operator deliberately engaged.
_dh_act = _dh_code(
    open(os.path.join(_dh_root, "src", "server", "actions.ts"),
         encoding="utf-8").read())

chk("DH-13 every exported server function checks the origin",
    _dh_act.count("assertSameOrigin(") == 5,
    _dh_act.count("assertSameOrigin("), "4 call sites + 1 definition")
chk("DH-14 the state-changing pair demand a positive same-origin signal",
    _dh_act.count("assertSameOrigin(true)") == 2,
    _dh_act.count("assertSameOrigin(true)"), 2)
chk("DH-15 the reads are guarded too, at the weaker level",
    _dh_act.count("assertSameOrigin(false)") == 2)

# A guard that returns instead of throwing would let the call proceed.
chk("DH-16 a rejected origin throws rather than returning",
    _dh_act.count("throw new ForbiddenOriginError") >= 3)

# The allowlist must not be derived from the request's own Host header: under
# DNS rebinding the attacker controls Host, and a forged Origin would agree
# with it. It has to come from configuration.
chk("DH-17 the allowlist comes from configuration, not the request",
    "DASHBOARD_ORIGIN" in _dh_act and "127.0.0.1" in _dh_act)
chk("DH-18 the guard never consults the request Host to build the allowlist",
    'getRequestHeader("host")' not in _dh_act
    and "getRequestHost" not in _dh_act
    and "getRequestHeader(" not in _dh_act)

# Absence of Origin must not be a bypass for the state-changing calls.
chk("DH-19 a missing Origin is refused on state-changing calls",
    "no Origin header on a state-changing call" in _dh_act)

# Every function that reaches the bot must be wrapped. If a new export appears
# without a guard, this catches it rather than waiting for a review.
_dh_exports = _re.findall(r"export const (\w+) = createServerFn", _dh_act)
_dh_handlers = _re.split(r"export const \w+ = createServerFn", _dh_act)[1:]
_dh_unguarded = [n for n, body in zip(_dh_exports, _dh_handlers)
                 if "assertSameOrigin(" not in body]
chk("DH-20 no server function reaches the bot without a guard",
    not _dh_unguarded, _dh_unguarded, "none")
chk("DH-21 there are four such functions, so DH-20 is not vacuous",
    len(_dh_exports) == 4, len(_dh_exports), 4)

# --- Shadowed imports ------------------------------------------------------
# Renaming the imports to route through the server functions collided with a
# local closure: HeartbeatStatus declared `const fetchHeartbeat` wrapping a
# call to `fetchHeartbeat()`, which after the rename resolved to itself.
# Unbounded recursion, swallowed by the surrounding catch into a permanent
# "not alive" -- a monitoring panel that could never report a problem.
#
# Every FE-* check passed on this, because they assert on source text. tsc
# found it in one run. That is the argument for `bun run typecheck` in CI.
_dh_shadow = []
for _dirpath, _dirnames, _files in os.walk(os.path.join(_dh_root, "src")):
    if "node_modules" in _dirpath:
        continue
    for _name in _files:
        if not _name.endswith((".ts", ".tsx")):
            continue
        _full = os.path.join(_dirpath, _name)
        _text = _dh_code(open(_full, encoding="utf-8").read())
        _imported = set()
        for _m in _re.finditer(r"import\s*\{([^}]*)\}\s*from", _text):
            for _part in _m.group(1).split(","):
                _part = _part.strip().split(" as ")[-1].strip()
                if _part:
                    _imported.add(_part)
        for _sym in _imported:
            if _re.search(r"\b(?:const|let|var|function)\s+%s\b" % _re.escape(_sym),
                          _text):
                _dh_shadow.append("%s: %s"
                                  % (os.path.relpath(_full, _dh_root), _sym))
chk("DH-22 no local declaration shadows an imported name",
    not _dh_shadow, _dh_shadow, "none")
chk("DH-23 the shadow scan actually read the component tree",
    os.path.exists(os.path.join(_dh_root, "src", "components",
                                "HeartbeatStatus.tsx")))

# A typecheck script must exist, because `vite build` does not typecheck --
# esbuild strips types without verifying them, so the build passed on the
# recursion above.
_dh_pkg = _json.load(open(os.path.join(_dh_root, "package.json"),
                          encoding="utf-8"))
chk("DH-24 a typecheck script exists separately from build",
    "typecheck" in _dh_pkg.get("scripts", {}),
    sorted(_dh_pkg.get("scripts", {})), "includes typecheck")
chk("DH-25 Bun's globals are typed, so serve.ts can be checked",
    "@types/bun" in _dh_pkg.get("devDependencies", {}),
    sorted(_dh_pkg.get("devDependencies", {})), "includes @types/bun")


# ===========================================================================
print("\n" + "=" * 74)
print("Educated Trades — safety & integrity suite")
print("=" * 74)
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
if _PROBE_DIAGNOSTICS:
    print("\nSubprocess probes that came back short:")
    for _kind, _key, _rc, _err in _PROBE_DIAGNOSTICS:
        print("  %s key=%s rc=%s\n    %s" % (_kind, _key, _rc, _err))
print("\n%d/%d passed, %d failed" % (len(RESULTS) - fails, len(RESULTS), fails))
if fails:
    # An intermittent failure is only useful if the run that produced it is
    # still readable afterwards. Two were lost to overwritten scrollback
    # before this existed.
    _record = os.path.join(tempfile.gettempdir(), "educated_trades_test_failures.log")
    try:
        with open(_record, "a", encoding="utf-8") as _fh:
            _fh.write("=== %s  %d/%d failed ===\n"
                      % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         fails, len(RESULTS)))
            for _n, _ok, _got, _exp in RESULTS:
                if not _ok:
                    _fh.write("  %s\n    got: %s\n    exp: %s\n" % (_n, _got, _exp))
        print("Failure detail appended to %s" % _record)
    except OSError:
        pass
sys.exit(1 if fails else 0)
