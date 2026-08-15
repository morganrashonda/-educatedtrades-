"""
Shared alert database module.

Replaces all team-db subprocess calls with direct sqlite3 writes to
a local database file under DATA_DIR.  No external binaries needed.
"""

import logging
import os
import sqlite3
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "alerts.db")

# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    level       TEXT    NOT NULL,
    alert_type  TEXT    NOT NULL,
    message     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_level     ON alerts(level);
"""

# Thread-local connections for safety
_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    """Get a thread-local connection, creating it and the DB if needed."""
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
        _local.conn = conn
    return _local.conn


def init_alert_db() -> None:
    """Ensure the alerts table exists. Safe to call repeatedly."""
    _get_connection()


#: Alerts at or above this level are pushed off the machine.
_ESCALATE = frozenset({"critical", "error"})
#: Do not send the same alert type more often than this (seconds). One
#: repeating fault should not produce a message every cycle -- that is how an
#: alert channel becomes something you mute, which is worse than no alerts.
_REPEAT_S = int(os.environ.get("ALERT_REPEAT_SECONDS", "900"))
_last_sent: dict = {}
_notify_guard = threading.Lock()


def _webhook_url() -> str:
    """Where critical alerts go. Falls back to the watchdog's webhook."""
    return (os.environ.get("ALERT_WEBHOOK_URL", "")
            or os.environ.get("WATCHDOG_DISCORD_WEBHOOK", "")).strip()


def _push(level: str, alert_type: str, message: str) -> None:
    """POST an alert to the webhook. Runs on its own thread; never raises.

    Deliberately fire-and-forget on a daemon thread. A blocking network call
    in the alerting path would put the trading loop at the mercy of Discord's
    availability -- and the moment you most need an alert is the moment
    something is already wrong, which is the worst time to add a new way to
    hang.
    """
    url = _webhook_url()
    if not url:
        return
    try:
        import json as _json
        import urllib.request

        text = "**%s** `%s`\n%s" % (level.upper(), alert_type, message[:1500])
        payload = _json.dumps({"content": text}).encode("utf-8")
        request = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(request, timeout=10).close()
    except Exception as exc:                      # noqa: BLE001
        # An unreachable webhook must never break the caller. The alert is
        # already in the database; this is the redundant copy.
        logger.warning("Could not push %s alert off-machine: %s", level, exc)


def _maybe_escalate(level: str, alert_type: str, message: str) -> None:
    """Send critical and error alerts off the machine, debounced per type."""
    if str(level).lower() not in _ESCALATE:
        return
    now = time.time()
    with _notify_guard:
        last = _last_sent.get(alert_type, 0.0)
        if now - last < _REPEAT_S:
            return
        _last_sent[alert_type] = now
    threading.Thread(target=_push, args=(level, alert_type, message),
                     daemon=True).start()


def insert_alert(alert_type: str, message: str, level: str = "info") -> None:
    """
    Insert a single alert row.

    Parameters
    ----------
    alert_type : str
        Short category label (e.g. 'KILL_SWITCH_TRIGGERED', 'backup_failure').
    message : str
        Free-form detail message.
    level : str
        Severity: 'debug', 'info', 'warning', 'error', 'critical'.
    """
    try:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO alerts (timestamp, level, alert_type, message) VALUES (?, ?, ?, ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), level, alert_type, message),
        )
        conn.commit()
        logger.debug("Alert written to %s: [%s] %s", DB_PATH, level, alert_type)
    except Exception as e:
        logger.error("Failed to write alert to %s: %s", DB_PATH, e)
    finally:
        # In `finally` on purpose: if the DATABASE write failed, that is
        # exactly when you most want the alert to leave the machine.
        _maybe_escalate(level, alert_type, message)


def get_recent_alerts(limit: int = 20) -> list:
    """
    Return the most recent alerts as a list of dicts.
    """
    try:
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Failed to read alerts from %s: %s", DB_PATH, e)
        return []


def get_latest_alert_by_type(alert_type: str) -> Optional[dict]:
    """
    Return the most recent alert of a given type, or None if none exist.
    """
    try:
        conn = _get_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM alerts WHERE alert_type = ? ORDER BY id DESC LIMIT 1",
            (alert_type,),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("Failed to read alert type '%s' from %s: %s", alert_type, DB_PATH, e)
        return None