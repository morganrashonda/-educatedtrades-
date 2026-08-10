# Mode Precedence

How the bot determines its operating mode at startup and at runtime.

---

## Startup Precedence (Highest to Lowest)

| Priority | Input | Effect |
|----------|-------|--------|
| **1** | `killed_state` file (`{DATA_DIR}/killed_state`) | If present: `killed=True`, trading blocked regardless of mode. Cleared only by RESET_DRAWDOWN. |
| **2** | Operator mode file (`{DATA_DIR}/orchestrator_mode.txt`) | Operator-owned. Valid: `manual`, `autonomous`, `stopped`. Legacy `killed` → manual. Unknown → MANUAL. |
| **3** | Default fallback | **MANUAL**. No env vars, no CLI flags. Bot always starts MANUAL unless the operator mode file says otherwise. |

---

## Runtime Precedence

| Input | Takes Effect | Writes File? |
|-------|-------------|--------------|
| `POST /api/mode` (`set_mode()`) | Immediately | Yes — writes to `orchestrator_mode.txt` |
| Direct file write to `orchestrator_mode.txt` | **No** — file only read at startup and on API `set_mode()` | N/A |
| Kill mechanisms | Immediately (via `killed=True` flag) | Yes — writes to `killed_state` file |

Only `POST /api/mode` changes the operator mode at runtime. Writing to `orchestrator_mode.txt` by hand has no effect on a running bot — the bot reads it only at startup.

---

## Demotion / Kill Triggers

| Trigger | Writes To | Effect |
|---------|-----------|--------|
| Drawdown ≥ 15% | `killed_state` | Kills trading, persists across restarts |
| `KILL_SWITCH` file detected | `killed_state` | Kills trading, persists across restarts |
| `POST /api/kill` | `killed_state` | Kills trading, persists across restarts |
| `_trigger_kill_switch()` (critical exception) | `killed_state` | Kills trading, persists across restarts |
| Pre-market health check FAIL | Nothing (session flag only) | Blocks trading for session, retries at next phase transition |
| Equity read failures | Nothing | WARNING/CRITICAL logs only |
| Daily loss limit hit | Nothing (in-memory `DAILY_LOSS_LIMIT` mode) | Auto-clears on next day or reset |

**Key invariant:** Automated conditions NEVER write to `orchestrator_mode.txt`. Kill mechanisms write to `killed_state`. Health failures use a session flag. Only the operator (via API) writes the mode file.

---

## Restart Behavior

```
On startup:
  1. killed_state file exists? → killed=True, trading blocked.
     (mode from operator file, or MANUAL if absent)
  2. No killed_state file? → killed=False.
     Read operator file:
       "autonomous" → AUTONOMOUS
       "manual"     → MANUAL
       "stopped"    → STOPPED
       "killed"     → MANUAL (legacy)
       missing/unknown → MANUAL
```

Killed state survives restarts — the bot stays dead until the operator clears it.

---

## File Paths

All paths derived from `DATA_DIR` env var:

| File | Purpose | Written By |
|------|---------|------------|
| `{DATA_DIR}/orchestrator_mode.txt` | Operator's mode choice | Operator (via `POST /api/mode`) |
| `{DATA_DIR}/killed_state` | Kill-state sentinel | Bot (on kill triggers) |
| `{DATA_DIR}/KILL_SWITCH` | External kill trigger | Operator |
| `{DATA_DIR}/RESET_DRAWDOWN` | Reset signal | Operator |

Production: `/opt/educated_trades/data/`
Sandbox: `/home/team/shared/data/`

---

## Reset Procedure

`RESET_DRAWDOWN` file (checked each pipeline cycle) and `POST /api/reset` both:

1. Set `killed = False`
2. Set `health_failed_this_session = False`
3. Remove `killed_state` file from disk
4. Re-read `orchestrator_mode.txt` to restore operator mode
5. Reset drawdown tracker (peak equity, max drawdown)

## Absent-File Defaults

| File Missing | Default |
|-------------|---------|
| No `killed_state` | `killed = False` (normal operation) |
| No `orchestrator_mode.txt` | `mode = MANUAL` |
| Both missing | `killed = False`, `mode = MANUAL` |

## Removed Inputs

These no longer exist (removed in rework):

- **`TRADING_MODE` env var** — Dead code. `_load_persisted_mode()` always overrides.
- **`--autonomous` / `-a` CLI flag** — Dead code. Same reason.
