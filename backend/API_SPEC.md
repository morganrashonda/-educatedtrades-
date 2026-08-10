# Educated Trades — Backend API & Database Spec

## Overview

The backend consists of three modules that form a pipeline:

```
News Headlines
      │
      ▼
┌─────────────────┐     ┌──────────────────┐
│ SentimentEngine │────▶│  PatternEngine   │
│  sentiment.py   │     │   patterns.py    │
└─────────────────┘     └────────┬─────────┘
      │                          │
      └──────────┬───────────────┘
                 ▼
        ┌────────────────┐
        │ TradingEngine  │
        │  trading.py    │
        └────────────────┘
                 │
        ┌────────┴────────┐
        ▼                 ▼
   Alpaca API       Pocket Option
   (Stocks)         (Binary - placeholder)
```

---

## 1. Database Schema — `data/patterns.db`

### Table: `pattern_memory`

Stores every pattern occurrence with its outcome (once resolved).

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment record ID |
| timestamp | REAL | Unix timestamp when pattern was recorded |
| symbol | TEXT | Trading symbol (e.g., "SPY") |
| pattern_hash | TEXT | SHA-256 hash of `sentiment_zone\|rsi_zone\|ema_cross` (16-char hex) |
| sentiment_zone | TEXT | `bearish` \| `neutral` \| `bullish` |
| rsi_zone | TEXT | `oversold` \| `normal` \| `overbought` |
| ema_cross | TEXT | `bearish_cross` \| `no_cross` \| `bullish_cross` |
| sentiment_score | REAL | Raw conviction score from sentiment engine (-1 to +1) |
| rsi_value | REAL | RSI value at time of pattern |
| conviction_score | REAL | Blended conviction score |
| entry_price | REAL | Entry price at pattern recording |
| exit_price | REAL | Exit price (NULL if pending) |
| exit_hours_later | REAL | Hours between entry and exit (NULL if pending) |
| profit_pct | REAL | Profit/loss percentage (NULL if pending) |
| outcome | TEXT | `win` \| `loss` \| `pending` |

### Table: `pattern_stats`

Aggregated statistics per unique pattern signature.

| Column | Type | Description |
|--------|------|-------------|
| pattern_id | TEXT PK | Same as pattern_hash |
| sentiment_zone | TEXT | bearish \| neutral \| bullish |
| rsi_zone | TEXT | oversold \| normal \| overbought |
| ema_cross | TEXT | bearish_cross \| no_cross \| bullish_cross |
| count | INTEGER | Total occurrences |
| wins | INTEGER | Winning outcomes |
| losses | INTEGER | Losing outcomes |
| total_profit_pct | REAL | Sum of all profit percentages |
| last_seen | REAL | Unix timestamp of most recent occurrence |

**Derived fields (computed at runtime, not stored):**
- `win_rate` = wins / count
- `avg_profit_pct` = total_profit_pct / count
- `signal_strength` = (win_rate - 0.5) × 2 × min(1, count/30)  → range [-1, +1]
- `is_robust` = count >= 10

### Table: `pattern_learned_weights`

Adaptive weights that indicate which indicator dimension is most predictive.

| Column | Type | Description |
|--------|------|-------------|
| weight_id | TEXT PK | `"default"` |
| sentiment_mult | REAL | Sentiment predictive multiplier |
| rsi_mult | REAL | RSI predictive multiplier |
| ema_mult | REAL | EMA cross predictive multiplier |
| updated_at | REAL | Last retrain timestamp |

---

## 2. Python Import API

Each module is importable directly from `/home/team/shared/backend/`.

### Sentiment Engine — `sentiment.py`

```python
from sentiment import MarketSentimentEngine, quick_batch, quick_conviction

# Method 1: Full control
engine = MarketSentimentEngine()
result = engine.analyze(["Fed holds rates steady", "New tariffs announced"])
# result.aggregate_conviction  → float (-1 to +1)
# result.consensus             → "bullish" | "neutral" | "bearish"
# result.volatility_signal     → float (std dev of headline scores)
# result.headlines             → list of HeadlineResult

# Method 2: One-shot (returns dict — JSON-serializable)
data = quick_batch(["Fed holds rates steady", "New tariffs announced"])
# data = {
#   "conviction_score": 0.12,
#   "consensus": "neutral",
#   "volatility_signal": 0.45,
#   "headline_count": 2,
#   "headlines": [
#     {"text": "...", "conviction": 0.55, "label": "bullish",
#      "confidence": "medium", "matched_keywords": ["rate hike"]},
#     ...
#   ]
# }

# Method 3: Quick aggregate (just the number)
score = quick_conviction(["Fed holds rates steady"])
```

### Pattern Engine — `patterns.py`

