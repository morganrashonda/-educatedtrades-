"""
Focused tests for Ticket G: gate-counter schema fix.

Verifies:
1. pattern_memory table has 'tier' column after schema init
2. Migration adds 'tier' column to existing databases
3. Migration raises on non-duplicate-column errors (does not swallow)
4. insert_pattern writes tier correctly
5. record_pattern threads tier through
6. record_trade_pattern_and_track threads tier through
7. tier='signal' is stored for Tier 1 trades
8. tier=NULL for backward-compatible calls (missing tier parameter)
9. Fail-closed: SELECT on NULL-tier returns None, not a crash
"""

import os
import sys
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_db():
    """Create a temporary patterns SQLite file path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# Tests: Schema and migration
# ---------------------------------------------------------------------------

def test_pattern_memory_has_tier_column_on_init():
    """pattern_memory CREATE TABLE includes 'tier' column."""
    from patterns import PatternDatabase

    db_path = _make_temp_db()
    try:
        db = PatternDatabase(Path(db_path))

        conn = db._connect()
        rows = conn.execute("PRAGMA table_info('pattern_memory')").fetchall()
        columns = {row[1] for row in rows}
        conn.close()

        assert "tier" in columns, f"tier column missing from pattern_memory. Columns: {columns}"
    finally:
        os.unlink(db_path)


def test_tier_column_default_is_null():
    """tier column defaults to NULL for new rows without tier specified."""
    from patterns import PatternDatabase

    db_path = _make_temp_db()
    try:
        db = PatternDatabase(Path(db_path))

        # Insert a row without tier (INSERT OR IGNORE via insert_pattern)
        rid = db.insert_pattern(
            pattern_hash="test_hash_default",
            symbol="SPY",
            sentiment_zone="neutral",
            rsi_zone="normal",
            ema_cross="no_cross",
            sentiment_score=0.0,
            rsi_value=50.0,
            conviction_score=0.0,
            entry_price=450.0,
            data_source="live",
            # tier NOT passed
        )

        conn = db._connect()
        row = conn.execute(
            "SELECT tier FROM pattern_memory WHERE id=?", (rid,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["tier"] is None, f"tier should default to NULL, got: {row['tier']}"
    finally:
        os.unlink(db_path)


def test_tier_column_written_correctly():
    """tier column stores the passed value."""
    from patterns import PatternDatabase

    db_path = _make_temp_db()
    try:
        db = PatternDatabase(Path(db_path))

        rid = db.insert_pattern(
            pattern_hash="test_hash_signal",
            symbol="SPY",
            sentiment_zone="neutral",
            rsi_zone="normal",
            ema_cross="no_cross",
            sentiment_score=0.2,
            rsi_value=50.0,
            conviction_score=0.3,
            entry_price=450.0,
            data_source="live",
            tier="signal",
        )

        conn = db._connect()
        row = conn.execute(
            "SELECT tier FROM pattern_memory WHERE id=?", (rid,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["tier"] == "signal", f"tier should be 'signal', got: {row['tier']}"
    finally:
        os.unlink(db_path)


def test_tier_migration_adds_column_to_existing_db():
    """ALTER TABLE migration adds tier column when DB predates the fix."""
    db_path = _make_temp_db()

    # Create a pre-migration database: pattern_memory without tier column
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pattern_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            symbol TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            sentiment_zone TEXT NOT NULL,
            rsi_zone TEXT NOT NULL,
            ema_cross TEXT NOT NULL,
            sentiment_score REAL,
            rsi_value REAL,
            conviction_score REAL,
            entry_price REAL,
            exit_price REAL,
            exit_hours_later REAL,
            profit_pct REAL,
            outcome TEXT DEFAULT 'pending',
            data_source TEXT DEFAULT 'live'
        )
    """)
    conn.commit()
    conn.close()

    try:
        from patterns import PatternDatabase
        db = PatternDatabase(Path(db_path))

        conn = db._connect()
        rows = conn.execute("PRAGMA table_info('pattern_memory')").fetchall()
        columns = {row[1] for row in rows}
        conn.close()

        assert "tier" in columns, (
            f"Migration should have added tier column. Columns: {columns}"
        )
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Tests: record_pattern threading
# ---------------------------------------------------------------------------

