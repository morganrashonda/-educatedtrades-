"""
Honest Out-of-Sample Backtest & Strategy Research Engine for Educated Trades.

Goal (owner priority): a realistic, no-shortcuts backtest of the current strategy
and 4 alternative strategies on REAL historical daily data, with a strict 70/30
train/test split so we measure out-of-sample performance — not curve-fit in-sample fantasy.

Data: daily OHLCV for SPY, QQQ, IWM from Alpaca's historical API. ~3.5 years.

Strategies Replayed:
  - Variant A (price-only): original technical-only strategy (ADX, RSI MR, EMA Trend + Pattern memory)
  - Variant B (hybrid): original strategy with price-momentum sentiment proxy
  - Strategy C (mean_revert_long): Pure mean-reversion long-only strategy (optimized in-sample)
  - Strategy D (sma_200_trend): 200-day SMA trend filter (optimized in-sample)
  - Strategy E (ma_crossover): Dual Moving Average crossover (optimized in-sample)
  - Strategy F (volatility_breakout): 20-day breakout with volume and ATR-based stops (constant parameter)

Risk model: hard stop-loss, take-profit, checked intraday against high/low, and max hold days.
For Strategy F, stops are ATR-based. For all others, stops are percentage-based (-2.5% SL, +3% TP, 15 days).

No look-ahead: on day t we decide using ONLY bars[0..t]; we ENTER at day t+1's open.
"""

import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patterns as P  # reuse the REAL indicator implementations

SYMBOLS = ["SPY", "QQQ", "IWM"]
START = datetime(2022, 1, 1)
END = datetime(2025, 6, 30)

# Risk model (mirrors production defaults)
STOP_LOSS_PCT = 0.025
TAKE_PROFIT_PCT = 0.03

# Regime / signal params
ADX_PERIOD = 14
RSI_PERIOD = 14
EMA_FAST, EMA_SLOW = 12, 26
MOMENTUM_LOOKBACK = 10          # trading days for momentum/sentiment proxy
MIN_HISTORY = 40               # bars needed before we trust indicators
RISK_PER_TRADE = 0.005         # 0.5% of equity notional sizing (flat fractional)
STARTING_EQUITY = 100_000.0
MAX_HOLD_DAYS = 15             # time-stop so trades don't linger forever
TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@dataclass
class Bar:
    d: date
    o: float
    h: float
    l: float
    c: float
    v: float


def load_alpaca(symbol: str) -> List[Bar]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    key = os.environ.get("APCA_API_KEY_ID", "")
    secret = os.environ.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("No Alpaca credentials")
    client = StockHistoricalDataClient(key, secret)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol], timeframe=TimeFrame.Day, start=START, end=END,
    )
    resp = client.get_stock_bars(req)
    data = resp.data.get(symbol, [])
    return [
        Bar(b.timestamp.date(), float(b.open), float(b.high), float(b.low),
            float(b.close), float(b.volume))
        for b in data
    ]


def load_stooq(symbol: str) -> List[Bar]:
    """Fallback: daily CSV from stooq.com (no key needed)."""
    import urllib.request
    url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d"
    with urllib.request.urlopen(url, timeout=30) as r:
        text = r.read().decode()
    bars: List[Bar] = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            d = datetime.strptime(parts[0], "%Y-%m-%d").date()
            if not (START.date() <= d <= END.date()):
                continue
            bars.append(Bar(d, float(parts[1]), float(parts[2]),
                            float(parts[3]), float(parts[4]), float(parts[5] or 0)))
        except ValueError:
            continue
    return bars


def load_data(symbol: str) -> List[Bar]:
    try:
        bars = load_alpaca(symbol)
        if len(bars) >= 200:
            return bars
    except Exception as e:
        print(f"  Alpaca load failed for {symbol}: {e}; trying Stooq…")
    return load_stooq(symbol)


# ---------------------------------------------------------------------------
# ATR Helper
# ---------------------------------------------------------------------------
def compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < period + 1:
        return None
    tr = []
    for i in range(1, n):
        tr_val = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr.append(tr_val)
    return float(np.mean(tr[-period:]))


