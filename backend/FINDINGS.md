# Fix Branch: fix/all-pattern-and-mode-issues

## Changes Included

### 1. TRADING_MODE env var parsing fix (main.py:3107)
**Before:** `TRADING_MODE=autonomous` was misread as manual because `"autonomous"` was accidentally in the manual-mode list.
**After:** `TRADING_MODE=autonomous` correctly enables autonomous mode.
**Also:** `--autonomous` CLI flag still works.

### 2. Mode persistence (main.py)
**Problem:** Mode was in-memory only. After restart, mode reverted to startup defaults.
**Fix:** Added `_load_persisted_mode()` / `_save_persisted_mode()` using a text file at `DATA_DIR/orchestrator_mode.txt`. `set_mode()` now persists the mode. On startup, the persisted mode overrides the startup default.
**Note:** The CLI flag and env var still set the initial mode; the persisted file overrides it. If you want to reset to factory, delete the file.

### 3. Seed data removed from startup (main.py)
**Change:** Removed the `seed_from_daily_bars()` call from the startup sequence. Pattern memory starts empty and builds only from real pipeline outcomes.
**Skip behavior:** When `evaluate()` finds `stats.count == 0`, it returns an `EvaluationSignal` with `action="skip"` and `conviction=0.0` — never substitutes default values.

### 4. Per-symbol pattern hash (patterns.py)
**Change:** `PatternSignature` now includes `symbol` in the hash. The `hash_id` property uses `f"{self.symbol}|{self.sentiment_zone}|{self.rsi_zone}|{self.ema_cross}"`. This means SPY, QQQ, and IWM each get their own `pattern_stats` rows.
**Call sites updated:** `_build_signature()`, `record_pattern()`, `evaluate()`, `seed_mock_data()`.
**Legacy default:** `symbol: str = ""` preserves backward compatibility for any existing code constructing `PatternSignature` without a symbol.

### 5. Zero-sample skip in evaluate() (patterns.py)
**Change:** Added early return when `stats.count == 0`:
```python
if stats.count == 0:
    return EvaluationSignal(
        symbol=symbol, pattern_signature=signature, pattern_stats=stats,
        action="skip", conviction=0.0,
        reason="No historical samples for this pattern yet — build from real trades",
    )
```
This prevents the old behavior where `signal_strength=0.0` was blended with sentiment conviction, potentially producing non-zero signals from zero data.

### 6. data_source filter in /api/stats (stats.py)
**Change:** All five SQL queries now filter by `AND data_source = 'live'`. This ensures seed rows can never contaminate the 100-trade gate or stats, even if seed data is re-introduced later.

### 7. Migration script (scripts/cleanup_seed_data.py)
**Script:** `scripts/cleanup_seed_data.py` — deletes all rows where `data_source = 'seed'` and clears `pattern_stats`. Run manually on the droplet after deploying this branch:
```
python3 scripts/cleanup_seed_data.py
```

## Not Addressed in This Branch
- **1.000 confidence on droplet:** Still unexplained. Query the droplet's `pattern_stats` table and verify the deployed commit to trace the source.