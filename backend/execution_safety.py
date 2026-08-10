"""Broker-authoritative execution safety coordinator.

The ledger is an atomic JSON document (separate from pattern learning data) so
order intent survives process restarts without touching a live database.

Adapter contract -- intentionally tiny:
    submit_order(**kwargs)          -> order-like result
    get_order(order_id)             -> order-like result
    get_position(symbol)            -> position-like result, or None when flat
    get_order_by_client_id(key)     -> optional; enables recovery of reserved
                                       intents whose submit was interrupted

No adapter call is ever retried automatically after an ambiguous submission.
The caller must resolve ambiguity explicitly.

Concurrency: each THREAD gets its own SQLite connection, and every mutation
commits with a compare-and-set on a per-record revision counter, so a slow
writer can never overwrite a newer, more authoritative record.

This docstring used to credit an advisory file lock for serialisation. It was
wrong twice over: nothing called the lock, and fcntl locks are per-PROCESS, so
they would not have serialised the threads that actually contend here -- the
pipeline, the position monitor and the API. Meanwhile a single connection was
shared by all of them. Forty concurrent submits lost four orders. Correctness
across threads now comes from per-thread connections plus WAL; correctness
across processes comes from the revision CAS. Running two HOSTS against one
ledger is still not supported.

Platform: flock is POSIX-only. This module does not run on Windows.
"""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
import inspect
from dataclasses import asdict, dataclass, fields
from types import MappingProxyType
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExecutionRecord:
    client_order_key: str
    symbol: str
    side: str
    quantity: int
    status: str = "submitted"
    broker_order_id: Optional[str] = None
    filled_qty: int = 0
    filled_price: Optional[float] = None
    error: Optional[str] = None
    updated_at: float = 0.0
    revision: int = 0
    #: When the intent was first recorded. Distinct from updated_at, which
    #: moves on every reconcile -- so only this can answer "when did we last
    #: try to trade this symbol".
    created_at: float = 0.0
    #: True for liquidating orders. Tracked separately so an unresolved ENTRY
    #: can never block an EXIT -- refusing to let a position out is far more
    #: dangerous than refusing to let a new one in.
    is_exit: bool = False


class LedgerUnreadable(RuntimeError):
    """The ledger exists but cannot be trusted. Never treated as 'empty'."""