```python
from patterns import PatternEngine

engine = PatternEngine()

# Record a new pattern occurrence
record_id = engine.record_pattern(
    symbol="SPY",
    sentiment_score=0.7,
    conviction_score=0.65,
    rsi_value=62.0,
    ema_short=510.5,
    ema_long=505.2,
)

# Later, record the outcome (the "learning" step)
engine.record_outcome(
    record_id=record_id,
    exit_price=520.0,
    hours_later=4.5,
)

# Evaluate current conditions against historical memory
signal = engine.evaluate(
    symbol="SPY",
    sentiment_score=0.55,
    conviction_score=0.50,
    rsi_value=58.0,
    ema_short=510.0,
    ema_long=507.0,
    prev_ema_short=508.0,  # optional — enables cross detection
    prev_ema_long=506.5,
)

# signal.action      → "strong_buy" | "buy" | "neutral" | "sell" | "strong_sell"
# signal.conviction  → float (-1 to +1)
# signal.reason      → human-readable string
# signal.pattern_stats.win_rate
# signal.pattern_stats.count

# Get summary of all learned patterns
summary = engine.summary()
# summary = {
#   "total_patterns": 12,
#   "total_occurrences": 87,
#   "robust_patterns": 3,
#   "pending_outcomes": 5,
#   "learned_weights": {"sentiment_mult": 1.2, "rsi_mult": 0.8, "ema_mult": 1.0},
#   "top_patterns": [...]
# }
```

### Trading Engine — `trading.py`

```python
from trading import TradingEngine, TradeSignal
from sentiment import MarketSentimentEngine
from patterns import PatternEngine

# Initialise (simulate=True if no Alpaca keys)
engine = TradingEngine(simulate=True)

# Option A: Provide a raw signal directly
signal = TradeSignal(
    symbol="SPY",
    action="buy",
    conviction=0.65,
    source="sentiment+pattern",
    reason="Bullish sentiment + pattern match"
)
result = engine.execute(signal)

# Option B: Full pipeline — sentiment + pattern → execution
result = engine.evaluate_and_execute(
    symbol="SPY",
    sentiment_conviction=0.65,
    pattern_signal=pattern_signal,      # from PatternEngine.evaluate()
    sentiment_reason="Bullish headline batch aggregate",
)

# result = ExecutionResult
# result.success      → bool
# result.side         → "buy" | "sell" | None
# result.quantity     → int (number of shares)
# result.filled_price → float
# result.status       → "filled" | "rejected" | etc.
# result.latency_ms   → float
# result.order_id     → str
# result.dict()       → dict (JSON-serializable)
```

---

## 3. Dashboard JSON Endpoints (proposed for Frontend)

These are JSON structures the backend can emit for the frontend dashboard to consume. They are ready to be served via a simple REST API or WebSocket stream.

### `GET /api/status` — Overall system health

```json
{
  "sentiment_engine": "ready",
  "pattern_engine": {
    "patterns_learned": 12,
    "total_occurrences": 87,
    "robust_patterns": 3,
    "pending_outcomes": 5
  },
  "trading_engine": {
    "mode": "simulation",
    "buying_power": 100000.0,
    "equity": 100000.0,
    "open_positions": 1,
    "trades_today": 5
  }
}
```

### `GET /api/sentiment/latest` — Latest sentiment analysis

```json
{
  "timestamp": 1700000000.0,
  "headlines_analyzed": 5,
  "aggregate_conviction": 0.25,
  "consensus": "bullish",
  "volatility_signal": 0.48,
  "headlines": [
    {
      "text": "Fed holds rates steady",
      "conviction": 0.55,
      "label": "bullish",
      "confidence": "high",
      "matched_keywords": ["hold rates"]
    }
  ]
}
```

### `GET /api/patterns/top` — Top learned patterns

```json
{
  "patterns": [
    {
      "signature": "bullish_normal_no_cross",
      "count": 24,
      "win_rate": 0.83,
      "avg_profit_pct": 1.2,
      "signal_strength": 0.55,
      "is_robust": true
    }
  ],
  "learned_weights": {
    "sentiment": 1.2,
    "rsi": 0.8,
    "ema": 1.0
  }
}
```

### `GET /api/trades/recent` — Recent trade history

```json
{
  "trades": [
    {
      "timestamp": 1700000000.0,
      "symbol": "SPY",
      "action": "buy",
      "conviction": 0.65,
      "source": "sentiment+pattern",
      "success": true,
      "side": "buy",
      "quantity": 100,
      "filled_price": 550.25,
      "status": "filled",
      "latency_ms": 1.2
    }
  ]
}
```

### `GET /api/evaluate?symbol=SPY` — Evaluate current conditions

Trigger a live evaluation and return the signal + execution recommendation.

```json
{
  "symbol": "SPY",
  "sentiment_conviction": 0.55,
  "pattern_signal": {
    "action": "buy",
    "conviction": 0.38,
    "reason": "Pattern: bullish_normal_no_cross | ..."
  },
  "blended_signal": {
    "action": "buy",
    "conviction": 0.47,
    "source": "sentiment+pattern"
  },
  "trade_recommendation": {
    "actionable": true,
    "side": "buy",
    "estimated_quantity": 42
  }
}
```

---

## 4. File Locations

| Asset | Path |
|-------|------|
| Sentiment Engine | `/home/team/shared/backend/sentiment.py` |
| Pattern Engine | `/home/team/shared/backend/patterns.py` |
| Trading Engine | `/home/team/shared/backend/trading.py` |
| Pattern Database | `/home/team/shared/data/patterns.db` |
| Python Venv | `/home/team/shared/venv/bin/python` |

---

## 5. Environment Variables

| Variable | Purpose |
|----------|---------|
| `APCA_API_KEY_ID` | Alpaca API key (for live stock trading) |
| `APCA_API_SECRET_KEY` | Alpaca API secret key |
| `APCA_BASE_URL` | Alpaca base URL (defaults to paper trading) |

All modules work in simulation mode when credentials are absent — no env vars required for development/testing.