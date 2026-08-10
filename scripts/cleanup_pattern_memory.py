#!/usr/bin/env python3
"""One-time cleanup: deduplicate pattern_memory, then create UNIQUE INDEX and rebuild stats."""

import os
import sys

DB_PATH = os.environ.get("DB_PATH", "/home/team/shared/data/patterns.db")

import sqlite3
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Count before
before = conn.execute("SELECT COUNT(*) FROM pattern_memory").fetchone()[0]
hashes = conn.execute("SELECT COUNT(DISTINCT pattern_hash) FROM pattern_memory").fetchone()[0]
print(f"BEFORE: {before} rows, {hashes} distinct hashes")

# Dedup: keep lowest id per (pattern_hash, symbol, entry_price)
print("Deduplicating...")
conn.execute("DELETE FROM pattern_memory WHERE id NOT IN (SELECT MIN(id) FROM pattern_memory GROUP BY pattern_hash, symbol, entry_price)")
conn.commit()

# Count after
after = conn.execute("SELECT COUNT(*) FROM pattern_memory").fetchone()[0]
print(f"AFTER: {after} rows (removed {before - after} duplicates)")

# Create UNIQUE INDEX (now safe since duplicates are removed)
print("Creating UNIQUE INDEX...")
conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_pattern ON pattern_memory(pattern_hash, symbol, entry_price)")
conn.commit()
print("UNIQUE INDEX created.")

# Rebuild pattern_stats from actual data
print("Rebuilding pattern_stats...")
conn.execute("DELETE FROM pattern_stats")
rebuild_sql = (
    "INSERT INTO pattern_stats "
    "(pattern_id, sentiment_zone, rsi_zone, ema_cross, count, last_seen, wins, losses, total_profit_pct) "
    "SELECT "
    "  pm.pattern_hash, pm.sentiment_zone, pm.rsi_zone, pm.ema_cross, "
    "  COUNT(*) as count, MAX(pm.timestamp) as last_seen, "
    "  SUM(CASE WHEN pm.outcome = 'win' THEN 1 ELSE 0 END) as wins, "
    "  SUM(CASE WHEN pm.outcome = 'loss' THEN 1 ELSE 0 END) as losses, "
    "  COALESCE(SUM(CASE WHEN pm.outcome = 'win' THEN pm.profit_pct ELSE 0 END), 0) as total_profit_pct "
    "FROM pattern_memory pm "
    "GROUP BY pm.pattern_hash, pm.sentiment_zone, pm.rsi_zone, pm.ema_cross"
)
conn.execute(rebuild_sql)
conn.commit()
stats_count = conn.execute("SELECT COUNT(*) FROM pattern_stats").fetchone()[0]
print(f"pattern_stats rebuilt: {stats_count} rows.")

conn.close()
print("=== ALL DONE: dedup, index, stats ===")