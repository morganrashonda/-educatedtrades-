#!/usr/bin/env python3
"""
External Health Check — standalone watchdog for Educated Trades.

Polls the bot's heartbeat file, journald logs, and mode file from
OUTSIDE the bot process. Fires webhook alerts on anomalies. Designed
to run every 300 seconds via systemd timer.

Configuration (env vars):
    HEALTH_WEBHOOK_URL     Webhook URL (Discord or generic). Falls back to
                           WATCHDOG_DISCORD_WEBHOOK for backward compat.
    DATA_DIR               Path to bot data directory (heartbeat, mode file).
                           Default: /opt/educated_trades/data
    JOURNAL_UNIT           systemd unit to tail for critical logs.
                           Default: educated-trades.service
    HEARTBEAT_STALE_SEC    Seconds before heartbeat is considered stale.
                           Default: 600 (10 minutes)

Usage:
    python3 scripts/health_check.py          # normal run
    python3 scripts/health_check.py --test   # send test alert and exit

State file: ~/.educated_trades_health_state.json
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------
def _resolve_data_dir() -> str:
    """Find the bot's data directory the way the bot itself derives it.

    This runs under its own systemd unit, so unlike the in-process watchdog it
    does NOT inherit the DATA_DIR the orchestrator exports. Reading an
    environment variable with a hardcoded default meant that the moment the
    data directory became credential-derived ($DATA_ROOT/paper, $DATA_ROOT/live)
    this monitored a path nothing writes to -- and a health check pointed at
    the wrong directory reports a healthy silence, which is worse than no
    health check at all.

    So it derives the same answer from the same credentials. An explicit
    DATA_DIR still wins, for one-off runs.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
        from trading import resolve_data_dir
        return resolve_data_dir()
    except Exception:
        # The bot may not be importable from here (separate deployment root).
        # Fall back to the same shape rather than a fixed path.
        return os.environ.get("DATA_DIR") or os.path.join(
            os.environ.get("DATA_ROOT", "/home/team/shared/data"), "paper")


DATA_DIR = _resolve_data_dir()

HEARTBEAT_PATH = Path(DATA_DIR) / "heartbeat"
MODE_FILE_PATH = Path(DATA_DIR) / "orchestrator_mode.txt"
STATE_PATH = Path(
    os.environ.get("HEALTH_STATE_FILE",
                   str(Path(DATA_DIR) / "health_state.json"))
)

# Journald unit to tail for critical log lines
JOURNAL_UNIT = os.environ.get("JOURNAL_UNIT", "educated-trades.service")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WEBHOOK_URL = os.environ.get(
    "HEALTH_WEBHOOK_URL",
    os.environ.get("WATCHDOG_DISCORD_WEBHOOK", ""),
)
STALE_SECONDS = int(os.environ.get("HEARTBEAT_STALE_SEC", "600"))

# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------
def warn_if_webhook_unconfigured() -> bool:
    """Warn once at startup when alert delivery is not configured.

    The checks still run without a webhook so local/manual invocations remain
    useful, but no alert can reach the operator until the environment is fixed.
    Returns True when a webhook is configured.
    """
    if WEBHOOK_URL:
        return True

    print(
        "[health-check] WARNING: HEALTH_WEBHOOK_URL is empty; "
        "health checks will run but alerts cannot be delivered. "
        "Set HEALTH_WEBHOOK_URL in /etc/educated-trades-health.env and restart "
        "the health-check timer."
    )
    return False


def send_webhook(message: str) -> bool:
    """Send an alert message to the configured webhook. Returns True on success."""
    if not WEBHOOK_URL:
        print(f"[health-check] WARN: No webhook URL configured. Would alert: {message}")
        return False

    try:
        import urllib.request
        # Discord-compatible payload; works with generic webhooks too.
        payload = json.dumps({"content": message}).encode()
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status in (200, 204)
            if ok:
                print(f"[health-check] Alert sent: {message[:80]}...")
            else:
                print(f"[health-check] Webhook returned status {resp.status}")
            return ok
    except Exception as e:
        print(f"[health-check] ERROR: Webhook delivery failed: {e}")
        return False

# ---------------------------------------------------------------------------
# State persistence (log position, last mode)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    """Load persisted state (last log check timestamp, last seen mode, active alerts)."""
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_log_check": None, "last_mode": None, "active_alerts": []}

def save_state(state: dict) -> None:
    """Persist state to disk."""
    STATE_PATH.write_text(json.dumps(state))