# ---------------------------------------------------------------------------
# Trade model
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    symbol: str
    side: str            # "long" | "short"
    entry_date: date
    entry_price: float
    qty: float
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    reason: str = ""
    regime: str = ""
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        if self.side == "long":
            return (self.exit_price - self.entry_price) * self.qty
        return (self.entry_price - self.exit_price) * self.qty

    @property
    def ret_pct(self) -> float:
        if self.exit_price is None or self.entry_price == 0:
            return 0.0
        if self.side == "long":
            return (self.exit_price - self.entry_price) / self.entry_price
        return (self.entry_price - self.exit_price) / self.entry_price

    @property
    def hold_days(self) -> int:
        if self.exit_date is None:
            return 0
        return (self.exit_date - self.entry_date).days


# ---------------------------------------------------------------------------
# Signal generation (no look-ahead: only uses closes[:i+1])
# ---------------------------------------------------------------------------
def decide_signal(bars: List[Bar], i: int, strategy: str,
                  pattern_stats: Dict, config: Dict = None) -> Optional[dict]:
    """
    Decide a signal using ONLY information available at the close of bar i.
    Returns dict(side, regime, size_factor, reason, atr) or None.
    """
    if i < MIN_HISTORY:
        return None
    if config is None:
        config = {}

    window = bars[:i + 1]
    highs = [b.h for b in window]
    lows = [b.l for b in window]
    closes = [b.c for b in window]
    volumes = [b.v for b in window]

    # Precalculate baseline indicators
    adx = P.compute_adx(highs, lows, closes, ADX_PERIOD)
    regime = P.classify_regime(adx)
    rsi = P.compute_rsi(closes, RSI_PERIOD)
    ema_fast = P.compute_ema(closes, EMA_FAST)
    ema_slow = P.compute_ema(closes, EMA_SLOW)

    side = None
    reason = ""
    size_factor = 1.0
    atr = None

    # Strategy Option 1: Variant A (price-only)
    if strategy == "A_price_only":
        if rsi is None or ema_fast is None or ema_slow is None:
            return None
        
        if regime == "range_bound":
            if rsi <= 30:
                side, reason = "long", f"mean_revert oversold rsi={rsi:.0f}"
            elif rsi >= 70:
                side, reason = "short", f"mean_revert overbought rsi={rsi:.0f}"
        else:
            trend_up = ema_fast > ema_slow
            if trend_up:
                side, reason = "long", f"trend up ema {ema_fast:.1f}>{ema_slow:.1f}"
            else:
                side, reason = "short", f"trend down ema {ema_fast:.1f}<{ema_slow:.1f}"
            if regime == "transitioning":
                size_factor = 0.5

        if side is None:
            return None

        # Pattern-memory gate (learned on TRAIN only): skip setups with win rate <40%
        if pattern_stats:
            sig_key = (regime, side)
            st = pattern_stats.get(sig_key)
            if st and st["n"] >= 10 and st["wins"] / st["n"] < 0.40:
                return None

        return {"side": side, "regime": regime, "size_factor": size_factor, "reason": reason}

    # Strategy Option 2: Variant B (hybrid with momentum sentiment proxy)
    elif strategy == "B_hybrid":
        if rsi is None or ema_fast is None or ema_slow is None:
            return None
        
        mom = (closes[-1] - closes[-1 - MOMENTUM_LOOKBACK]) / closes[-1 - MOMENTUM_LOOKBACK]
        sentiment = max(-1.0, min(1.0, mom * 10.0))

        if regime == "range_bound":
            if rsi <= 30:
                side, reason = "long", f"mean_revert oversold rsi={rsi:.0f}"
            elif rsi >= 70:
                side, reason = "short", f"mean_revert overbought rsi={rsi:.0f}"
        else:
            trend_up = ema_fast > ema_slow
            conviction = (1.0 if trend_up else -1.0) * 0.5 + sentiment * 0.5
            if conviction >= 0.3:
                side, reason = "long", f"trend up ema {ema_fast:.1f}>{ema_slow:.1f} sent={sentiment:+.2f}"
            elif conviction <= -0.3:
                side, reason = "short", f"trend down ema {ema_fast:.1f}<{ema_slow:.1f} sent={sentiment:+.2f}"
            if regime == "transitioning":
                size_factor = 0.5

        if side is None:
            return None

        # Pattern-memory gate (learned on TRAIN only): skip setups with win rate <40%
        if pattern_stats:
            sig_key = (regime, side)
            st = pattern_stats.get(sig_key)
            if st and st["n"] >= 10 and st["wins"] / st["n"] < 0.40:
                return None

        return {"side": side, "regime": regime, "size_factor": size_factor, "reason": reason}

    # Strategy Option 3: Strategy C (Pure mean reversion long-only)
    elif strategy == "C_mean_revert_long":
        rsi_threshold = config.get("rsi_threshold", 30)
        if rsi is None:
            return None
        
        # Only trade Range Bound markets, and strictly go Long on oversold RSI
        if regime == "range_bound" and rsi <= rsi_threshold:
            side, reason = "long", f"MR long rsi={rsi:.1f} (thresh={rsi_threshold})"
            return {"side": side, "regime": regime, "size_factor": 1.0, "reason": reason}
        return None

    # Strategy Option 4: Strategy D (200-day SMA Trend Filter)
    elif strategy == "D_sma_200_trend":
        use_regime_filter = config.get("use_regime_filter", False)
        if len(closes) < 200:
            return None
        
        sma_200 = float(np.mean(closes[-200:]))
        
        # If using regime filter, restrict trading to trending or transitioning markets
        if use_regime_filter and regime == "range_bound":
            return None

        if closes[-1] > sma_200:
            side, reason = "long", f"price > 200-day SMA ({sma_200:.1f})"
            return {"side": side, "regime": regime, "size_factor": 1.0, "reason": reason}
        elif closes[-1] < sma_200:
            side, reason = "short", f"price < 200-day SMA ({sma_200:.1f})"
            return {"side": side, "regime": regime, "size_factor": 1.0, "reason": reason}
        return None

    # Strategy Option 5: Strategy E (Dual MA Crossover)
    elif strategy == "E_ema_crossover":
        ma_type = config.get("ma_type", "EMA")
        fast_period = config.get("fast_period", 20)
        slow_period = config.get("slow_period", 50)
        use_regime_filter = config.get("use_regime_filter", False)

        if len(closes) < slow_period:
            return None

        # Compute moving averages
        if ma_type == "EMA":
            fast_ma = P.compute_ema(closes, fast_period)
            slow_ma = P.compute_ema(closes, slow_period)
        else:
            fast_ma = float(np.mean(closes[-fast_period:]))
            slow_ma = float(np.mean(closes[-slow_period:]))

        if fast_ma is None or slow_ma is None:
            return None

        # If using regime filter, skip range bound markets
        if use_regime_filter and regime == "range_bound":
            return None

        if fast_ma > slow_ma:
            side, reason = "long", f"{ma_type} cross fast={fast_ma:.1f} > slow={slow_ma:.1f}"
            return {"side": side, "regime": regime, "size_factor": 1.0, "reason": reason}
        elif fast_ma < slow_ma:
            side, reason = "short", f"{ma_type} cross fast={fast_ma:.1f} < slow={slow_ma:.1f}"
            return {"side": side, "regime": regime, "size_factor": 1.0, "reason": reason}
        return None

    # Strategy Option 6: Strategy F (Volatility breakout with ATR-based stops)
    elif strategy == "F_volatility_breakout":
        lookback = 20
        if len(closes) < lookback + 1:
            return None

        recent_high = max(highs[-lookback-1:-1])
        recent_low = min(lows[-lookback-1:-1])
        avg_vol = float(np.mean(volumes[-lookback-1:-1]))
        
        atr = compute_atr(highs, lows, closes, 14)
        if atr is None:
            return None

        # Breakout above 20-day high with above-average volume
        if closes[-1] > recent_high and volumes[-1] > avg_vol:
            side, reason = "long", f"breakout > 20-day high ({recent_high:.1f})"
            return {"side": side, "regime": regime, "size_factor": 1.0,
                    "reason": reason, "adx": adx, "rsi": rsi, "atr": atr}
        # Breakout below 20-day low with above-average volume
        elif closes[-1] < recent_low and volumes[-1] > avg_vol:
            side, reason = "short", f"breakout < 20-day low ({recent_low:.1f})"
            return {"side": side, "regime": regime, "size_factor": 1.0,
                    "reason": reason, "adx": adx, "rsi": rsi, "atr": atr}
        return None

    return None


