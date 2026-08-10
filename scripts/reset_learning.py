#!/usr/bin/env python3
"""Retire learning state that was accumulated under superseded logic.

Everything the pattern engine learned before today was built on inputs that
have since been corrected:

  * EMA was computed over only `period` bars from a raw-price seed, so the
    12/26 crossover disagreed with a correct EMA ~20% of the time -- and
    `ema_cross` is one of the signature's features. Patterns were bucketed
    by a value that was often wrong.
  * ADX used a plain mean of DX instead of Wilder smoothing, so the regime
    label disagreed ~42% of the time.
  * Short trades recorded P&L with the sign inverted, so every short taught
    the engine the opposite of what happened.
  * `sentiment_zone` was a third signature axis. Sentiment has been removed
    from the signal path, so old signatures are not comparable to new ones.

Those are not bad rows to be repaired -- the SIGNATURES themselves mean
something different now. Rebuilding stats from them would carry the
contamination forward.

So this archives rather than deletes. History moves to `*_legacy` tables and
stays queryable; the live tables start empty. Nothing is lost, and nothing
misleading is left in the path the learner reads.

    python3 scripts/reset_learning.py            # report only (default)
    python3 scripts/reset_learning.py --apply    # archive and reset
    python3 scripts/reset_learning.py --apply --keep-outcomes
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.environ.get("DATA_DIR", "."), "patterns.db"))
FITS_DIR = os.path.join(os.environ.get("DATA_DIR", "."), "patterns_fits")

#: Tables whose contents were produced by the superseded logic.
LEARNED_TABLES = ("pattern_memory", "pattern_stats", "pattern_learned_weights")


def table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def count(conn, name: str) -> int:
    if not table_exists(conn, name):
        return 0
    return conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]


def archive_table(conn, name: str) -> int:
    """Copy `name` into `name_legacy_<stamp>`, then empty it."""
    if not table_exists(conn, name):
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    legacy = "%s_legacy_%s" % (name, stamp)
    conn.execute("DROP TABLE IF EXISTS %s" % legacy)
    conn.execute("CREATE TABLE %s AS SELECT * FROM %s" % (legacy, name))
    moved = count(conn, legacy)
    conn.execute("DELETE FROM %s" % name)
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--apply", action="store_true",
                        help="perform the reset (default is a report)")
    parser.add_argument("--keep-outcomes", action="store_true",
                        help="retain pattern_memory rows; reset only the "
                             "derived stats and weights")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print("No database at %s" % args.db)
        print("Set DB_PATH or DATA_DIR, or pass --db.")
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    print("=" * 68)
    print("RESET LEARNING STATE — %s" % args.db)
    print("=" * 68)

    resolved = 0
    if table_exists(conn, "pattern_memory"):
        resolved = conn.execute(
            "SELECT COUNT(*) FROM pattern_memory "
            "WHERE outcome IN ('win','loss')").fetchone()[0]

    print("pattern_memory rows        : %d  (%d with a resolved outcome)"
          % (count(conn, "pattern_memory"), resolved))
    print("pattern_stats rows         : %d" % count(conn, "pattern_stats"))
    print("pattern_learned_weights    : %d" % count(conn, "pattern_learned_weights"))
    print("milestone_tracker (KEPT)   : %d" % count(conn, "milestone_tracker"))
    print("active_positions (KEPT)    : %d" % count(conn, "active_positions"))
    print("daily_bars (KEPT)          : %d" % count(conn, "daily_bars"))

    fits = []
    if os.path.isdir(FITS_DIR):
        fits = [f for f in os.listdir(FITS_DIR) if f.endswith(".json")]
    print("saved fits (%s): %d" % (FITS_DIR, len(fits)))

    open_positions = count(conn, "active_positions")
    if open_positions:
        print()
        print("WARNING: %d position(s) are still open. Their record_ids point "
              "into pattern_memory;" % open_positions)
        print("         resetting now leaves them unable to record an outcome. "
              "Close or settle")
        print("         them first, or pass --keep-outcomes.")

    targets = (["pattern_stats", "pattern_learned_weights"] if args.keep_outcomes
               else list(LEARNED_TABLES))
    print()
    print("Will archive and clear: %s" % ", ".join(targets))
    print("Will preserve         : milestone_tracker, active_positions, daily_bars")

    if not args.apply:
        print()
        print("DRY RUN — nothing changed. Re-run with --apply to proceed.")
        conn.close()
        return 0

    if open_positions and not args.keep_outcomes:
        print()
        print("REFUSING: open positions would be orphaned. Settle them first,")
        print("          or re-run with --keep-outcomes.")
        conn.close()
        return 2

    backup = "%s.bak-%s" % (args.db,
                            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    shutil.copy2(args.db, backup)
    print()
    print("Backup written: %s" % backup)

    total = 0
    for name in targets:
        moved = archive_table(conn, name)
        total += moved
        print("  %-26s archived %d row(s)" % (name, moved))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    if fits:
        retired = os.path.join(FITS_DIR, "retired-%s"
                               % datetime.now(timezone.utc).strftime("%Y%m%d"))
        os.makedirs(retired, exist_ok=True)
        for name in fits:
            shutil.move(os.path.join(FITS_DIR, name),
                        os.path.join(retired, name))
        print("  %-26s moved %d fit(s) to %s"
              % ("patterns_fits", len(fits), retired))

    print()
    print("Archived %d row(s) into *_legacy_* tables; live tables are empty." % total)
    print("The engine will now learn from scratch on corrected inputs.")
    print()
    print("Expect no tradeable patterns for a while: with the multiple-testing")
    print("correction, a pattern needs real evidence before it carries weight.")
    print("Silence is the system working, not failing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
