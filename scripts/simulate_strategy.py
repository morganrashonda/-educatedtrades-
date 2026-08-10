"""End-to-end simulation of the live signal logic on synthetic 30-min bars.

The data is a random walk with mild autocorrelation and NO exploitable edge.
So the honest expectation is:
    gross (no costs)  ~ 0
    net  (with costs) ~ -(cost per round trip x trades)
A materially positive gross result would mean look-ahead bias, not skill.
"""
import sys, math, time
import numpy as np
sys.path.insert(0, '.')
from patterns import (compute_ema, compute_adx, compute_rsi,
                      realized_volatility_pct, trend_conviction,
                      mean_reversion_conviction, classify_regime,
                      ADX_TREND_MIN, ADX_RANGE_MAX)

BARS_PER_DAY   = 13
SESSIONS       = 250
SIGMA          = 0.0028          # ~1% daily vol over 13 bars
STOP           = 0.00693         # scaled 30-min stop
TARGET         = 0.00832
COST_RT        = 0.0003
STOP_SLIP      = 0.0002   # slippage between touch and fill          # 0.03% per round trip
MAX_HOLD_BARS  = 26              # two sessions
MIN_CONV       = 0.20
WINDOW         = 90
AUTOCORR       = float(sys.argv[2]) if len(sys.argv) > 2 else 0.12
PATHS          = int(sys.argv[1]) if len(sys.argv) > 1 else 24

def make_path(rng):
    n = BARS_PER_DAY * SESSIONS
    r = np.zeros(n); e = rng.normal(0, SIGMA, n)
    for i in range(1, n):
        r[i] = AUTOCORR * r[i-1] + e[i]
    px = 100 * np.exp(np.cumsum(r))
    hi = px * (1 + np.abs(rng.normal(0, SIGMA*0.4, n)))
    lo = px * (1 - np.abs(rng.normal(0, SIGMA*0.4, n)))
    return px, hi, lo

def run_path(px, hi, lo, apply_costs):
    trades = []
    pos = None
    for i in range(WINDOW, len(px)):
        c = px[:i]; h = hi[:i]; l = lo[:i]
        price = px[i-1]
        if pos is not None:
            held = i - pos['i']
            move = (price - pos['entry']) / pos['entry']
            signed = move if pos['side'] == 'buy' else -move
            # Exits are INTRABAR: a stop triggers the moment price touches it,
            # not at the close. Checking only closes either truncates the loss
            # (if you credit the stop price) or exaggerates it (if you credit
            # the close). Use the bar's extremes, and check the stop FIRST --
            # when a bar spans both levels, assume the worse one.
            bar_hi = (hi[i-1] - pos['entry']) / pos['entry']
            bar_lo = (lo[i-1] - pos['entry']) / pos['entry']
            adverse    = bar_lo if pos['side'] == 'buy' else -bar_hi
            favourable = bar_hi if pos['side'] == 'buy' else -bar_lo
            hit_stop   = adverse <= -STOP
            hit_target = (not hit_stop) and favourable >= TARGET
            if hit_stop or hit_target or held >= MAX_HOLD_BARS:
                # A stop does NOT reliably fill at the stop price: it becomes
                # a market order once touched, so a bar that closes beyond the
                # stop fills near the close. Crediting exactly -STOP truncates
                # every overshoot and manufactures drift out of nothing -- the
                # single most common way a backtest flatters itself.
                if hit_stop:
                    # Market order once touched: stop price plus slippage.
                    ret = -STOP - STOP_SLIP
                elif hit_target:
                    ret = TARGET                  # limit order: fills at target
                else:
                    ret = signed
                if apply_costs: ret -= COST_RT
                trades.append({'ret': ret, 'reason':
                               'stop' if hit_stop else 'target' if hit_target else 'time',
                               'strategy': pos['strategy']})
                pos = None
            continue
        w = c[-WINDOW:]
        es, el = compute_ema(w, 20), compute_ema(w, 50)
        if es is None or el is None: continue
        adx = compute_adx(h[-WINDOW:].tolist(), l[-WINDOW:].tolist(), w.tolist())
        if adx is None: continue
        rsi = compute_rsi(w.tolist())
        if rsi is None: continue
        vol = realized_volatility_pct(w.tolist())
        regime = classify_regime(adx)
        if regime == 'range':
            conv = mean_reversion_conviction(rsi)
            strategy = 'mean_reversion'
            if abs(rsi - 50) < 20: conv = 0.0
        else:
            conv = trend_conviction(adx, es, el, vol)
            strategy = 'trend_following'
        if abs(conv) < MIN_CONV: continue
        pos = {'i': i, 'entry': price, 'side': 'buy' if conv > 0 else 'sell',
               'strategy': strategy}
    return trades

def summarise(all_trades, label):
    if not all_trades:
        print('%-22s no trades' % label); return
    rets = np.array([t['ret'] for t in all_trades])
    wins = (rets > 0).sum()
    eq = np.cumprod(1 + rets); peak = np.maximum.accumulate(eq)
    dd = ((peak - eq) / peak).max() * 100
    reasons = {}
    for t in all_trades: reasons[t['reason']] = reasons.get(t['reason'], 0) + 1
    print('%-22s trades %5d | win %5.1f%% | mean/trade %+7.4f%% | total %+8.2f%% | maxDD %5.1f%% | %s'
          % (label, len(rets), wins/len(rets)*100, rets.mean()*100,
             (eq[-1]-1)*100, dd, reasons))

t0 = time.time()
rng = np.random.default_rng(20260810)
gross, net = [], []
for p in range(PATHS):
    px, hi, lo = make_path(rng)
    gross += run_path(px, hi, lo, apply_costs=False)
    net   += run_path(px, hi, lo, apply_costs=True)
    if (p+1) % 4 == 0:
        print('  ... %d/%d paths (%.0fs)' % (p+1, PATHS, time.time()-t0), flush=True)

print()
print('=' * 108)
print('SIMULATION — %d independent years of 30-min bars, random walk, NO real edge' % PATHS)
print('=' * 108)
summarise(gross, 'GROSS (no costs)')
summarise(net,   'NET (0.03%/round trip)')
print()
tr = len(gross) / PATHS / SESSIONS
print('trades per symbol per day : %.2f   (x3 symbols = %.1f/day)' % (tr, tr*3))
for strat in ('trend_following', 'mean_reversion'):
    sub = [t['ret'] for t in gross if t['strategy'] == strat]
    if sub:
        print('  %-16s %5d trades, mean %+0.4f%%' % (strat, len(sub), np.mean(sub)*100))
print()
print('elapsed %.0fs' % (time.time()-t0))