# ---------------------------------------------------------------------------
# Pattern-memory learning (TRAIN only) — win rate per (regime, side)
# ---------------------------------------------------------------------------
def learn_pattern_stats(bars: List[Bar], strategy: str) -> Dict:
    """
    Walk the TRAIN slice and tally the realised win/loss of each (regime, side)
    setup under the same entry/exit rules. Used only to gate the TEST period.
    """
    stats: Dict = {}
    i = MIN_HISTORY
    n = len(bars)
    while i < n - 1:
        sig = decide_signal(bars, i, strategy, {}, {})
        if not sig:
            i += 1
            continue
        entry = bars[i + 1].o
        outcome = _simulate_exit(bars, i + 1, sig["side"], entry)
        key = (sig["regime"], sig["side"])
        s = stats.setdefault(key, {"n": 0, "wins": 0})
        s["n"] += 1
        if outcome["ret"] > 0:
            s["wins"] += 1
        i = outcome["exit_idx"] + 1  # no overlapping trades per symbol
    return stats


def _simulate_exit(bars: List[Bar], entry_idx: int, side: str,
                   entry_price: float) -> dict:
    """Simulate SL/TP/time-stop exit from entry_idx forward."""
    for j in range(entry_idx, min(entry_idx + MAX_HOLD_DAYS + 1, len(bars))):
        b = bars[j]
        if side == "long":
            if b.l <= entry_price * (1 - STOP_LOSS_PCT):
                return {"exit_idx": j, "price": entry_price * (1 - STOP_LOSS_PCT),
                        "ret": -STOP_LOSS_PCT, "reason": "stop"}
            if b.h >= entry_price * (1 + TAKE_PROFIT_PCT):
                return {"exit_idx": j, "price": entry_price * (1 + TAKE_PROFIT_PCT),
                        "ret": TAKE_PROFIT_PCT, "reason": "target"}
        else:
            if b.h >= entry_price * (1 + STOP_LOSS_PCT):
                return {"exit_idx": j, "price": entry_price * (1 + STOP_LOSS_PCT),
                        "ret": -STOP_LOSS_PCT, "reason": "stop"}
            if b.l <= entry_price * (1 - TAKE_PROFIT_PCT):
                return {"exit_idx": j, "price": entry_price * (1 - TAKE_PROFIT_PCT),
                        "ret": TAKE_PROFIT_PCT, "reason": "target"}
    # Time stop at close.
    j = min(entry_idx + MAX_HOLD_DAYS, len(bars) - 1)
    exit_price = bars[j].c
    ret = ((exit_price - entry_price) / entry_price) if side == "long" \
        else ((entry_price - exit_price) / entry_price)
    return {"exit_idx": j, "price": exit_price, "ret": ret, "reason": "time"}


