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