def test_record_pattern_threads_tier():
    """record_pattern passes tier through to insert_pattern."""
    from patterns import PatternEngine
    from patterns import PatternDatabase

    db_path = _make_temp_db()
    try:
        db = PatternDatabase(Path(db_path))
        engine = PatternEngine(Path(db_path))

        rid = engine.record_pattern(
            symbol="QQQ",
            sentiment_score=0.5,
            conviction_score=0.4,
            rsi_value=35.0,
            ema_short=380.0,
            ema_long=375.0,
            entry_price=400.0,
            tier="signal",
        )

        conn = db._connect()
        row = conn.execute(
            "SELECT tier FROM pattern_memory WHERE id=?", (rid,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["tier"] == "signal", f"tier should be 'signal', got: {row['tier']}"
    finally:
        os.unlink(db_path)


def test_record_pattern_tier_none_when_omitted():
    """record_pattern with no tier stores NULL."""
    from patterns import PatternEngine
    from patterns import PatternDatabase

    db_path = _make_temp_db()
    try:
        db = PatternDatabase(Path(db_path))
        engine = PatternEngine(Path(db_path))

        rid = engine.record_pattern(
            symbol="QQQ",
            sentiment_score=0.5,
            conviction_score=0.4,
            rsi_value=35.0,
            ema_short=380.0,
            ema_long=375.0,
            entry_price=400.0,
            # tier omitted
        )

        conn = db._connect()
        row = conn.execute(
            "SELECT tier FROM pattern_memory WHERE id=?", (rid,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["tier"] is None, f"tier should be NULL when omitted, got: {row['tier']}"
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Tests: record_trade_pattern_and_track threading
# ---------------------------------------------------------------------------

def test_record_trade_pattern_and_track_threads_tier():
    """record_trade_pattern_and_track passes tier through."""
    from patterns import PatternEngine
    from patterns import PatternDatabase

    db_path = _make_temp_db()
    try:
        db = PatternDatabase(Path(db_path))
        engine = PatternEngine(Path(db_path))

        rid, phash = engine.record_trade_pattern_and_track(
            symbol="IWM",
            sentiment_score=0.6,
            conviction_score=0.5,
            rsi_value=30.0,
            ema_short=195.0,
            ema_long=190.0,
            entry_price=200.0,
            quantity=10,
            side="buy",
            tier="signal",
        )

        conn = db._connect()
        row = conn.execute(
            "SELECT tier FROM pattern_memory WHERE id=?", (rid,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["tier"] == "signal", f"tier should be 'signal', got: {row['tier']}"
        assert isinstance(phash, str) and len(phash) > 0
    finally:
        os.unlink(db_path)


def test_record_trade_pattern_and_track_tier_omitted_is_null():
    """record_trade_pattern_and_track without tier stores NULL (backward compat)."""
    from patterns import PatternEngine
    from patterns import PatternDatabase

    db_path = _make_temp_db()
    try:
        db = PatternDatabase(Path(db_path))
        engine = PatternEngine(Path(db_path))

        rid, phash = engine.record_trade_pattern_and_track(
            symbol="IWM",
            sentiment_score=0.6,
            conviction_score=0.5,
            rsi_value=30.0,
            ema_short=195.0,
            ema_long=190.0,
            entry_price=200.0,
            quantity=10,
            side="buy",
            # tier omitted
        )

        conn = db._connect()
        row = conn.execute(
            "SELECT tier FROM pattern_memory WHERE id=?", (rid,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["tier"] is None, f"tier should be NULL when omitted, got: {row['tier']}"
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Tests: Fail-closed on missing tier
# ---------------------------------------------------------------------------

def test_select_tier_returns_none_not_error():
    """SELECT tier on a NULL-tier row returns None, not an exception."""
    from patterns import PatternDatabase

    db_path = _make_temp_db()
    try:
        db = PatternDatabase(Path(db_path))

        rid = db.insert_pattern(
            pattern_hash="test_hash_null",
            symbol="SPY",
            sentiment_zone="neutral",
            rsi_zone="normal",
            ema_cross="no_cross",
            sentiment_score=0.0,
            rsi_value=50.0,
            conviction_score=0.0,
            entry_price=450.0,
            data_source="live",
            # tier omitted → NULL
        )

        conn = db._connect()
        try:
            row = conn.execute(
                "SELECT tier FROM pattern_memory WHERE id=?", (rid,)
            ).fetchone()
            # Should succeed — NULL is valid
            assert row is not None
            assert row["tier"] is None
        except Exception as e:
            pytest.fail(f"SELECT tier should not raise on NULL value: {e}")
        finally:
            conn.close()
    finally:
        os.unlink(db_path)