# ---------------------------------------------------------------------------
# Backtest engine (portfolio across symbols, daily marked-to-close equity)
# ---------------------------------------------------------------------------
def run_backtest(data: Dict[str, List[Bar]], test_start: date, test_end: date,
                 strategy: str, pattern_stats: Dict = None, config: Dict = None) -> dict:
    """
    Event-driven backtest over the specified date window. One open position per symbol.
    """
    ref = data["SPY"]
    idx_by_date = {b.d: k for k, b in enumerate(ref)}
    all_dates = [b.d for b in ref if test_start <= b.d <= test_end]

    cash = STARTING_EQUITY
    peak_equity = STARTING_EQUITY
    open_trades: Dict[str, Trade] = {}
    closed: List[Trade] = []
    equity_curve = []

    # Per-symbol index alignment
    sym_idx = {s: {b.d: k for k, b in enumerate(data[s])} for s in data}

    hold_days_limit = config.get("max_hold_days", MAX_HOLD_DAYS) if config else MAX_HOLD_DAYS

    for d in all_dates:
        # 1. Manage open positions (check exits using today's bar).
        for sym in list(open_trades.keys()):
            if d not in sym_idx[sym]:
                continue
            k = sym_idx[sym][d]
            b = data[sym][k]
            tr = open_trades[sym]
            hit = None
            
            # Determine exit thresholds (use custom stop/target prices if provided)
            if tr.side == "long":
                sl_price = tr.stop_loss_price if tr.stop_loss_price is not None else tr.entry_price * (1 - STOP_LOSS_PCT)
                tp_price = tr.take_profit_price if tr.take_profit_price is not None else tr.entry_price * (1 + TAKE_PROFIT_PCT)
                if b.l <= sl_price:
                    hit = (sl_price, "stop")
                elif b.h >= tp_price:
                    hit = (tp_price, "target")
            else:
                sl_price = tr.stop_loss_price if tr.stop_loss_price is not None else tr.entry_price * (1 + STOP_LOSS_PCT)
                tp_price = tr.take_profit_price if tr.take_profit_price is not None else tr.entry_price * (1 - TAKE_PROFIT_PCT)
                if b.h >= sl_price:
                    hit = (sl_price, "stop")
                elif b.l <= tp_price:
                    hit = (tp_price, "target")
                    
            if hit is None and (d - tr.entry_date).days >= hold_days_limit:
                hit = (b.c, "time")
                
            if hit:
                tr.exit_date, tr.exit_price, tr.reason = d, hit[0], hit[1]
                cash += tr.pnl
                closed.append(tr)
                del open_trades[sym]

        # 2. Generate new entries (decide on prior close, enter at today's open).
        for sym in data:
            if sym in open_trades or d not in sym_idx[sym]:
                continue
            k = sym_idx[sym][d]
            if k < 1:
                continue
            # Decide using bars up to YESTERDAY (k-1) — no look-ahead.
            sig = decide_signal(data[sym], k - 1, strategy, pattern_stats, config)
            if not sig:
                continue
            
            entry = data[sym][k].o
            equity_now = cash + sum(_mark(t, data, sym_idx, d) for t in open_trades.values())

            # Track peak equity and drawdown
            peak_equity = max(peak_equity, equity_now)
            drawdown = (equity_now - peak_equity) / peak_equity if peak_equity > 0 else 0.0

            # Drawdown Safety Floors — mirrors the live orchestrator's policy
            # (backend/main.py._finalize_cycle) via the shared constants in
            # patterns.py, so the backtest can't silently drift from what the
            # live bot actually does.
            if drawdown <= -P.DRAWDOWN_KILL_PCT:
                # stop all trading
                continue

            # Halve position size once drawdown crosses the live halving floor
            risk_mult = 0.5 if drawdown <= -P.DRAWDOWN_HALVE_PCT else 1.0
            
            # Portfolio cap assertion
            PORTFOLIO_CAP = 0.15
            assert PORTFOLIO_CAP <= 0.50, f"CRITICAL: Portfolio cap {PORTFOLIO_CAP} cannot exceed 50%!"

            # Flat fractional sizing (no conviction scaling)
            notional = equity_now * RISK_PER_TRADE * risk_mult / STOP_LOSS_PCT
            notional = min(notional, equity_now * PORTFOLIO_CAP)  # 15% cap
            qty = max(0.0, notional / entry)
            if qty <= 0:
                continue
            
            # Setup optional custom ATR stop loss/take profit prices
            sl_price = None
            tp_price = None
            if "atr" in sig and sig["atr"] is not None:
                atr_val = sig["atr"]
                if sig["side"] == "long":
                    sl_price = entry - 2.0 * atr_val
                    tp_price = entry + 3.0 * atr_val
                else:
                    sl_price = entry + 2.0 * atr_val
                    tp_price = entry - 3.0 * atr_val

            open_trades[sym] = Trade(
                symbol=sym, side=sig["side"], entry_date=d, entry_price=entry,
                qty=qty, regime=sig["regime"], reason=sig["reason"],
                stop_loss_price=sl_price, take_profit_price=tp_price,
            )

        # 3. Mark-to-close equity for the day.
        equity = cash + sum(_mark(t, data, sym_idx, d) for t in open_trades.values())
        equity_curve.append({"date": d.isoformat(), "equity": round(equity, 2)})

    # Close any still-open trades at last available close.
    last_d = all_dates[-1]
    for sym, tr in list(open_trades.items()):
        k = sym_idx[sym][last_d]
        tr.exit_date, tr.exit_price, tr.reason = last_d, data[sym][k].c, "eot"
        cash += tr.pnl
        closed.append(tr)

    return compute_metrics(closed, equity_curve, test_start, last_d)


