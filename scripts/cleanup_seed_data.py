"""
Migration script: delete all seed rows from pattern_memory on the droplet.

Run manually on the droplet:
    cd /path/to/deployment && python3 scripts/cleanup_seed_data.py

This deletes rows where data_source = 'seed' and drops/recreates
pattern_stats so per-symbol hashes start fresh.  The TRADING_MODE fix
and per-symbol hash code change must be deployed first.
"""

import os
import sqlite3
from pathlib import Path

DATA_DIR = os.environ.get("DATA_DIR", "/home/team/shared/data")
DB_PATH = Path(os.environ.get("DB_PATH", os.path.join(DATA_DIR, "patterns.db")))


def main():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Count before
    total = conn.execute("SELECT COUNT(*) FROM pattern_memory").fetchone()[0]
    seed = conn.execute("SELECT COUNT(*) FROM pattern_memory WHERE data_source = 'seed'").fetchone()[0]
    live = conn.execute("SELECT COUNT(*) FROM pattern_memory WHERE data_source = 'live'").fetchone()[0]
    stats_count = conn.execute("SELECT COUNT(*) FROM pattern_stats").fetchone()[0]

    print(f"Before: pattern_memory: {total} total ({seed} seed, {live} live)")
    print(f"Before: pattern_stats: {stats_count} rows")

    # Delete seed rows
    cur = conn.execute("DELETE FROM pattern_memory WHERE data_source = 'seed'")
    deleted = cur.rowcount
    print(f"Deleted {deleted} seed rows")

    # Drop old pattern_stats (all were built from seed data with no symbol dimension)
    cur = conn.execute("DELETE FROM pattern_stats")
    print("Cleared pattern_stats (will be rebuilt from live data)")

    conn.commit()

    # Verify
    remaining = conn.execute("SELECT COUNT(*) FROM pattern_memory").fetchone()[0]
    stats_remaining = conn.execute("SELECT COUNT(*) FROM pattern_stats").fetchone()[0]
    print(f"After:  pattern_memory: {remaining} rows")
    print(f"After:  pattern_stats: {stats_remaining} rows")
    print("Done. Restart the orchestrator to begin collecting live data.")


if __name__ == "__main__":
    main()