#!/usr/bin/env python3
"""Heartbeat watchdog — alerts if the orchestrator stops writing heartbeats.

Runs as a SEPARATE PROCESS from the orchestrator (watchdog_loop.sh, every
60s), which is the whole point: a watchdog inside the thing it watches cannot
report that the thing has died. But being a separate process is also what
made it fragile, and three of its four failures came from that:

  * It read DATA_DIR from the environment. main.py derived the segregated
    paper/live directory but kept it in a module global, so main wrote the
    heartbeat to $DATA_ROOT/paper while this looked in /home/team/shared/data
    and reported "orchestrator may not be running" every 60 seconds forever.
    main.py now exports DATA_DIR, which this inherits.

  * It reimplemented market hours with a hardcoded UTC-5 offset for Central.
    Chicago is UTC-6 in winter, so from November to March the window was an
    hour out: it alerted for an hour before the open and went silent for the
    last hour of the session. It also knew nothing about holidays or half
    days, so every early close produced three hours of false criticals. It
    now asks market_clock, the same authority the orchestrator uses.

  * A missing heartbeat file alerted regardless of market hours, so a bot
    that is legitimately not running overnight generated an alert a minute
    all night.

  * Nothing was debounced. A real outage produced one Discord message per
    minute until someone noticed -- which is how an alert channel becomes
    something people mute.

Usage:
    WATCHDOG_DISCORD_WEBHOOK=https://discord.com/api/webhooks/... python3 watchdog.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/home/team/shared/data"))
HEARTBEAT_PATH = DATA_DIR / "heartbeat"
#: Debounce state. Lives beside the heartbeat so it is segregated too.
ALERT_STATE_PATH = DATA_DIR / "watchdog_alert_state.json"

STALE_SECONDS = int(os.environ.get("WATCHDOG_STALE_SECONDS", "300"))
#: Do not repeat the same alert more often than this. An outage lasting an
#: hour should produce a handful of messages, not sixty.
REPEAT_SECONDS = int(os.environ.get("WATCHDOG_REPEAT_SECONDS", "900"))
DISCORD_WEBHOOK = os.environ.get("WATCHDOG_DISCORD_WEBHOOK", "")


def is_market_hours(now=None) -> bool:
    """True during a real trading session, using the orchestrator's clock.

    Deliberately NOT reimplemented here. market_clock knows the holiday
    calendar, the half-day calendar and the actual DST rules; a second
    approximation living in the watchdog is a second thing to get wrong, and
    it was wrong. If it cannot be imported, fail toward alerting: a spurious
    alert is recoverable, a silent watchdog is not.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from market_clock import MarketClock
        return MarketClock().is_open(now)
    except Exception as exc:  # pragma: no cover - import-environment specific
        print("[watchdog] WARN: market_clock unavailable (%s); "
              "assuming market hours" % exc)
        return True


def get_heartbeat_age():
    """Seconds since the last heartbeat, or None if it cannot be determined."""
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        data = json.loads(HEARTBEAT_PATH.read_text())
        ts = data.get("timestamp")
        if ts is None:
            return None
        # A malformed timestamp used to raise TypeError, which was not in the
        # except clause, so the watchdog crashed instead of reporting.
        return time.time() - float(ts)
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
        return None


def _should_send(key: str) -> bool:
    """Debounce: True if this alert has not fired recently."""
    now = time.time()
    state = {}
    try:
        if ALERT_STATE_PATH.exists():
            state = json.loads(ALERT_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        state = {}
    last = state.get(key)
    if isinstance(last, (int, float)) and now - last < REPEAT_SECONDS:
        return False
    state[key] = now
    try:
        ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(ALERT_STATE_PATH) + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(state, handle)
        os.replace(tmp, ALERT_STATE_PATH)
    except OSError as exc:
        # Losing the debounce record means the next run alerts again. Noisy,
        # but never silent -- the safe direction for a watchdog.
        print("[watchdog] WARN: could not persist debounce state: %s" % exc)
    return True


def clear_alert_state() -> None:
    """Forget the debounce record so recovery re-arms the next alert."""
    try:
        if ALERT_STATE_PATH.exists():
            ALERT_STATE_PATH.unlink()
    except OSError:
        pass


def send_discord_alert(message: str, key: str = "default") -> bool:
    """Send to Discord, at most once per REPEAT_SECONDS per key."""
    if not _should_send(key):
        print("[watchdog] suppressed (already alerted within %ds): %s"
              % (REPEAT_SECONDS, key))
        return False
    if not DISCORD_WEBHOOK:
        print("[watchdog] WARN: No DISCORD_WEBHOOK set. Would alert: %s" % message)
        return False
    try:
        import urllib.request
        payload = json.dumps({"content": message}).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception as exc:
        print("[watchdog] ERROR: Failed to send Discord alert: %s" % exc)
        return False


def main() -> int:
    age = get_heartbeat_age()
    open_now = is_market_hours()

    if age is None:
        # Outside a session this is the normal resting state of a bot that is
        # not running, not an incident. Alerting anyway produced a message a
        # minute all night and taught everyone to ignore the channel.
        if not open_now:
            print("[watchdog] No heartbeat, but the market is closed — no alert")
            return 0
        msg = ("Educated Trades: no readable heartbeat at %s during market "
               "hours — the orchestrator may not be running."
               % HEARTBEAT_PATH)
        print("[watchdog] CRITICAL: %s" % msg)
        send_discord_alert(msg, key="missing")
        return 1

    if age < STALE_SECONDS:
        print("[watchdog] OK: heartbeat is %.0fs old (threshold %ds)"
              % (age, STALE_SECONDS))
        clear_alert_state()
        return 0

    if not open_now:
        print("[watchdog] Heartbeat stale (%.0fs) but outside market hours "
              "— no alert" % age)
        return 0

    msg = ("Educated Trades: heartbeat stale %.0fs (threshold %ds) at %s. "
           "Market is open — possible crash or freeze."
           % (age, STALE_SECONDS,
              datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    print("[watchdog] CRITICAL: %s" % msg)
    send_discord_alert(msg, key="stale")
    return 1


if __name__ == "__main__":
    sys.exit(main())