class ExecutionSafety:
    """Idempotent submission and conservative broker reconciliation.

    Storage is SQLite. The previous implementation kept the whole ledger in a
    single JSON document and rewrote it -- with fsync -- on every mutation, so
    submit latency grew linearly with every order ever placed (10ms at 50
    orders, 52ms at 800, and no pruning). That is invisible at 60 orders a
    year and fatal at intraday frequency. Here the hot queries are indexed and
    cost is flat regardless of how much history is retained.

    The public API is unchanged. _read()/_write() remain as a compatibility
    layer for callers and tests that want the whole document.
    """

    _TERMINAL = frozenset({"filled", "cancelled", "rejected", "refused"})
    _PENDING = frozenset({
        "reserved", "pending", "submitted", "exit_pending",
        "ambiguous", "partial", "residual",
    })
    _VOCABULARY = _TERMINAL | _PENDING

    _STATUS_MAP = MappingProxyType({
        "filled": "filled",
        "canceled": "cancelled", "cancelled": "cancelled",
        "rejected": "rejected", "refused": "refused",
        "expired": "cancelled",
        "new": "submitted", "accepted": "submitted", "submitted": "submitted",
        "accepted_for_bidding": "submitted",
        "pending_new": "pending", "pending_cancel": "pending",
        "pending_replace": "pending", "pending_review": "pending",
        "stopped": "pending", "replaced": "pending", "suspended": "pending",
        "done_for_day": "pending", "held": "pending", "calculated": "pending",
        "partially_filled": "partial",
        "reserved": "reserved", "pending": "pending", "ambiguous": "ambiguous",
        "partial": "partial", "exit_pending": "exit_pending", "residual": "residual",
    })

    assert set(_STATUS_MAP.values()) <= _VOCABULARY, "status map escapes vocabulary"
    assert not (_TERMINAL & _PENDING), "terminal and pending must not overlap"

    _COLUMNS = ("client_order_key", "symbol", "side", "quantity", "status",
                "broker_order_id", "filled_qty", "filled_price", "error",
                "updated_at", "revision", "is_exit", "created_at")

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock_path = "%s.lock" % path
        self._kill_path = "%s.kill" % path
        #: Connections are per-thread. A single shared connection lost
        #: orders under concurrency; see _connect().
        self._local = threading.local()
        self._conn: Optional[sqlite3.Connection] = None
        # An unreadable ledger must NOT stop construction: the kill switch has
        # to remain usable precisely when the ledger cannot be trusted. The
        # error is raised when a ledger operation is actually attempted.
        self._unreadable: Optional[str] = None
        try:
            self._ensure_schema()
        except LedgerUnreadable as exc:
            self._unreadable = str(exc)
            logger.critical(
                "Execution ledger at %s is unreadable (%s). Order operations "
                "will fail closed; the kill switch remains available.",
                path, exc,
            )

    # ------------------------------------------------------------------ db
    def _connect(self) -> sqlite3.Connection:
        """A connection owned by the calling thread.

        This used to hand every thread the SAME sqlite3.Connection, opened
        with check_same_thread=False and guarded by nothing. SQLite's own
        locking does not help there: the implicit-transaction state lives on
        the connection, so two threads issuing statements on one connection
        corrupt each other's transaction bookkeeping.

        Measured before this change: 40 concurrent submits produced 12
        exceptions -- "cannot start a transaction within a transaction",
        "cannot commit - no transaction is active", "no more rows available"
        -- and only 36 of 40 orders reached the ledger. FOUR ORDERS VANISHED
        from the record that exists specifically so no order can be lost.
        The pipeline thread, the 15-second position monitor and the API thread
        all touch this.

        The module docstring credited an advisory file lock for serialisation.
        fcntl locks are per-PROCESS; they do nothing between threads inside
        one. Per-thread connections plus WAL is what SQLite actually wants,
        and the compare-and-set on `revision` already makes cross-connection
        writes safe.
        """
        if self._unreadable:
            raise LedgerUnreadable(self._unreadable)
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        legacy = self._legacy_json()
        try:
            conn = sqlite3.connect(self.path, timeout=30.0,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError as exc:
            if legacy is None:
                # Neither a database nor a readable legacy ledger. An
                # unreadable ledger is not an empty one.
                raise LedgerUnreadable(
                    "execution ledger unreadable; refusing fail-open reset") from exc
            raise
        self._local.conn = conn
        self._conn = conn   # newest connection, for compatibility
        return conn

    def _legacy_json(self) -> Optional[dict]:
        """Parse a pre-SQLite JSON ledger, if that is what sits at `path`."""
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as stream:
                data = json.load(stream)
        except (ValueError, UnicodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _ensure_schema(self) -> None:
        legacy = self._legacy_json()
        if legacy is not None:
            # Migrate the old document, then move it aside so this only runs once.
            os.replace(self.path, self.path + ".migrated")
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                client_order_key TEXT PRIMARY KEY,
                symbol           TEXT NOT NULL,
                side             TEXT NOT NULL,
                quantity         INTEGER NOT NULL,
                status           TEXT NOT NULL,
                broker_order_id  TEXT,
                filled_qty       INTEGER DEFAULT 0,
                filled_price     REAL,
                error            TEXT,
                updated_at       REAL DEFAULT 0.0,
                revision         INTEGER DEFAULT 0,
                is_exit          INTEGER DEFAULT 0,
                created_at       REAL DEFAULT 0.0
            );
            CREATE INDEX IF NOT EXISTS idx_orders_symbol_side
                ON orders(symbol, side, status);
            CREATE INDEX IF NOT EXISTS idx_orders_status
                ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_exit
                ON orders(symbol, is_exit, status);
            CREATE INDEX IF NOT EXISTS idx_orders_updated
                ON orders(updated_at);
            CREATE INDEX IF NOT EXISTS idx_orders_created
                ON orders(symbol, is_exit, created_at);
            CREATE TABLE IF NOT EXISTS orders_archive (
                client_order_key TEXT PRIMARY KEY,
                symbol TEXT, side TEXT, quantity INTEGER, status TEXT,
                broker_order_id TEXT, filled_qty INTEGER, filled_price REAL,
                error TEXT, updated_at REAL, revision INTEGER, is_exit INTEGER,
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        try:
            conn.execute("ALTER TABLE orders ADD COLUMN created_at REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass  # already present
        try:
            conn.execute(
                "ALTER TABLE orders_archive ADD COLUMN created_at REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        if legacy is not None:
            for raw in (legacy.get("orders") or {}).values():
                try:
                    self._upsert(self._record_from_raw(raw))
                except Exception:
                    continue
            if legacy.get("kill"):
                self.set_kill(True)
            logger.info("Migrated %d order(s) from the legacy JSON ledger",
                        len(legacy.get("orders") or {}))

    # -------------------------------------------------------------- status
    @staticmethod
    def _status_value(status: Any) -> str:
        value = getattr(status, "value", status)
        return str(value).strip().lower()

    @classmethod
    def _normalize_status(cls, status: Any) -> str:
        return cls._STATUS_MAP.get(cls._status_value(status), "pending")

    @classmethod
    def _is_terminal(cls, status: Any) -> bool:
        return cls._normalize_status(status) in cls._TERMINAL

    # -------------------------------------------------------------- record
    @staticmethod
    def _coerce_price(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return price if math.isfinite(price) and price > 0 else None

    @classmethod
    def _record_from_raw(cls, raw: Any) -> ExecutionRecord:
        if isinstance(raw, sqlite3.Row):
            raw = dict(raw)
        if not isinstance(raw, dict):
            raise ValueError("invalid execution record")
        known = {f.name for f in fields(ExecutionRecord)}
        values = {k: v for k, v in raw.items() if k in known}
        for key, default in (("broker_order_id", None), ("filled_qty", 0),
                             ("filled_price", None), ("error", None),
                             ("updated_at", 0.0), ("revision", 0),
                             ("is_exit", False), ("created_at", 0.0)):
            values.setdefault(key, default)
        values.setdefault("status", "submitted")
        values["status"] = cls._normalize_status(values["status"])
        values["is_exit"] = bool(values["is_exit"])
        try:
            values["quantity"] = int(values["quantity"])
            values["filled_qty"] = max(0, int(values["filled_qty"] or 0))
            values["revision"] = max(0, int(values["revision"] or 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid execution record schema") from exc
        values["filled_price"] = cls._coerce_price(values.get("filled_price"))
        return ExecutionRecord(**values)

    @staticmethod
    def _complete_fill(quantity: int, filled_qty: Any, filled_price: Any) -> bool:
        try:
            price = float(filled_price)
            return int(filled_qty or 0) >= quantity and math.isfinite(price) and price > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _accepts_client_key(func: Any) -> bool:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return False
        for parameter in signature.parameters.values():
            if parameter.name == "client_order_key":
                return True
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                return True
        return False

    @staticmethod
    def _valid_quantity(quantity: int) -> bool:
        return isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0

    # ------------------------------------------------------------------ io
    def _upsert(self, rec: ExecutionRecord) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT INTO orders (%s) VALUES (%s) "
            "ON CONFLICT(client_order_key) DO UPDATE SET %s"
            % (",".join(self._COLUMNS),
               ",".join("?" * len(self._COLUMNS)),
               ",".join("%s=excluded.%s" % (c, c) for c in self._COLUMNS[1:])),
            tuple(getattr(rec, c) if c != "is_exit" else int(rec.is_exit)
                  for c in self._COLUMNS),
        )
        conn.commit()

    def _fetch(self, client_order_key: str) -> Optional[ExecutionRecord]:
        row = self._connect().execute(
            "SELECT * FROM orders WHERE client_order_key=?",
            (client_order_key,)).fetchone()
        return self._record_from_raw(row) if row else None

    def _read(self) -> dict[str, Any]:
        """Whole-document view. Compatibility only -- prefer indexed queries."""
        conn = self._connect()
        orders = {}
        for row in conn.execute("SELECT * FROM orders"):
            record = dict(row)
            record["is_exit"] = bool(record["is_exit"])
            orders[record["client_order_key"]] = record
        return {"orders": orders, "kill": self._kill_engaged()}

    def _write(self, data: dict[str, Any]) -> None:
        """Whole-document write. Compatibility only."""
        conn = self._connect()
        keys = set()
        for key, raw in (data.get("orders") or {}).items():
            raw = dict(raw)
            raw.setdefault("client_order_key", key)
            self._upsert(self._record_from_raw(raw))
            keys.add(key)
        if keys:
            placeholders = ",".join("?" * len(keys))
            conn.execute(
                "DELETE FROM orders WHERE client_order_key NOT IN (%s)" % placeholders,
                tuple(keys))
        else:
            conn.execute("DELETE FROM orders")
        conn.commit()
        if "kill" in data:
            self.set_kill(bool(data["kill"]))

    @contextmanager
    def _locked(self):
        """Advisory file lock. Retained for callers that coordinate externally.

        SQLite provides its own transactional locking, so ledger correctness no
        longer depends on this.
        """
        handle = open(self._lock_path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield handle
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    # -------------------------------------------------------------- commit
    def _commit(self, rec: ExecutionRecord, expected_revision: int) -> ExecutionRecord:
        """Compare-and-set on revision, as a single atomic UPDATE."""
        conn = self._connect()
        rec.updated_at = time.time()
        cursor = conn.execute(
            "UPDATE orders SET status=?, broker_order_id=?, filled_qty=?, "
            "filled_price=?, error=?, updated_at=?, revision=?, is_exit=? "
            "WHERE client_order_key=? AND revision=?",
            (rec.status, rec.broker_order_id, rec.filled_qty, rec.filled_price,
             rec.error, rec.updated_at, expected_revision + 1, int(rec.is_exit),
             rec.client_order_key, expected_revision))
        conn.commit()
        if cursor.rowcount:
            rec.revision = expected_revision + 1
            return rec
        current = self._fetch(rec.client_order_key)
        if current is None:
            rec.revision = expected_revision + 1
            self._upsert(rec)
            return rec
        # Someone advanced the record while we were talking to the broker.
        # Only backfill a broker id they are missing; never erase their result.
        if current.broker_order_id is None and rec.broker_order_id is not None:
            current.broker_order_id = rec.broker_order_id
            current.revision += 1
            current.updated_at = time.time()
            self._upsert(current)
        return current

    # --------------------------------------------------------- kill switch
    def _kill_engaged(self, data: Optional[dict[str, Any]] = None) -> bool:
        if os.path.exists(self._kill_path):
            return True
        if isinstance(data, dict) and data.get("kill"):
            return True
        if self._unreadable:
            return False
        try:
            row = self._connect().execute(
                "SELECT value FROM meta WHERE key='kill'").fetchone()
        except (sqlite3.DatabaseError, LedgerUnreadable):
            return False
        return bool(row and row["value"] == "1")

    def set_kill(self, enabled: bool) -> None:
        """Engage/release the kill switch.

        Engaging must work even when the ledger is unreadable, so the flag is a
        standalone sentinel file as well as a row.
        """
        if enabled:
            with open(self._kill_path, "w") as stream:
                stream.write(str(time.time()))
                stream.flush()
                os.fsync(stream.fileno())
        elif os.path.exists(self._kill_path):
            os.unlink(self._kill_path)
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO meta (key,value) VALUES ('kill',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if enabled else "0",))
            conn.commit()
        except Exception:
            return  # sentinel already written; ledger repair is separate

    def kill_engaged(self) -> bool:
        return self._kill_engaged()

    # -------------------------------------------------------------- submit
    def submit(self, adapter: Any, *, client_order_key: str, symbol: str,
               side: str, quantity: int, **kwargs: Any) -> ExecutionRecord:
        if not client_order_key or not symbol or side not in {"buy", "sell"} or not self._valid_quantity(quantity):
            raise ValueError("invalid order identity, side, or quantity")

        conn = self._connect()
        existing = self._fetch(client_order_key)
        if existing:
            return existing
        now = time.time()
        if self._kill_engaged():
            rec = ExecutionRecord(client_order_key, symbol, side, quantity,
                                  "refused", error="kill_switch",
                                  updated_at=now, revision=1, created_at=now)
            self._upsert(rec)
            return rec

        rec = ExecutionRecord(client_order_key, symbol, side, quantity,
                              "reserved", updated_at=now, revision=1, created_at=now)
        try:
            # Atomic reservation: a concurrent worker that loses this race sees
            # the row and returns it rather than submitting a second order.
            conn.execute(
                "INSERT INTO orders (%s) VALUES (%s)"
                % (",".join(self._COLUMNS), ",".join("?" * len(self._COLUMNS))),
                tuple(getattr(rec, c) if c != "is_exit" else int(rec.is_exit)
                      for c in self._COLUMNS))
            conn.commit()
        except sqlite3.IntegrityError:
            # Release the write lock the failed INSERT is still holding.
            # Without this the aborted transaction stays open on this
            # connection, and the thread that WON the race then cannot commit
            # its own update -- "database is locked" -- leaving the order
            # stuck at `reserved` even though the broker accepted it. It never
            # showed while every thread shared one connection.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            current = self._fetch(client_order_key)
            if current is not None:
                return current

        reserved_revision = rec.revision
        try:
            submit = getattr(adapter, "submit_order", None) or getattr(adapter, "execute_order")
            call_kwargs = dict(kwargs)
            if self._accepts_client_key(submit):
                call_kwargs["client_order_key"] = client_order_key
            result = submit(symbol=symbol, side=side, quantity=quantity, **call_kwargs)
            broker_id = getattr(result, "id", getattr(result, "order_id", None))
            rec.broker_order_id = str(broker_id) if broker_id is not None and str(broker_id) else None
            rec.filled_qty = max(0, int(getattr(result, "filled_qty", 0) or 0))
            rec.filled_price = self._coerce_price(getattr(result, "filled_price", None))
            if self._complete_fill(rec.quantity, rec.filled_qty, rec.filled_price):
                rec.status = "filled"
            elif rec.broker_order_id is None:
                rec.status = "ambiguous"
            else:
                rec.status = "submitted"
        except Exception as exc:
            rec.status, rec.error = "ambiguous", str(exc)
        return self._commit(rec, reserved_revision)

    # ----------------------------------------------------------- reconcile
    def reconcile(self, adapter: Any, client_order_key: str) -> ExecutionRecord:
        rec = self._fetch(client_order_key)
        if rec is None:
            raise KeyError(client_order_key)
        expected_revision = rec.revision

        if not rec.broker_order_id:
            resolved = self._resolve_reserved(adapter, rec)
            if resolved is None:
                return rec
            rec = resolved
            if rec.status in self._TERMINAL or not rec.broker_order_id:
                return self._commit(rec, expected_revision)

        try:
            order = adapter.get_order(rec.broker_order_id)
            filled_qty = max(0, int(getattr(order, "filled_qty", 0) or 0))
            filled_price = self._coerce_price(getattr(order, "filled_avg_price", None))
            normalized = self._normalize_status(getattr(order, "status", rec.status))
            if self._complete_fill(rec.quantity, filled_qty, filled_price):
                rec.status = "filled"
            elif filled_qty > 0 or normalized == "partial":
                rec.status = "partial"
            else:
                rec.status = normalized
            rec.filled_qty, rec.filled_price, rec.error = filled_qty, filled_price, None
        except Exception as exc:
            rec = self._record_from_raw(asdict(rec))
            rec.status, rec.error = "ambiguous", "broker order query failed: %s" % exc
        return self._commit(rec, expected_revision)

    def _resolve_reserved(self, adapter: Any, rec: ExecutionRecord) -> Optional[ExecutionRecord]:
        """Recover an intent whose submit was interrupted before commit."""
        lookup = getattr(adapter, "get_order_by_client_id", None)
        if rec.status != "reserved" or lookup is None:
            return None
        try:
            order = lookup(rec.client_order_key)
        except Exception:
            return None
        if order is None:
            rec.status, rec.error = "cancelled", "reserved_intent_never_reached_broker"
            return rec
        broker_id = getattr(order, "id", getattr(order, "order_id", None))
        rec.broker_order_id = str(broker_id) if broker_id is not None and str(broker_id) else None
        if rec.broker_order_id is None:
            return None
        return rec

    def reconcile_all(self, adapter: Any) -> list[ExecutionRecord]:
        """Reconcile every unresolved order. Indexed, so cost tracks OPEN
        orders rather than total history."""
        if not hasattr(adapter, "get_order"):
            return []
        placeholders = ",".join("?" * len(self._TERMINAL))
        rows = self._connect().execute(
            "SELECT client_order_key FROM orders "
            "WHERE status NOT IN (%s) AND (broker_order_id IS NOT NULL "
            "OR status='reserved')" % placeholders,
            tuple(self._TERMINAL)).fetchall()
        records = []
        for row in rows:
            try:
                records.append(self.reconcile(adapter, row["client_order_key"]))
            except KeyError:
                continue
        return records

    # ----------------------------------------------------------- positions
    def position_state(self, adapter: Any, symbol: str) -> str:
        """Return 'flat', 'open', or 'unknown'."""
        try:
            pos = adapter.get_position(symbol)
        except Exception:
            return "unknown"
        if pos is None:
            return "flat"
        qty = pos.get("qty", 0) if isinstance(pos, dict) else getattr(pos, "qty", 0)
        try:
            value = float(qty)
        except (TypeError, ValueError):
            return "unknown"
        if not math.isfinite(value):
            return "unknown"
        return "flat" if value == 0 else "open"

    def broker_flat(self, adapter: Any, symbol: str) -> bool:
        return self.position_state(adapter, symbol) == "flat"

    def reconcile_flat(self, adapter: Any, client_order_key: str) -> ExecutionRecord:
        rec = self.reconcile(adapter, client_order_key)
        if rec.status != "filled":
            return rec
        state = self.position_state(adapter, rec.symbol)
        if state == "flat":
            return rec
        current = self._fetch(client_order_key)
        if current is None:
            return rec
        current.status = "residual"
        current.error = ("broker_position_not_flat" if state == "open"
                         else "broker_position_unknown")
        return self._commit(current, current.revision)

    # ------------------------------------------------------------- queries
    def _exists(self, sql: str, params: tuple) -> bool:
        return self._connect().execute(sql, params).fetchone() is not None

    def has_open_exposure(self, symbol: str, side: str) -> bool:
        """Indexed: cost does not grow with history."""
        placeholders = ",".join("?" * len(self._TERMINAL))
        return self._exists(
            "SELECT 1 FROM orders WHERE symbol=? AND side=? "
            "AND status NOT IN (%s) LIMIT 1" % placeholders,
            (symbol, side) + tuple(self._TERMINAL))

    def has_pending_exit(self, symbol: str) -> bool:
        placeholders = ",".join("?" * len(self._TERMINAL))
        return self._exists(
            "SELECT 1 FROM orders WHERE symbol=? AND status NOT IN (%s) LIMIT 1"
            % placeholders, (symbol,) + tuple(self._TERMINAL))

    def has_unresolved_exit(self, symbol: str) -> bool:
        """Liquidating orders only: an unresolved ENTRY must never block an EXIT."""
        placeholders = ",".join("?" * len(self._TERMINAL))
        return self._exists(
            "SELECT 1 FROM orders WHERE symbol=? AND is_exit=1 "
            "AND status NOT IN (%s) LIMIT 1" % placeholders,
            (symbol,) + tuple(self._TERMINAL))

    def last_entry_time(self, symbol: str) -> Optional[float]:
        """When an ENTRY for this symbol was last submitted, or None.

        The per-symbol cooldown lived only in memory, so a restart cleared it
        and the bot could immediately re-enter a symbol it had just traded.
        The ledger already knows this; there is no need for a second store.
        Exits are excluded -- closing a position must not delay re-entry.
        """
        row = self._connect().execute(
            "SELECT MAX(created_at) AS t FROM orders "
            "WHERE symbol=? AND is_exit=0", (symbol,)).fetchone()
        if row is None or not row["t"]:
            return None
        return float(row["t"])

    def symbols(self) -> list:
        rows = self._connect().execute(
            "SELECT DISTINCT symbol FROM orders").fetchall()
        return sorted(r["symbol"] for r in rows if r["symbol"])

    def register_exit(self, *, client_order_key: str, symbol: str, side: str, quantity: int,
                      broker_order_id: Optional[str] = None) -> ExecutionRecord:
        if not client_order_key or not symbol or side not in {"buy", "sell"} or not self._valid_quantity(quantity):
            raise ValueError("invalid exit identity, side, or quantity")
        existing = self._fetch(client_order_key)
        if existing:
            return existing
        rec = ExecutionRecord(client_order_key, symbol, side, quantity, "exit_pending",
                              broker_order_id=broker_order_id, updated_at=time.time(),
                              revision=1, is_exit=True, created_at=time.time())
        self._upsert(rec)
        return rec

    def outcome_pnl(self, client_order_key: str, exit_price: float) -> float:
        """Return signed percent P&L from the recorded entry side/fill."""
        rec = self._fetch(client_order_key)
        if (rec is None or rec.status != "filled"
                or rec.filled_qty < rec.quantity
                or rec.filled_price is None
                or rec.side not in {"buy", "sell"}):
            raise ValueError("authoritative complete fill required before P&L")
        entry = self._coerce_price(rec.filled_price)
        exit_value = self._coerce_price(exit_price)
        if entry is None or exit_value is None:
            raise ValueError("prices must be positive finite numbers")
        pct = (exit_value - entry) / entry * 100.0
        return round(pct if rec.side == "buy" else -pct, 4)

    # ------------------------------------------------------------ retention
    def prune(self, older_than_days: float = 7.0) -> int:
        """Archive settled orders. Keeps the hot table proportional to OPEN
        orders instead of to everything ever traded.

        Only terminal records move; anything unresolved stays hot no matter
        how old, because age is not resolution.
        """
        cutoff = time.time() - older_than_days * 86400.0
        conn = self._connect()
        placeholders = ",".join("?" * len(self._TERMINAL))
        params = tuple(self._TERMINAL) + (cutoff,)
        conn.execute(
            "INSERT OR REPLACE INTO orders_archive SELECT * FROM orders "
            "WHERE status IN (%s) AND updated_at < ?" % placeholders, params)
        cursor = conn.execute(
            "DELETE FROM orders WHERE status IN (%s) AND updated_at < ?"
            % placeholders, params)
        conn.commit()
        if cursor.rowcount:
            logger.info("Archived %d settled order(s) older than %.1f days",
                        cursor.rowcount, older_than_days)
        return cursor.rowcount

    def stats(self) -> dict:
        conn = self._connect()
        hot = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        archived = conn.execute("SELECT COUNT(*) c FROM orders_archive").fetchone()["c"]
        placeholders = ",".join("?" * len(self._TERMINAL))
        unresolved = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE status NOT IN (%s)" % placeholders,
            tuple(self._TERMINAL)).fetchone()["c"]
        return {"hot_orders": hot, "archived_orders": archived,
                "unresolved": unresolved, "kill": self._kill_engaged()}


class BrokerExecutionAdapter:
    """Adapts AlpacaBroker to the tiny contract ExecutionSafety expects.

    ExecutionSafety speaks a deliberately small, broker-agnostic language:
    string sides, plain quantities, and three lookups. AlpacaBroker speaks
    enums and returns rich ExecutionResult objects. Rather than loosen either
    side, this adapter is the single seam between them.

    It also threads the client_order_key through to the broker as
    client_order_id, so idempotency is enforced by the broker itself and not
    only by our local ledger -- that is what makes a retry after an ambiguous
    submission safe.
    """

    def __init__(self, broker: Any):
        self._broker = broker

    @property
    def broker(self) -> Any:
        return self._broker

    @staticmethod
    def _to_order_side(side: str):
        from trading import OrderSide
        normalized = str(getattr(side, "value", side)).strip().lower()
        if normalized == "buy":
            return OrderSide.BUY
        if normalized == "sell":
            return OrderSide.SELL
        raise ValueError("unsupported side: %r" % (side,))

    def submit_order(self, *, symbol: str, side: str, quantity: int,
                     client_order_key: Optional[str] = None, **kwargs: Any):
        """Submit through AlpacaBroker, preserving the idempotency key."""
        return self._broker.execute_order(
            symbol=symbol,
            side=self._to_order_side(side),
            quantity=quantity,
            client_order_id=client_order_key,
            **kwargs,
        )

    def get_order(self, order_id: str):
        return self._broker.get_order(order_id)

    def get_order_by_client_id(self, client_order_id: str):
        return self._broker.get_order_by_client_id(client_order_id)

    def get_position(self, symbol: str):
        """Strict position lookup: raises on outage so 'flat' stays honest.

        ExecutionSafety.position_state() maps an exception to 'unknown', which
        is what we want -- an unreachable broker must never read as flat.
        """
        return self._broker.get_position_strict(symbol)


class PositionTruth:
    """The single authority on 'am I exposed in this symbol, and may I trade it?'

    Before this existed the system had two answers to that question and no
    arbiter:

      * ExecutionSafety   -- the ORDER ledger: intents, fills, ambiguity.
      * PositionStateManager -- the POSITION file: what we believe we hold.

    Either can be right while the other is stale, and a disagreement between
    them is exactly the situation in which it is least safe to trade. This
    class consults both plus the broker and collapses them into one verdict,
    with two rules:

      1. The broker is authoritative when it answers.
      2. Anything unknown, unresolved, or contradictory blocks entry.

    It never silently reconciles a disagreement -- it reports it and refuses.
    """

    #: Verdicts from exposure()
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"
    UNKNOWN = "unknown"
    CONFLICTED = "conflicted"

    def __init__(self, safety: "ExecutionSafety", adapter: Any,
                 state_manager: Any = None):
        #: Serialises the check-then-submit window, one lock per symbol.
        self._entry_locks: dict = {}
        self._exit_locks: dict = {}
        self._entry_lock_guard = threading.Lock()
        self.safety = safety
        self.adapter = adapter
        self._state_manager = state_manager

    # ------------------------------------------------------------------
    @property
    def state_manager(self):
        if self._state_manager is None:
            from position_state import PositionStateManager
            self._state_manager = PositionStateManager()
        return self._state_manager

    def _local_position(self, symbol: str) -> Optional[dict]:
        try:
            for position in self.state_manager.load_positions():
                if position.get("symbol") == symbol:
                    return position
        except Exception:
            # An unreadable position file is not evidence of being flat.
            return {"symbol": symbol, "qty": 0, "side": "", "_unreadable": True}
        return None

    @staticmethod
    def _side_of(quantity: float, declared: Any = None) -> str:
        declared = str(declared or "").strip().lower()
        if declared in ("buy", "long"):
            return "long"
        if declared in ("sell", "short"):
            return "short"
        if quantity > 0:
            return "long"
        if quantity < 0:
            return "short"
        return ""

    # ------------------------------------------------------------------
    def exposure(self, symbol: str) -> dict:
        """Collapse broker, order ledger and position file into one verdict.

        Returns a dict with 'verdict', the three raw views, and 'reasons' --
        never just a bare boolean, because *why* we are blocked matters when
        a human has to intervene.
        """
        reasons: list[str] = []

        broker_state = self.safety.position_state(self.adapter, symbol)
        broker_qty = 0.0
        broker_side = ""
        if broker_state == "open":
            try:
                raw = self.adapter.get_position(symbol)
                broker_qty = float(raw.get("qty", 0) if isinstance(raw, dict)
                                   else getattr(raw, "qty", 0))
                broker_side = self._side_of(
                    broker_qty,
                    raw.get("side") if isinstance(raw, dict) else getattr(raw, "side", None))
            except Exception:
                broker_state = "unknown"
        if broker_state == "unknown":
            reasons.append("broker position unreachable")

        local = self._local_position(symbol)
        local_unreadable = bool(local is not None and local.get("_unreadable"))
        if local_unreadable:
            reasons.append("local position file unreadable")
            local_side = ""
        elif local is not None:
            local_side = self._side_of(float(local.get("qty", 0) or 0), local.get("side"))
        else:
            local_side = ""

        unresolved_orders = self.safety.has_pending_exit(symbol)
        if unresolved_orders:
            reasons.append("unresolved order in ledger")

        if self.safety.kill_engaged():
            reasons.append("kill switch engaged")

        # --- collapse -------------------------------------------------
        # A position file we cannot read is not evidence of being flat; it is
        # missing evidence, which must read as unknown.
        if broker_state == "unknown" or local_unreadable:
            verdict = self.UNKNOWN
        elif broker_state == "open":
            verdict = broker_side or self.UNKNOWN
            if local_side and local_side != broker_side:
                verdict = self.CONFLICTED
                reasons.append(
                    "position file says %s, broker says %s" % (local_side, broker_side))
        else:  # broker says flat
            if local_side:
                verdict = self.CONFLICTED
                reasons.append(
                    "position file says %s, broker reports flat" % local_side)
            else:
                verdict = self.FLAT

        return {
            "symbol": symbol,
            "verdict": verdict,
            "broker": {"state": broker_state, "qty": broker_qty, "side": broker_side},
            "local": {"side": local_side, "record": local},
            "orders": {"unresolved": unresolved_orders},
            "reasons": reasons,
        }

    # ------------------------------------------------------------------
    def can_enter(self, symbol: str, side: str) -> tuple:
        """(allowed, reason) -- fail closed on anything but a confirmed flat.

        Deliberately strict: 'I am not sure' is treated exactly like 'I am
        already exposed'. A missed opportunity costs nothing; a duplicate or
        opposing position costs real money.
        """
        if self.safety.kill_engaged():
            return False, "kill switch engaged"

        snapshot = self.exposure(symbol)
        verdict = snapshot["verdict"]

        if verdict == self.UNKNOWN:
            return False, "position state unknown: " + "; ".join(snapshot["reasons"])
        if verdict == self.CONFLICTED:
            return False, "position state conflicted: " + "; ".join(snapshot["reasons"])
        if verdict != self.FLAT:
            return False, "already exposed (%s) in %s" % (verdict, symbol)

        # Side-specific: an unresolved order on THIS side is a duplicate of
        # the trade being attempted, which is worth naming separately from a
        # generic outstanding order. `side` was previously accepted and
        # ignored, so the signature promised a precision it did not have.
        try:
            if self.safety.has_open_exposure(symbol, str(side).lower()):
                return False, ("an unresolved %s order already exists for %s"
                               % (side, symbol))
        except Exception:
            return False, "could not check same-side exposure for %s" % symbol

        if snapshot["orders"]["unresolved"]:
            return False, "unresolved order outstanding for %s" % symbol
        return True, "flat"

    # ------------------------------------------------------------------
    def _entry_lock(self, symbol: str) -> threading.Lock:
        """One lock per symbol, created on demand."""
        key = str(symbol).upper()
        with self._entry_lock_guard:
            lock = self._entry_locks.get(key)
            if lock is None:
                lock = self._entry_locks[key] = threading.Lock()
            return lock

    def _exit_lock(self, symbol: str) -> threading.Lock:
        """One exit lock per symbol, separate from the entry lock.

        Deliberately NOT the same lock. Sharing one would make an exit wait
        behind an entry's broker round-trip, and delaying a liquidation is far
        more dangerous than delaying an entry.
        """
        key = str(symbol).upper()
        with self._entry_lock_guard:
            lock = self._exit_locks.get(key)
            if lock is None:
                lock = self._exit_locks[key] = threading.Lock()
            return lock

    @contextmanager
    def exit_claim(self, symbol: str):
        """Hold the right to close `symbol` while the caller acts on it.

        `has_unresolved_exit()` READS and `register_exit()` WRITES, and the
        broker call sits between them -- the same check-then-act shape as the
        entry gate, but with a worse failure. A duplicate close on a long does
        not merely flatten it: the surplus sell opens a SHORT.

        Measured before this existed: 10 concurrent exit requests on one
        symbol submitted 6 closes. Against a 10-share long that is 60 shares
        sold -- flat, then short 50.

        The three exit paths -- the 15-second position monitor, the daily-loss
        flatten, and the kill switch -- can all fire at once, and they are most
        likely to do so under exactly the stress that triggers them.
        """
        with self._exit_lock(symbol):
            yield

    @contextmanager
    def entry_claim(self, symbol: str, side: str):
        """Hold the right to enter `symbol` while the caller acts on it.

        can_enter() READS exposure and submit() WRITES it, and between those
        two the answer can change. Two threads -- a pipeline cycle and an
        operator hitting POST /api/execute, or two overlapping cycles -- could
        both see "flat" and both submit. The gate looked like it prevented a
        duplicate position and did not.

        Measured before this existed: 12 concurrent attempts on one symbol put
        5 orders at the broker, 50 shares against an intended 10. Five times
        the position and five times the risk, from a gate that reported it was
        working -- the seven it refused made it look correct.

        So the check and the order that acts on it happen under one per-symbol
        lock. Per symbol rather than global, because serialising SPY behind
        GLD would throw away good trades for no safety gain.

        Usage:

            with truth.entry_claim(symbol, side) as (allowed, reason):
                if not allowed:
                    ...refuse...
                ...size and submit here, still holding the claim...
        """
        with self._entry_lock(symbol):
            yield self.can_enter(symbol, side)

    # ------------------------------------------------------------------
    def reconcile(self, symbols: Optional[list] = None) -> dict:
        """Reconcile orders and positions together, and report disagreements.

        Runs the order ledger reconciliation first (so fills are known), then
        compares every symbol's three views. Conflicts are returned, never
        auto-resolved -- silently picking a winner is how a real position
        becomes invisible.
        """
        orders = self.safety.reconcile_all(self.adapter)

        if symbols is None:
            seen = set(self.safety.symbols())
            try:
                for position in self.state_manager.load_positions():
                    if position.get("symbol"):
                        seen.add(position["symbol"])
            except Exception:
                pass
            symbols = sorted(seen)

        # An order that reconciled to "filled" may still have left a position
        # open at the broker -- a partial close, or a fill on one leg of a
        # bracket. reconcile_flat is what detects that, and until now nothing
        # called it outside the guarded exit path.
        for record in orders:
            if record.status == "filled":
                try:
                    self.safety.reconcile_flat(
                        self.adapter, record.client_order_key)
                except Exception as exc:
                    logger.warning(
                        "Could not confirm %s is flat after reconcile: %s",
                        record.symbol, exc)

        snapshots = {s: self.exposure(s) for s in symbols}
        conflicts = {s: v for s, v in snapshots.items()
                     if v["verdict"] in (self.CONFLICTED, self.UNKNOWN)}
        return {
            "orders_reconciled": len(orders),
            "symbols": snapshots,
            "conflicts": conflicts,
            "status": "conflicted" if conflicts else "ok",
        }
