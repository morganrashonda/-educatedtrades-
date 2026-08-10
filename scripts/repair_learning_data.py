#!/usr/bin/env python3
"""Repair learning data corrupted by the direction-blind outcome bug.

Before the fix, `record_outcome()` computed P&L as (exit - entry) / entry
regardless of side. Long trades were recorded correctly; SHORT trades were
recorded with the sign inverted -- a short that fell 10% (a win) was stored as
a 10% loss, and vice versa. `pattern_memory` had no `side` column, so the row
itself carries no way to tell.

The damage is recoverable. `close_tracked_position()` computed the P&L
CORRECTLY (side-aware) for reporting, and wrote that correct value into
`milestone_tracker` as a note of the form:

    WIN: SELL SPY qty=10 @ $100.00→$90.00 (+10.00%)

So the truth was persisted all along -- just to a different table than the one
the learner reads. This script recovers it.

Safe by default: DRY RUN unless you pass --apply. It never deletes anything,
takes a backup before writing, and reports every change it intends to make.

    python3 scripts/repair_learning_data.py                 # report only
    python3 scripts/repair_learning_data.py --apply         # repair
    python3 scripts/repair_learning_data.py --quarantine    # mark, don't fix
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.environ.get("DATA_DIR", "."), "patterns.db"))

#: WIN: SELL SPY qty=10 @ $100.00→$90.00 (+10.00%)
NOTE_RE = re.compile(
    r"^(?P<outcome>WIN|LOSS):\s*(?P<side>BUY|SELL)\s+(?P<symbol>\S+)\s+"
    r"qty=(?P<qty>[\d.]+)\s*@\s*\$(?P<entry>[\d.]+)\s*[→>-]+\s*\$(?P<exit>[\d.]+)\s*"
    r"\((?P<pct>[+-]?[\d.]+)%\)", re.IGNORECASE)


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def parse_milestones(conn):
    """Recover (symbol, entry, exit) -> truth from the milestone notes."""
    try:
        rows = conn.execute(
            "SELECT timestamp, symbol, value, note FROM milestone_tracker "
            "WHERE type = 'trade' ORDER BY timestamp ASC").fetchall()
    except sqlite3.Error as exc:
        print("Could not read milestone_tracker: %s" % exc)
        return {}

    recovered = {}
    unparsed = 0
    for row in rows:
        match = NOTE_RE.match((row["note"] or "").strip())
        if not match:
            unparsed += 1
            continue
        key = (match.group("symbol").upper(),
               round(float(match.group("entry")), 2),
               round(float(match.group("exit")), 2))
        recovered[key] = {
            "side": match.group("side").lower(),
            "outcome": match.group("outcome").lower(),
            "profit_pct": float(match.group("pct")),
            "timestamp": row["timestamp"],
        }
    if unparsed:
        print("  (%d milestone notes did not match the expected format)" % unparsed)
    return recovered


def analyse(conn):
    truth = parse_milestones(conn)
    try:
        trades = conn.execute(
            "SELECT id, symbol, entry_price, exit_price, profit_pct, outcome, "
            "timestamp FROM pattern_memory "
            "WHERE outcome IN ('win','loss') ORDER BY timestamp ASC").fetchall()
    except sqlite3.Error as exc:
        print("Could not read pattern_memory: %s" % exc)
        return [], [], truth

    inverted, unmatched = [], []
    for row in trades:
        if row["entry_price"] is None or row["exit_price"] is None:
            unmatched.append(dict(row))
            continue
        key = (str(row["symbol"]).upper(),
               round(float(row["entry_price"]), 2),
               round(float(row["exit_price"]), 2))
        fact = truth.get(key)
        if fact is None:
            unmatched.append(dict(row))
            continue
        stored = float(row["profit_pct"] or 0.0)
        if (abs(stored - fact["profit_pct"]) > 0.01
                or row["outcome"] != fact["outcome"]):
            inverted.append({
                "id": row["id"], "symbol": row["symbol"],
                "stored_pct": stored, "stored_outcome": row["outcome"],
                "true_pct": fact["profit_pct"], "true_outcome": fact["outcome"],
                "side": fact["side"],
            })
    return inverted, unmatched, truth


def rebuild_stats(conn):
    """Recompute pattern_stats from the corrected pattern_memory rows."""
    conn.execute("UPDATE pattern_stats SET wins = 0, losses = 0, "
                 "total_profit_pct = 0.0")
    rows = conn.execute(
        "SELECT pattern_hash, outcome, profit_pct FROM pattern_memory "
        "WHERE outcome IN ('win','loss')").fetchall()
    tally = {}
    for row in rows:
        entry = tally.setdefault(row["pattern_hash"], [0, 0, 0.0])
        if row["outcome"] == "win":
            entry[0] += 1
        else:
            entry[1] += 1
        entry[2] += float(row["profit_pct"] or 0.0)
    for pattern_hash, (wins, losses, total) in tally.items():
        conn.execute(
            "UPDATE pattern_stats SET wins=?, losses=?, total_profit_pct=? "
            "WHERE pattern_id=?", (wins, losses, round(total, 4), pattern_hash))
    return len(tally)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--apply", action="store_true",
                        help="write the corrections (default is a dry run)")
    parser.add_argument("--quarantine", action="store_true",
                        help="mark affected rows unusable instead of correcting")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print("No database at %s" % args.db)
        return 1

    conn = connect(args.db)
    print("=" * 68)
    print("LEARNING DATA REPAIR — %s" % args.db)
    print("=" * 68)

    inverted, unmatched, truth = analyse(conn)
    total = conn.execute(
        "SELECT COUNT(*) FROM pattern_memory WHERE outcome IN ('win','loss')"
    ).fetchone()[0]

    print("Completed trades in pattern_memory : %d" % total)
    print("Recoverable from milestone notes   : %d" % len(truth))
    print("Rows whose stored outcome is WRONG : %d" % len(inverted))
    print("Rows with no milestone match       : %d" % len(unmatched))

    if inverted:
        sides = Counter(r["side"] for r in inverted)
        print("\nAffected by side: %s" % dict(sides))
        print("\nFirst few corrections:")
        for row in inverted[:8]:
            print("  id=%-5s %-6s %-4s  stored %+7.2f%% (%s)  ->  true %+7.2f%% (%s)"
                  % (row["id"], row["symbol"], row["side"].upper(),
                     row["stored_pct"], row["stored_outcome"],
                     row["true_pct"], row["true_outcome"]))

    if unmatched:
        print("\n%d rows cannot be verified from milestones. Their direction is"
              "\nunknowable, so they are left alone -- treat them as suspect."
              % len(unmatched))

    if not args.apply and not args.quarantine:
        print("\nDRY RUN — nothing written. Re-run with --apply to correct,")
        print("or --quarantine to mark affected rows unusable instead.")
        return 0

    backup = "%s.bak-%s" % (args.db,
                            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    shutil.copy2(args.db, backup)
    print("\nBackup written: %s" % backup)

    if args.quarantine:
        ids = [r["id"] for r in inverted]
        for row_id in ids:
            conn.execute(
                "UPDATE pattern_memory SET outcome='quarantined' WHERE id=?",
                (row_id,))
        conn.commit()
        print("Quarantined %d rows (excluded from stats and learning)." % len(ids))
    else:
        for row in inverted:
            conn.execute(
                "UPDATE pattern_memory SET profit_pct=?, outcome=?, side=? "
                "WHERE id=?",
                (row["true_pct"], row["true_outcome"], row["side"], row["id"]))
        conn.commit()
        print("Corrected %d rows." % len(inverted))

    patterns = rebuild_stats(conn)
    conn.commit()
    print("Rebuilt pattern_stats for %d patterns." % patterns)
    print("\nNote: win rates and signal strength are now recomputed from the")
    print("corrected outcomes. Patterns whose evidence no longer clears the")
    print("confidence threshold will correctly carry zero weight.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
