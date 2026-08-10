"""
Backend Alerting & Monitoring System for Educated Trades.

Detects 'Big Moves' and 'System Breaks', logs to the local alerts database,
and sends high-severity notifications to the lead.

Usage:
    from monitoring import AlertManager
    alerts = AlertManager()
    alerts.check_cycle(sentiment_conviction, consensus, errors, cycle_count)
"""

import logging
import hashlib
import os
from alert_db import insert_alert
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("monitor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How many seconds to wait before sending another alert of the same type
DEBOUNCE_SECONDS = 300  # 5 minutes

# Alert severity levels
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITY_ERROR = "error"

# Where the decision audit trail is written (append-only JSON Lines).
AUDIT_LOG_PATH = os.path.join(os.environ.get("DATA_DIR", "/home/team/shared/data"), "audit_log.jsonl")


# ---------------------------------------------------------------------------
# Decision Audit Logger
# ---------------------------------------------------------------------------
class DecisionAuditLogger:
    """
    Append-only, structured audit trail for every decision the bot makes.

    Writes one JSON object per line (JSON Lines / .jsonl) to AUDIT_LOG_PATH so
    that every cycle, signal, and order can be reconstructed after the fact —
    answering exactly *why* a trade was taken or skipped.

    Thread-safe: a single lock guards appends so the fast-track position
    monitor and the main pipeline thread never interleave a line.

    Event types written:
      - "cycle"            end-of-cycle context (headlines, sentiment, patterns,
                           portfolio, blended conviction, decision)
      - "order_submit"     an order was submitted (incl. bracket SL/TP params)
      - "order_result"     fill or rejection (incl. full broker response/error)
      - "position_close"   a tracked position was closed (stop/target)
      - "signal_skipped"   a candidate signal was evaluated but not executed
    """

    _instance: Optional["DecisionAuditLogger"] = None

    def __init__(self, path: str = AUDIT_LOG_PATH):
        import threading
        self.path = path
        self._lock = threading.Lock()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception as e:
            logger.error("Audit logger could not create dir: %s", e)

    @classmethod
    def instance(cls) -> "DecisionAuditLogger":
        """Process-wide singleton so all modules share one audit trail."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def log(self, event_type: str, data: Dict[str, Any]) -> None:
        """Append a single structured audit record as one JSON line."""
        import json
        record = {
            "ts": round(time.time(), 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event_type,
            **data,
        }
        try:
            line = json.dumps(record, default=str)
        except Exception as e:
            # Never let a serialisation problem break trading — degrade to a
            # minimal record noting the failure.
            line = json.dumps({
                "ts": round(time.time(), 3), "event": event_type,
                "audit_error": f"serialise_failed: {e}",
            })
        try:
            with self._lock:
                with open(self.path, "a") as f:
                    f.write(line + "\n")
        except Exception as e:
            logger.error("Audit log write failed (%s): %s", event_type, e)

    # -- Convenience wrappers -------------------------------------------
    def log_cycle(self, context: Dict[str, Any]) -> None:
        self.log("cycle", context)

    def log_order_submit(self, params: Dict[str, Any]) -> None:
        self.log("order_submit", params)

    def log_order_result(self, result: Dict[str, Any]) -> None:
        self.log("order_result", result)


# ---------------------------------------------------------------------------
# Alert Manager
# ---------------------------------------------------------------------------
class AlertManager:
    """
    Detects, logs, and notifies about important system events.
    """

    def __init__(self):
        self._last_alert_time: Dict[str, float] = {}
        self._last_sentiment: Optional[float] = None
        self._consecutive_errors = 0
        self._cycle_count = 0

    # ------------------------------------------------------------------
    # Alert Detection
    # ------------------------------------------------------------------
    def check_cycle(
        self,
        sentiment_conviction: float,
        consensus: str,
        errors: List[str],
        cycle_count: int,
        prev_conviction: Optional[float] = None,
    ) -> dict:
        """
        Run all alert checks for a pipeline cycle.

        Returns:
            Dict with triggered alerts summary.
        """
        self._cycle_count = cycle_count
        triggered: List[dict] = []

        # 1. Big Move: Sentiment swing > 0.4
        if prev_conviction is not None:
            swing = abs(sentiment_conviction - prev_conviction)
            if swing > 0.4:
                alert = self._create_alert(
                    alert_type="big_move",
                    severity=SEVERITY_WARNING,
                    message=(
                        f"Large sentiment swing detected: "
                        f"{prev_conviction:+.3f} → {sentiment_conviction:+.3f} "
                        f"(Δ={swing:.3f}, consensus={consensus}) "
                        f"in cycle #{cycle_count}"
                    ),
                )
                if alert:
                    triggered.append(alert)

        # 2. Strong conviction alert
        if abs(sentiment_conviction) > 0.6:
            direction = "BULLISH" if sentiment_conviction > 0 else "BEARISH"
            alert = self._create_alert(
                alert_type="strong_signal",
                severity=SEVERITY_INFO,
                message=(
                    f"Strong {direction} conviction: "
                    f"{sentiment_conviction:+.3f} (cycle #{cycle_count})"
                ),
            )
            if alert:
                triggered.append(alert)

        # 3. System Break: Consecutive errors
        if errors:
            # Count error CYCLES, not error instances. Adding len(errors) and
            # decrementing by one on a clean cycle made this a leaky
            # accumulator: alternating good/bad cycles climbed forever and
            # tripped "system break" on errors that were never consecutive.
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                error_msgs = "; ".join(errors[-3:])
                alert = self._create_alert(
                    alert_type="system_break",
                    severity=SEVERITY_ERROR,
                    message=(
                        f"System break: {self._consecutive_errors} consecutive "
                        f"errors in pipeline. Recent: {error_msgs[:200]}"
                    ),
                )
                if alert:
                    triggered.append(alert)
        else:
            # A clean cycle breaks the streak. That is what consecutive means.
            self._consecutive_errors = 0

        # 4. Prolonged neutral sentiment (stale market)
        if abs(sentiment_conviction) < 0.05 and cycle_count > 5:
            alert = self._create_alert(
                alert_type="stale_market",
                severity=SEVERITY_INFO,
                message=(
                    f"Extended neutral sentiment: {sentiment_conviction:+.4f} "
                    f"after {cycle_count} cycles"
                ),
            )
            if alert:
                triggered.append(alert)

        # Update stored sentiment
        self._last_sentiment = sentiment_conviction

        return {
            "triggered_count": len(triggered),
            "alerts": triggered,
            "consecutive_errors": self._consecutive_errors,
        }

    def check_exception(self, exception: Exception, context: str = "") -> dict:
        """
        Check an exception that occurred during pipeline execution.
        """
        alert = self._create_alert(
            alert_type="exception",
            severity=SEVERITY_ERROR,
            message=f"Exception in {context}: {exception}"[:300],
        )
        return {"triggered": alert is not None, "alert": alert}

    # ------------------------------------------------------------------
    # Alert Creation & Logging
    # ------------------------------------------------------------------
    def _create_alert(
        self, alert_type: str, severity: str, message: str,
    ) -> Optional[dict]:
        """
        Create an alert with debouncing, log to database, and notify
        the lead for high-severity alerts.
        """
        now = time.time()

        # Debounce on (type, message), not type alone. Keying on type meant
        # "unprotected_position: SPY" suppressed "unprotected_position: QQQ"
        # for five minutes -- distinct incidents collapsing into one.
        fingerprint = "%s|%s" % (alert_type, hashlib.sha1(
            message.encode("utf-8", "replace")).hexdigest()[:16])
        last_time = self._last_alert_time.get(fingerprint, 0)
        debounced = (now - last_time) < DEBOUNCE_SECONDS

        alert_data = {
            "type": alert_type,
            "severity": severity,
            "message": message,
            "timestamp": now,
            "debounced": debounced,
        }

        # ALWAYS persist. Debouncing exists to stop notification spam, not to
        # erase the record -- previously a debounced alert never reached the
        # database, so the evidence of a repeating fault disappeared with it.
        self._log_to_database(alert_type, message, severity)

        if debounced:
            logger.debug(
                "Alert '%s' notification debounced (%.0fs since identical), "
                "still recorded", alert_type, now - last_time,
            )
            return None

        self._last_alert_time[fingerprint] = now

        # Send notification for high-severity alerts
        if severity in (SEVERITY_CRITICAL, SEVERITY_ERROR):
            self._notify_lead(alert_type, message, severity)

        logger.info(
            "ALERT [%s] %s: %s", severity.upper(), alert_type, message[:80]
        )

        return alert_data

    def _log_to_database(
        self, alert_type: str, message: str, severity: str,
    ) -> None:
        """Insert an alert record into the local alerts database."""
        try:
            insert_alert(alert_type, message, severity)
        except Exception as e:
            logger.error("Failed to log alert: %s", e)

    def _notify_lead(
        self, alert_type: str, message: str, severity: str,
    ) -> None:
        """
        Send a notification to the lead for high-severity alerts.
        Uses the send_message tool (called by orchestrator).
        """
        try:
            from inspect import currentframe

            # We'll store the notification in a predictable location
            # that the orchestrator can pick up and send
            notification = {
                "type": alert_type,
                "severity": severity,
                "message": message[:200],
                "timestamp": time.time(),
            }

            # Write to a notification file that the orchestrator reads
            import json
            notif_dir = os.path.join(os.environ.get("DATA_DIR", "/home/team/shared/data"), "notifications")
            os.makedirs(notif_dir, exist_ok=True)
            # Second-resolution names collided: two critical alerts in the
            # same second meant one silently overwrote the other.
            notif_path = os.path.join(
                notif_dir,
                "alert_%.6f_%s.json" % (time.time(), uuid.uuid4().hex[:8]),
            )
            with open(notif_path, "w") as f:
                json.dump(notification, f)

            logger.info(
                "High-severity alert queued for lead: %s", message[:60]
            )
        except Exception as e:
            logger.error("Failed to queue notification: %s", e)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        """Return current monitoring status."""
        return {
            "alerts_sent": {
                k: round(time.time() - v, 1)
                for k, v in self._last_alert_time.items()
            },
            "consecutive_errors": self._consecutive_errors,
            "cycle_count": self._cycle_count,
            "last_sentiment": self._last_sentiment,
        }


# ---------------------------------------------------------------------------
# Convenience: check and forward pending notifications
# ---------------------------------------------------------------------------
def send_pending_notifications() -> List[str]:
    """
    Check for pending notification files and send them to the lead.
    Called by the orchestrator at the end of each cycle.
    """
    import glob
    import json

    notif_dir = os.path.join(os.environ.get("DATA_DIR", "/home/team/shared/data"), "notifications")
    if not os.path.exists(notif_dir):
        return []

    sent: List[str] = []
    for fpath in sorted(glob.glob(os.path.join(notif_dir, "alert_*.json"))):
        try:
            with open(fpath) as f:
                notification = json.load(f)

            # Build message for the lead
            msg = (
                f"[{notification['severity'].upper()}] "
                f"Alert: {notification['type']}\n"
                f"Message: {notification['message'][:200]}"
            )

            # Send to lead via a flag file (orchestrator reads this)
            flag_path = os.path.join(os.environ.get("DATA_DIR", "/home/team/shared/data"), "send_lead_message.txt")
            with open(flag_path, "a") as f:
                f.write(
                    f"{notification['timestamp']}|"
                    f"{notification['severity']}|"
                    f"{notification['message'][:200]}\n"
                )

            sent.append(notification["type"])
            os.remove(fpath)
        except Exception as e:
            logger.error("Failed to process notification: %s", e)

    return sent