# ---------------------------------------------------------------------------
# Check 1: Heartbeat staleness
# ---------------------------------------------------------------------------
def check_heartbeat() -> list:
    """
    Check if the heartbeat file is stale (> STALE_SECONDS old).
    Returns a list of alert messages (empty if healthy).
    """
    if not HEARTBEAT_PATH.exists():
        return ["🔴 Heartbeat file missing — orchestrator may not be running."]

    try:
        data = json.loads(HEARTBEAT_PATH.read_text())
        ts = data.get("timestamp")
        if ts is None:
            return ["⚠️ Heartbeat file exists but has no timestamp."]
        age = time.time() - ts
        if age > STALE_SECONDS:
            return [
                f"🔴 Heartbeat STALE: {age:.0f}s old (threshold {STALE_SECONDS}s). "
                f"Orchestrator may be down or frozen."
            ]
    except (json.JSONDecodeError, OSError) as e:
        return [f"⚠️ Heartbeat file corrupted: {e}"]

    return []

# ---------------------------------------------------------------------------
# Check 2: CRITICAL log lines (via journald)
# ---------------------------------------------------------------------------
def check_critical_logs(state: dict) -> tuple:
    """
    Query journald for recent lines from the orchestrator unit,
    then substring-match for CRITICAL / 🔴 markers.
    Returns (list of alert messages, new_last_check timestamp).
    On failure the watermark is NOT advanced — missed lines will
    be re-scanned on the next run.
    """
    alerts = []
    last_check = state.get("last_log_check")
    # Type guard: old state files may have ISO-format strings.
    if not isinstance(last_check, (int, float)):
        last_check = None
    now = datetime.now(timezone.utc)

    try:
        cmd = [
            "journalctl", "-u", JOURNAL_UNIT,
            "--no-pager", "-o", "cat",
        ]
        if last_check is not None:
            # Clamp lookback to max 1 hour to avoid replaying the
            # entire journal if the checker was down for days.
            check_since = max(last_check, now.timestamp() - 3600)
            cmd += ["--since", f"@{int(check_since)}"]
        else:
            # First run — bound to the last 10 minutes so we don't
            # replay the entire journal on deploy.
            cmd += ["--since", "-10min"]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )

        if result.returncode != 0:
            alerts.append(
                f"⚠️ journalctl exited {result.returncode}: "
                f"{result.stderr.strip()[:200]}"
            )
            return alerts, last_check  # watermark NOT advanced

        for line in result.stdout.strip().split("\n"):
            if "CRITICAL" in line or "🔴" in line:
                snippet = line.strip()[:300]
                alerts.append(f"🔴 CRITICAL in orchestrator log: {snippet}")

        return alerts, now.timestamp()  # watermark advanced on success

    except FileNotFoundError:
        alerts.append("⚠️ journalctl not found — cannot check logs")
    except subprocess.TimeoutExpired:
        alerts.append("⚠️ journalctl timed out — logs not checked this cycle")
    except Exception as e:
        alerts.append(f"⚠️ journalctl error: {e}")

    return alerts, last_check  # watermark NOT advanced on any failure