def _mark(tr: Trade, data, sym_idx, d) -> float:
    """Unrealised mark-to-close value of an open trade for equity tracking."""
    if d not in sym_idx[tr.symbol]:
        return 0.0
    px = data[tr.symbol][sym_idx[tr.symbol][d]].c
    if tr.side == "long":
        return (px - tr.entry_price) * tr.qty
    return (tr.entry_price - px) * tr.qty


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_metrics(trades: List[Trade], equity_curve: List[dict],
                    start: date, end: date) -> dict:
    eq = [p["equity"] for p in equity_curve]
    n_days = len(eq)
    total_return = (eq[-1] / eq[0] - 1.0) if eq else 0.0
    years = max((end - start).days / 365.25, 1e-9)
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1 if eq and eq[0] > 0 else 0.0

    # Daily returns for Sharpe/Sortino
    rets = [eq[k] / eq[k - 1] - 1.0 for k in range(1, len(eq))]
    mean_r = sum(rets) / len(rets) if rets else 0.0
    std_r = (sum((r - mean_r) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 0.0
    downside = [r for r in rets if r < 0]
    dstd = (sum(r * r for r in downside) / len(downside)) ** 0.5 if downside else 0.0
    sharpe = (mean_r / std_r * math.sqrt(TRADING_DAYS)) if std_r > 0 else 0.0
    sortino = (mean_r / dstd * math.sqrt(TRADING_DAYS)) if dstd > 0 else 0.0

    # Max drawdown
    peak = -1e18
    max_dd = 0.0
    dd_date = None
    for p in equity_curve:
        peak = max(peak, p["equity"])
        dd = (p["equity"] - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
            dd_date = p["date"]

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_hold = sum(t.hold_days for t in trades) / len(trades) if trades else 0.0

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(),
                   "trading_days": n_days, "years": round(years, 2)},
        "starting_equity": round(eq[0], 2) if eq else STARTING_EQUITY,
        "ending_equity": round(eq[-1], 2) if eq else STARTING_EQUITY,
        "total_return_pct": round(total_return * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "num_trades": len(trades),
        "win_rate_pct": round(win_rate * 100, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "max_drawdown_date": dd_date,
        "avg_hold_days": round(avg_hold, 2),
        "equity_curve": equity_curve,
        "trades_sample": [
            {"symbol": t.symbol, "side": t.side, "regime": t.regime,
             "entry_date": t.entry_date.isoformat(),
             "exit_date": t.exit_date.isoformat() if t.exit_date else None,
             "ret_pct": round(t.ret_pct * 100, 2), "reason": t.reason}
            for t in trades[:25]
        ],
    }


# ---------------------------------------------------------------------------
# Training Optimizers (In-sample parameter tuning)
# ---------------------------------------------------------------------------
def optimize_mr_strategy(data: Dict, train_start: date, train_end: date) -> dict:
    print("Optimizing Pure Mean-Reversion (Strategy C) on training set...")
    best_sharpe = -float("inf")
    best_config = {"rsi_threshold": 30, "max_hold_days": 15}
    for rsi_thresh in [25, 30, 35]:
        for hold in [5, 10, 15]:
            config = {"rsi_threshold": rsi_thresh, "max_hold_days": hold}
            res = run_backtest(data, train_start, train_end, "C_mean_revert_long", config=config)
            sharpe = res["sharpe"]
            if res["num_trades"] >= 3 and sharpe > best_sharpe:
                best_sharpe = sharpe
                best_config = config
                print(f"  New best MR config: rsi={rsi_thresh}, hold={hold} -> Sharpe={sharpe:.3f}, Return={res['total_return_pct']}%")
    return best_config


def optimize_sma_strategy(data: Dict, train_start: date, train_end: date) -> dict:
    print("Optimizing 200-day SMA Trend Filter (Strategy D) on training set...")
    best_sharpe = -float("inf")
    best_config = {"use_regime_filter": False}
    for regime_filter in [False, True]:
        config = {"use_regime_filter": regime_filter}
        res = run_backtest(data, train_start, train_end, "D_sma_200_trend", config=config)
        sharpe = res["sharpe"]
        if res["num_trades"] >= 3 and sharpe > best_sharpe:
            best_sharpe = sharpe
            best_config = config
            print(f"  New best SMA config: use_regime_filter={regime_filter} -> Sharpe={sharpe:.3f}, Return={res['total_return_pct']}%")
    return best_config


def optimize_ma_crossover_strategy(data: Dict, train_start: date, train_end: date) -> dict:
    print("Optimizing MA Crossover (Strategy E) on training set...")
    best_sharpe = -float("inf")
    best_config = {"ma_type": "EMA", "fast_period": 20, "slow_period": 50, "use_regime_filter": False}
    for ma_type in ["EMA", "SMA"]:
        for fast, slow in [(10, 30), (20, 50), (50, 200)]:
            for regime_filter in [False, True]:
                config = {"ma_type": ma_type, "fast_period": fast, "slow_period": slow, "use_regime_filter": regime_filter}
                res = run_backtest(data, train_start, train_end, "E_ema_crossover", config=config)
                sharpe = res["sharpe"]
                if res["num_trades"] >= 3 and sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_config = config
                    print(f"  New best MA Crossover: {ma_type}({fast}/{slow}) regime={regime_filter} -> Sharpe={sharpe:.3f}, Return={res['total_return_pct']}%")
    return best_config


# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------
def main():
    print("Loading historical data (SPY, QQQ, IWM)…")
    data = {}
    for s in SYMBOLS:
        bars = load_data(s)
        data[s] = bars
        if bars:
            print(f"  {s}: {len(bars)} bars {bars[0].d} → {bars[-1].d}")
    if not data.get("SPY"):
        print("FATAL: no SPY data.")
        sys.exit(1)

    # Train/test split: 70% oldest = train (learn pattern memory and optimize parameters),
    # 30% newest = out-of-sample TEST. Split on SPY's calendar.
    ref = data["SPY"]
    split_k = int(len(ref) * 0.70)
    split_date = ref[split_k].d
    train_start, train_end = ref[0].d, ref[split_k - 1].d
    print(f"\nTrain: {train_start} → {train_end} ({split_k} bars)")
    print(f"Test (out-of-sample): {split_date} → {ref[-1].d} "
          f"({len(ref) - split_k} bars)\n")

    results = {
        "generated_at": datetime.now().isoformat() + "Z",
        "data_source": "Alpaca daily bars (fallback: Stooq)",
        "symbols": SYMBOLS,
        "split": {
            "train_start": train_start.isoformat(),
            "train_end": train_end.isoformat(),
            "test_start": split_date.isoformat(),
            "test_end": ref[-1].d.isoformat(),
            "train_frac": 0.70,
        },
        "risk_model": {"stop_loss_pct": STOP_LOSS_PCT,
                       "take_profit_pct": TAKE_PROFIT_PCT,
                       "risk_per_trade": RISK_PER_TRADE,
                       "max_hold_days": MAX_HOLD_DAYS},
        "variants": {},
    }

    # 1. Run Original Variant A and B
    for name, strategy_id in [("A_price_only", "A_price_only"), ("B_hybrid", "B_hybrid")]:
        merged_stats: Dict = {}
        for s in SYMBOLS:
            train_bars = [b for b in data[s] if b.d <= train_end]
            st = learn_pattern_stats(train_bars, strategy_id)
            for key, v in st.items():
                m = merged_stats.setdefault(key, {"n": 0, "wins": 0})
                m["n"] += v["n"]
                m["wins"] += v["wins"]
        
        metrics = run_backtest(data, split_date, ref[-1].d, strategy_id, merged_stats, {})
        metrics["pattern_stats_train"] = {
            f"{k[0]}|{k[1]}": {"n": v["n"], "win_rate":
                               round(v["wins"] / v["n"], 3) if v["n"] else 0.0}
            for k, v in merged_stats.items()
        }
        results["variants"][name] = metrics
        
        print(f"=== Variant {name} (out-of-sample) ===")
        for key in ("total_return_pct", "cagr_pct", "num_trades",
                    "win_rate_pct", "profit_factor", "sharpe", "sortino",
                    "max_drawdown_pct", "avg_hold_days"):
            print(f"  {key:20s}: {metrics[key]}")
        print()

    # 2. Run Strategy C (Pure Mean Reversion - Optimized)
    mr_config = optimize_mr_strategy(data, train_start, train_end)
    metrics_c = run_backtest(data, split_date, ref[-1].d, "C_mean_revert_long", config=mr_config)
    metrics_c["optimized_parameters"] = mr_config
    results["variants"]["C_mean_revert_long"] = metrics_c
    print(f"=== Strategy C Mean Reversion (out-of-sample) ===")
    print(f"  Optimized parameters: {mr_config}")
    for key in ("total_return_pct", "cagr_pct", "num_trades",
                "win_rate_pct", "profit_factor", "sharpe", "sortino",
                "max_drawdown_pct", "avg_hold_days"):
        print(f"  {key:20s}: {metrics_c[key]}")
    print()

    # 3. Run Strategy D (200-day SMA Trend Filter - Optimized)
    sma_config = optimize_sma_strategy(data, train_start, train_end)
    metrics_d = run_backtest(data, split_date, ref[-1].d, "D_sma_200_trend", config=sma_config)
    metrics_d["optimized_parameters"] = sma_config
    results["variants"]["D_sma_200_trend"] = metrics_d
    print(f"=== Strategy D 200-day SMA Filter (out-of-sample) ===")
    print(f"  Optimized parameters: {sma_config}")
    for key in ("total_return_pct", "cagr_pct", "num_trades",
                "win_rate_pct", "profit_factor", "sharpe", "sortino",
                "max_drawdown_pct", "avg_hold_days"):
        print(f"  {key:20s}: {metrics_d[key]}")
    print()

    # 4. Run Strategy E (MA Crossover - Optimized)
    ma_config = optimize_ma_crossover_strategy(data, train_start, train_end)
    metrics_e = run_backtest(data, split_date, ref[-1].d, "E_ema_crossover", config=ma_config)
    metrics_e["optimized_parameters"] = ma_config
    results["variants"]["E_ema_crossover"] = metrics_e
    print(f"=== Strategy E MA Crossover (out-of-sample) ===")
    print(f"  Optimized parameters: {ma_config}")
    for key in ("total_return_pct", "cagr_pct", "num_trades",
                "win_rate_pct", "profit_factor", "sharpe", "sortino",
                "max_drawdown_pct", "avg_hold_days"):
        print(f"  {key:20s}: {metrics_e[key]}")
    print()

    # 5. Run Strategy F (Volatility Breakout - Constant Params with ATR Stops)
    metrics_f = run_backtest(data, split_date, ref[-1].d, "F_volatility_breakout", config={})
    results["variants"]["F_volatility_breakout"] = metrics_f
    print(f"=== Strategy F Volatility Breakout (out-of-sample) ===")
    print(f"  Parameters: 20-day high/low breakout, ATR-based stops")
    for key in ("total_return_pct", "cagr_pct", "num_trades",
                "win_rate_pct", "profit_factor", "sharpe", "sortino",
                "max_drawdown_pct", "avg_hold_days"):
        print(f"  {key:20s}: {metrics_f[key]}")
    print()

    # Passive buy & hold SPY benchmark over the SAME out-of-sample window — honest
    # comparison: did any strategy beat simply owning the index?
    spy_test = [b for b in data["SPY"] if b.d >= split_date]
    bh_start = spy_test[0].c
    bh_curve = [{"date": b.d.isoformat(),
                 "equity": round(STARTING_EQUITY * b.c / bh_start, 2)}
                for b in spy_test]
    bh = compute_metrics([], bh_curve, split_date, spy_test[-1].d)
    bh["total_return_pct"] = round((spy_test[-1].c / bh_start - 1) * 100, 2)
    bh["note"] = "Passive buy-and-hold SPY, same test window (no trades)."
    results["benchmark_buy_hold_SPY"] = bh
    print("=== Benchmark: Buy & Hold SPY (out-of-sample) ===")
    print(f"  total_return_pct    : {bh['total_return_pct']}")
    print(f"  max_drawdown_pct    : {bh['max_drawdown_pct']}")
    print(f"  sharpe              : {bh['sharpe']}\n")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "backtest_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
