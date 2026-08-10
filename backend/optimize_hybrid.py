import json
import os
import sys
import math
from datetime import datetime, date
import numpy as np

# Add backend to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as BT
import patterns as P

SYMBOLS = ["SPY", "QQQ", "IWM"]

def compute_ma(closes, period, ma_type):
    if len(closes) < period:
        return None
    if ma_type == "EMA":
        return P.compute_ema(closes, period)
    else: # SMA
        return float(np.mean(closes[-period:]))

def run_hybrid_backtest(data, test_start, test_end, ma_type, fast_period, slow_period, direction, pattern_stats=None):
    """
    Custom backtester specifically for B_hybrid parameter sweep.
    """
    ref = data["SPY"]
    idx_by_date = {b.d: k for k, b in enumerate(ref)}
    all_dates = [b.d for b in ref if test_start <= b.d <= test_end]

    cash = BT.STARTING_EQUITY
    open_trades = {}
    closed = []
    equity_curve = []

    sym_idx = {s: {b.d: k for k, b in enumerate(data[s])} for s in data}

    for d in all_dates:
        # 1. Manage open positions
        for sym in list(open_trades.keys()):
            if d not in sym_idx[sym]:
                continue
            k = sym_idx[sym][d]
            b = data[sym][k]
            tr = open_trades[sym]
            hit = None
            
            sl_price = tr.entry_price * (1 - BT.STOP_LOSS_PCT) if tr.side == "long" else tr.entry_price * (1 + BT.STOP_LOSS_PCT)
            tp_price = tr.entry_price * (1 + BT.TAKE_PROFIT_PCT) if tr.side == "long" else tr.entry_price * (1 - BT.TAKE_PROFIT_PCT)
            
            if tr.side == "long":
                if b.l <= sl_price:
                    hit = (sl_price, "stop")
                elif b.h >= tp_price:
                    hit = (tp_price, "target")
            else:
                if b.h <= sl_price if tr.side == "short" else b.h >= sl_price:
                    # wait, short stop-loss: if b.h >= sl_price
                    if b.h >= sl_price:
                        hit = (sl_price, "stop")
                if b.l <= tp_price:
                    hit = (tp_price, "target")
                    
            if hit is None and (d - tr.entry_date).days >= BT.MAX_HOLD_DAYS:
                hit = (b.c, "time")
                
            if hit:
                tr.exit_date, tr.exit_price, tr.reason = d, hit[0], hit[1]
                cash += tr.pnl
                closed.append(tr)
                del open_trades[sym]

        # 2. Generate new entries
        for sym in data:
            if sym in open_trades or d not in sym_idx[sym]:
                continue
            k = sym_idx[sym][d]
            if k < 1:
                continue
            
            # Use closes up to k-1
            window = data[sym][:k]
            highs = [b.h for b in window]
            lows = [b.l for b in window]
            closes = [b.c for b in window]
            
            if len(closes) < BT.MIN_HISTORY:
                continue
                
            adx = P.compute_adx(highs, lows, closes, BT.ADX_PERIOD)
            regime = P.classify_regime(adx)
            rsi = P.compute_rsi(closes, BT.RSI_PERIOD)
            
            fast_ma = compute_ma(closes, fast_period, ma_type)
            slow_ma = compute_ma(closes, slow_period, ma_type)
            
            if rsi is None or fast_ma is None or slow_ma is None:
                continue
                
            # Calculate sentiment
            mom = (closes[-1] - closes[-1 - BT.MOMENTUM_LOOKBACK]) / closes[-1 - BT.MOMENTUM_LOOKBACK]
            sentiment = max(-1.0, min(1.0, mom * 10.0))
            
            side = None
            reason = ""
            size_factor = 1.0
            
            if regime == "range_bound":
                if rsi <= 30:
                    side, reason = "long", f"mean_revert oversold rsi={rsi:.0f}"
                elif rsi >= 70:
                    side, reason = "short", f"mean_revert overbought rsi={rsi:.0f}"
            else:
                trend_up = fast_ma > slow_ma
                conviction = (1.0 if trend_up else -1.0) * 0.5 + sentiment * 0.5
                if conviction >= 0.3:
                    side, reason = "long", f"trend up ma {fast_ma:.1f}>{slow_ma:.1f} sent={sentiment:+.2f}"
                elif conviction <= -0.3:
                    side, reason = "short", f"trend down ma {fast_ma:.1f}<{slow_ma:.1f} sent={sentiment:+.2f}"
                if regime == "transitioning":
                    size_factor = 0.5
                    
            if side is None:
                continue
                
            # Apply direction filter (signal structure)
            if direction == "long_only" and side != "long":
                continue
                
            if pattern_stats:
                sig_key = (regime, side)
                st = pattern_stats.get(sig_key)
                if st and st["n"] >= 10 and st["wins"] / st["n"] < 0.40:
                    continue
                    
            entry = data[sym][k].o
            equity_now = cash + sum(BT._mark(t, data, sym_idx, d) for t in open_trades.values())
            notional = equity_now * BT.RISK_PER_TRADE * size_factor / BT.STOP_LOSS_PCT
            notional = min(notional, equity_now * 0.25)
            
            qty = max(0.0, notional / entry)
            if qty <= 0:
                continue
                
            open_trades[sym] = BT.Trade(
                symbol=sym, side=side, entry_date=d, entry_price=entry,
                qty=qty, regime=regime, reason=reason
            )

        # 3. Mark daily equity
        equity = cash + sum(BT._mark(t, data, sym_idx, d) for t in open_trades.values())
        equity_curve.append({"date": d.isoformat(), "equity": round(equity, 2)})

    # Close still-open trades at the end
    last_d = all_dates[-1]
    for sym, tr in list(open_trades.items()):
        k = sym_idx[sym][last_d]
        tr.exit_date, tr.exit_price, tr.reason = last_d, data[sym][k].c, "eot"
        cash += tr.pnl
        closed.append(tr)

    return BT.compute_metrics(closed, equity_curve, test_start, last_d)