# ---------------------------------------------------------------------------
# Check 3: Mode changes
# ---------------------------------------------------------------------------
def check_mode_change(state: dict) -> tuple:
    """
    Check if orchestrator_mode.txt has changed since last run.
    Returns (alert messages list, new_mode).
    """
    alerts = []
    last_mode = state.get("last_mode")

    if not MODE_FILE_PATH.exists():
        # Mode file gone — if we had one before, that's a change
        if last_mode is not None:
            alerts.append(
                f"⚠️ orchestrator_mode.txt has DISAPPEARED. "
                f"Last known mode was '{last_mode}'."
            )
        return alerts, None

    try:
        current_mode = MODE_FILE_PATH.read_text().strip().lower()
    except OSError as e:
        return [f"⚠️ Could not read orchestrator_mode.txt: {e}"], last_mode

    if current_mode and last_mode is not None and current_mode != last_mode:
        alerts.append(
            f"⚠️ Orchestrator mode changed: '{last_mode}' → '{current_mode}'."
        )
    elif last_mode is None and current_mode:
        # First run with a valid mode file — just record, don't alert
        pass

    return alerts, current_mode

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    test_mode = "--test" in sys.argv
    webhook_configured = warn_if_webhook_unconfigured()

    if test_mode:
        print("[health-check] TEST MODE: sending test alert...")
        ok = send_webhook("✅ Educated Trades Health Check — TEST ALERT. Webhook is configured and working.")
        if ok:
            print("[health-check] Test alert sent successfully.")
        else:
            print("[health-check] Test alert FAILED — check webhook URL.")
            sys.exit(1)
        return

    state = load_state()
    # active_alerts is now a dict: {alert_type: {"hashes": [...], "last_notify": float}}
    prev_alerts = state.get("active_alerts", {})
    if not isinstance(prev_alerts, dict):
        # Migrate from old list format
        prev_alerts = {}
    current_raw = {}  # alert_type -> list of ALL messages

    # --- Heartbeat ---
    hb_alerts = check_heartbeat()
    if hb_alerts:
        # Classify heartbeat alert type from message content.
        # NOTE: substring classification ("missing"/"corrupted") is fragile —
        # if check_heartbeat() changes its phrasing these will silently break.
        hb_msg = hb_alerts[0]
        if "missing" in hb_msg.lower():
            hb_type = "heartbeat_missing"
        elif "corrupted" in hb_msg.lower():
            hb_type = "heartbeat_missing"  # same type for dedup
        else:
            hb_type = "heartbeat_stale"
        current_raw[hb_type] = hb_alerts  # ALL messages, not just [0]
        print(f"[health-check] Heartbeat: {len(hb_alerts)} alert(s)")

    # --- Critical logs ---
    log_alerts, new_last_check = check_critical_logs(state)
    state["last_log_check"] = new_last_check
    if log_alerts:
        current_raw["critical_logs"] = log_alerts  # ALL lines, not just [0]
        print(f"[health-check] Critical logs: {len(log_alerts)} line(s)")

    # --- Mode changes ---
    # Bug 2 fix: mode_change is a CONDITION (mode != autonomous), not an event.
    # Keep it active as long as the mode file is NOT "autonomous".
    # check_mode_change() still runs to detect transitions (for logging) and
    # update last_mode, but the alert persistence is driven by the actual mode.
    mode_alerts, new_mode = check_mode_change(state)
    state["last_mode"] = new_mode
    if new_mode and new_mode != "autonomous":
        # Mode is not autonomous — always report it.
        msg = mode_alerts[0] if mode_alerts else (
            f"⚠️ Orchestrator mode is '{new_mode}' (not autonomous)."
        )
        current_raw["mode_change"] = [msg]
        if mode_alerts:
            print(f"[health-check] Mode change: {len(mode_alerts)} alert(s)")
        else:
            print(f"[health-check] Mode still '{new_mode}' — alert persists")

    # --- Transition-based dedup ---
    now = time.time()
    renotify_interval = 3600  # 1 hour
    new_active = {}

    for alert_type, messages in current_raw.items():
        # Content-hash each message for dedup (Bug 3 fix)
        msg_hashes = []
        for msg in messages:
            h = hashlib.md5(msg.encode()).hexdigest()[:12]
            msg_hashes.append((h, msg))

        if alert_type not in prev_alerts:
            # ---- Onset: new condition ----
            delivered_any = False
            for h, msg in msg_hashes:
                ok = send_webhook(msg)
                if ok:
                    delivered_any = True
            # Bug 1 fix: only record as "active" if at least one message was delivered.
            if delivered_any:
                new_active[alert_type] = {
                    "hashes": [h for h, _ in msg_hashes],
                    "last_notify": now,
                }
            else:
                # Delivery failed — leave out of active_alerts so it retries next cycle.
                print(f"[health-check] WARN: {alert_type} onset NOT recorded — webhook delivery failed")
        else:
            # ---- Already active: check for new messages + re-notify ----
            prev_data = prev_alerts[alert_type]
            prev_hashes = set(prev_data.get("hashes", []))
            last_notify = prev_data.get("last_notify", 0)

            new_msgs = [(h, msg) for h, msg in msg_hashes if h not in prev_hashes]
            delivered_new = False
            for h, msg in new_msgs:
                ok = send_webhook(msg)
                if ok:
                    delivered_new = True
                    prev_hashes.add(h)

            if new_msgs and delivered_new:
                new_active[alert_type] = {
                    "hashes": list(prev_hashes),
                    "last_notify": now,
                }
            elif now - last_notify > renotify_interval and msg_hashes:
                # Re-notify: resend first message so persistent conditions stay visible
                _, first_msg = msg_hashes[0]
                ok = send_webhook(first_msg)
                if ok:
                    new_active[alert_type] = {
                        "hashes": list(prev_hashes),
                        "last_notify": now,
                    }
                else:
                    # Keep old state on failure
                    new_active[alert_type] = prev_data
            else:
                # No change, no re-notify needed — carry forward
                new_active[alert_type] = prev_data

    # ---- Cleared: conditions that resolved ----
    for alert_type in prev_alerts:
        if alert_type not in current_raw:
            send_webhook(f"✅ [CLEARED] {alert_type.replace('_', ' ').title()} — condition resolved.")

    state["active_alerts"] = new_active
    save_state(state)

    if not current_raw:
        delivery_note = " Webhook alerts are disabled." if not webhook_configured else ""
        print(f"[health-check] All checks passed. "
              f"Heartbeat OK, no CRITICAL logs, mode stable.{delivery_note}")

if __name__ == "__main__":
    main()