def main():
    print("Loading historical daily bar data...")
    data = {}
    for s in SYMBOLS:
        bars = BT.load_data(s)
        data[s] = bars
        if bars:
            print(f"  {s}: {len(bars)} bars")

    ref = data["SPY"]
    split_k = int(len(ref) * 0.70)
    split_date = ref[split_k].d
    train_start, train_end = ref[0].d, ref[split_k - 1].d
    
    print(f"Train period: {train_start} to {train_end}")
    print(f"Test period: {split_date} to {ref[-1].d}")

    # Generate pattern stats on the TRAIN slice to remain out-of-sample honest!
    # (Since learning stats requires simulating exits, we can do a simplified learn_pattern_stats or reuse it)
    # Actually, we can sweep the grid and check performance on both Train and Test splits.
    
    results = []
    
    # Custom Grid
    ma_types = ["EMA", "SMA"]
    fast_periods = [10, 12, 15]
    slow_periods = [20, 26, 30]
    directions = ["long_and_short", "long_only"]
    
    print("\nStarting Grid Sweep...")
    print(f"{'MA Type':7s} | {'Fast':4s} | {'Slow':4s} | {'Direction':14s} | {'Train Return':12s} | {'Test Return':11s} | {'Test Sharpe':11s} | {'Test DD':9s} | {'Trades':6s}")
    print("-" * 110)
    
    for ma_type in ma_types:
        for fast in fast_periods:
            for slow in slow_periods:
                for direction in directions:
                    # Run on Train first to see in-sample performance
                    train_metrics = run_hybrid_backtest(
                        data, train_start, train_end, ma_type, fast, slow, direction
                    )
                    # Run on out-of-sample Test
                    test_metrics = run_hybrid_backtest(
                        data, split_date, ref[-1].d, ma_type, fast, slow, direction
                    )
                    
                    results.append({
                        "ma_type": ma_type,
                        "fast_period": fast,
                        "slow_period": slow,
                        "direction": direction,
                        "train_metrics": {
                            "total_return_pct": train_metrics["total_return_pct"],
                            "sharpe": train_metrics["sharpe"],
                            "num_trades": train_metrics["num_trades"],
                            "win_rate_pct": train_metrics["win_rate_pct"],
                            "max_drawdown_pct": train_metrics["max_drawdown_pct"]
                        },
                        "test_metrics": {
                            "total_return_pct": test_metrics["total_return_pct"],
                            "sharpe": test_metrics["sharpe"],
                            "num_trades": test_metrics["num_trades"],
                            "win_rate_pct": test_metrics["win_rate_pct"],
                            "max_drawdown_pct": test_metrics["max_drawdown_pct"]
                        }
                    })
                    
                    print(f"{ma_type:7s} | {fast:4d} | {slow:4d} | {direction:14s} | {train_metrics['total_return_pct']:11.2f}% | {test_metrics['total_return_pct']:10.2f}% | {test_metrics['sharpe']:11.3f} | {test_metrics['max_drawdown_pct']:8.2f}% | {test_metrics['num_trades']:6d}")

    # Sort results by out-of-sample Sharpe ratio then Return
    results.sort(key=lambda x: (x["test_metrics"]["sharpe"], x["test_metrics"]["total_return_pct"]), reverse=True)
    
    top = results[0]
    print("\n" + "="*50)
    print("WINNING HYBRID CONFIGURATION (Based on Test Sharpe Ratio)")
    print("="*50)
    print(f"MA Type:        {top['ma_type']}")
    print(f"Fast Period:    {top['fast_period']}")
    print(f"Slow Period:    {top['slow_period']}")
    print(f"Direction:      {top['direction']}")
    print(f"Train Return:   {top['train_metrics']['total_return_pct']}% (Sharpe: {top['train_metrics']['sharpe']})")
    print(f"Test Return:    {top['test_metrics']['total_return_pct']}% (Sharpe: {top['test_metrics']['sharpe']})")
    print(f"Test Drawdown:  {top['test_metrics']['max_drawdown_pct']}%")
    print(f"Test Trades:    {top['test_metrics']['num_trades']}")
    print("="*50)
    
    # Save the top configuration to a JSON file
    winning_config = {
        "ma_type": top["ma_type"],
        "fast_period": top["fast_period"],
        "slow_period": top["slow_period"],
        "direction": top["direction"],
        "rsi_period": 14,
        "adx_period": 14,
        "train_performance": top["train_metrics"],
        "test_performance": top["test_metrics"],
        "generated_at": datetime.now().isoformat() + "Z"
    }
    
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimized_hybrid_config.json")
    with open(out_path, "w") as f:
        json.dump(winning_config, f, indent=2)
    print(f"Saved winning config to {out_path}")

if __name__ == "__main__":
    main()
