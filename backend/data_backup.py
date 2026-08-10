"""
Automated daily backup module for Educated Trades.

Exports gzipped snapshots of patterns.db, audit_log.jsonl, trade outcomes,
position state, and reports to a local backup repo, then pushes to GitHub
using an SSH deploy key.

30-day rotation removes stale snapshots.

The default remote is a dedicated backup repository so runtime data never
mixes with source code. BACKUP_REMOTE_URL must be set to push;
without it snapshots are taken locally and not pushed anywhere.

Usage:
    from data_backup import run_daily_backup, backup_restore
    result = run_daily_backup()                     # standalone
    result = run_daily_backup(patterns_db_path=…)   # custom path
    restored = backup_restore(snapshot_path)         # reconstruct state
"""

import gzip
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("data_backup")

# ---------------------------------------------------------------------------
# Constants  (all overridable via env vars)
# ---------------------------------------------------------------------------

# Where the backup repo clone lives locally
BACKUP_REPO_DIR = Path(os.environ.get("BACKUP_REPO_DIR",
    os.path.join(os.environ.get("DATA_DIR", "/var/lib/educated-trades/data"), "..", "backup_repo")))

# Where data to back up lives
DATA_SOURCES_DIR = Path(os.environ.get("DATA_DIR", "/var/lib/educated-trades/data"))

# Remote — SSH, and REQUIRED to be configured. There is deliberately no
# default.
#
# This used to default to a specific backup repository. That is a landmine the
# moment the project is forked or copied: the new bot would push its
# patterns.db, audit log and position state into the ORIGINAL project's backup
# repo, where both write date-named snapshots and silently overwrite each
# other. Same collision as paper-vs-live sharing one directory, one level up
# — and harder to notice, because a backup only gets read when something has
# already gone wrong.
#
# Unset means: still take local snapshots, do not push anywhere. Losing
# off-box redundancy is recoverable; writing your data into someone else's
# repository is not.
REMOTE_URL = os.environ.get("BACKUP_REMOTE_URL", "").strip()

# How old a snapshot directory must be (in days) before it is pruned.
RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))


def current_environment() -> str:
    """"paper" or "live", from the same detector the trading path uses.

    BACKUP_REPO_DIR is `$DATA_DIR/../backup_repo`, so once DATA_DIR became
    `$DATA_ROOT/paper` and `$DATA_ROOT/live`, both environments resolved to
    the SAME backup repo -- and snapshots are named by date alone. A paper
    run and a live run on the same day wrote `patterns.db.gz` to the same
    path, so whichever finished last silently replaced the other's backup and
    committed the overwrite. Snapshots are therefore filed under the
    environment as well as the date.
    """
    try:
        from trading import detect_environment
        return detect_environment()
    except Exception:
        return "paper"


def _source_dir() -> Path:
    """Where the live data actually is, resolved at call time.

    Read at import time this froze to a default that no longer matched, so
    `_create_snapshots` looked in `/var/lib/educated-trades/data` while the
    caller passed the correct path in `patterns_db_path` -- a parameter the
    function then ignored, reporting success having backed up nothing.
    """
    return Path(os.environ.get("DATA_DIR", str(DATA_SOURCES_DIR)))


# ---------------------------------------------------------------------------
# Git helpers  (SSH-based — no PAT required)
# ---------------------------------------------------------------------------

def _ensure_ssh_remote(repo_dir: Path) -> bool:
    """
    Ensure the backup repo's 'origin' remote points at REMOTE_URL over SSH.
    Returns True if the remote is correct (or was set), False on failure.
    """
    try:
        orig = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10, cwd=str(repo_dir),
        )
        current = orig.stdout.strip() if orig.returncode == 0 else ""
        if current != REMOTE_URL:
            subprocess.run(
                ["git", "remote", "set-url", "origin", REMOTE_URL],
                capture_output=True, text=True, timeout=10, cwd=str(repo_dir),
            )
            logger.info("Remote updated: %s → %s", current, REMOTE_URL)
        return True
    except Exception as e:
        logger.error("Failed to set remote URL: %s", e)
        return False


def _commit_and_push(repo_dir: Path, today: str, backed_up: List[str], branch: str = "main") -> bool:
    """
    Git add, commit, and push changes in repo_dir to REMOTE_URL over SSH.
    Returns True if the push succeeded, False otherwise.
    """
    try:
        subprocess.run(
            ["git", "add", "-A"],
            capture_output=True, text=True, timeout=30, cwd=str(repo_dir),
        )
        commit = subprocess.run(
            ["git", "commit", "-m", f"Daily backup {today} [{len(backed_up)} files]"],
            capture_output=True, text=True, timeout=30, cwd=str(repo_dir),
        )
        if commit.returncode != 0:
            out = (commit.stdout + commit.stderr).lower()
            if "nothing to commit" in out:
                logger.info("Nothing new to commit — backup data unchanged")
                return True
            logger.warning("Commit rc=%d: %s", commit.returncode, commit.stderr[:200])

        push = subprocess.run(
            ["git", "push", "origin", branch],
            capture_output=True, text=True, timeout=60, cwd=str(repo_dir),
        )
        if push.returncode == 0:
            logger.info("Backup pushed successfully to %s", REMOTE_URL)
            return True
        else:
            logger.warning("Push FAILED (rc=%d): %s", push.returncode, push.stderr[:300])
            return False
    except Exception as e:
        logger.error("Git commit/push exception: %s", e)
        return False


# ---------------------------------------------------------------------------
# Alert helper (push failure)
# ---------------------------------------------------------------------------

def _alert_push_failure(detail: str) -> None:
    """Log a CRITICAL alert to the team-db alerts table AND write a
    notification file that the orchestrator can forward to the lead."""
    notif_dir = _source_dir() / "notifications"
    notif_dir.mkdir(parents=True, exist_ok=True)
    notif = {
        "type": "backup_failure",
        "severity": "critical",
        "message": f"Daily backup push FAILED: {detail[:200]}",
        "timestamp": time.time(),
    }
    notif_path = notif_dir / f"backup_failure_{int(time.time())}.json"
    try:
        with open(notif_path, "w") as f:
            json.dump(notif, f)
        logger.critical("Backup failure notification written to %s", notif_path)
    except Exception as e:
        logger.error("Failed to write backup notification file: %s", e)

    try:
        from alert_db import insert_alert
        insert_alert("backup_failure", detail, "critical")
    except Exception as e:
        logger.error("Failed to log backup failure alert to DB: %s", e)


# ---------------------------------------------------------------------------
# Core: snapshot creation
# ---------------------------------------------------------------------------

def _create_snapshots(snap_dir: Path, source_dir: Optional[Path] = None,
                      patterns_db_path: Optional[str] = None) -> List[str]:
    """Copy and gzip all tracked data files into *snap_dir*.

    `source_dir` is resolved at call time. It used to be the module-level
    DATA_SOURCES_DIR, frozen at import from an environment variable that
    main.py did not export -- so this read a directory that had nothing in it
    while the caller passed the right one and was told the backup succeeded.

    Returns list of filenames that were successfully backed up."""
    backed_up: List[str] = []
    src_dir = Path(source_dir) if source_dir else _source_dir()

    # 1. patterns.db
    db_src = Path(patterns_db_path) if patterns_db_path else src_dir / "patterns.db"
    if db_src.exists():
        with open(db_src, "rb") as f_in:
            with gzip.open(snap_dir / "patterns.db.gz", "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        backed_up.append("patterns.db.gz")
    else:
        logger.warning("patterns.db not found at %s", db_src)

    # 2. audit_log.jsonl
    audit_src = src_dir / "audit_log.jsonl"
    if audit_src.exists():
        with open(audit_src, "rb") as f_in:
            with gzip.open(snap_dir / "audit_log.jsonl.gz", "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        backed_up.append("audit_log.jsonl.gz")

    # 3. Trade outcomes (query patterns.db pattern_memory)
    if db_src.exists():
        try:
            conn = sqlite3.connect(str(db_src))
            conn.row_factory = sqlite3.Row
            trades = conn.execute(
                "SELECT * FROM pattern_memory ORDER BY timestamp DESC LIMIT 5000"
            ).fetchall()
            if trades:
                trade_list = [dict(r) for r in trades]
                with gzip.open(snap_dir / "trade_outcomes.json.gz", "wt", encoding="utf-8") as f:
                    json.dump(trade_list, f, default=str)
                backed_up.append("trade_outcomes.json.gz")
            conn.close()
        except Exception as te:
            logger.warning("Failed to export trade outcomes: %s", te)

    # 4. Position state
    pos_src = src_dir / "position_state.json"
    if pos_src.exists():
        with open(pos_src, "rb") as f_in:
            with gzip.open(snap_dir / "position_state.json.gz", "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        backed_up.append("position_state.json.gz")

    # 5. Reconciliation / overnight risk / backfill status reports
    for fname, src_name in [
        ("reconciliation.json.gz", "reconciliation_latest.json"),
        ("overnight_risk.json.gz", "overnight_risk_latest.json"),
        ("backfill_status.json.gz", "backfill_status.json"),
    ]:
        p = src_dir / src_name
        if p.exists():
            with open(p, "rb") as f_in:
                with gzip.open(snap_dir / fname, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            backed_up.append(fname)

    return backed_up


def _rotate_old_snapshots(repo_data: Path) -> int:
    """Remove snapshot directories older than RETENTION_DAYS.
    Returns the number of directories removed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    removed = 0
    for d in sorted(repo_data.iterdir()):
        if d.is_dir():
            try:
                d_date = datetime.strptime(d.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if d_date < cutoff:
                    shutil.rmtree(d)
                    removed += 1
            except ValueError:
                pass
    return removed


# ---------------------------------------------------------------------------
# Public API: run_daily_backup
# ---------------------------------------------------------------------------

def run_daily_backup(
    patterns_db_path: Optional[str] = None,
    backup_date_ref: Optional[List[str]] = None,
    branch: str = "main",
) -> Dict[str, Any]:
    """
    Execute a full daily backup cycle.

    1. Ensures the backup repo exists (clone if missing, pull if present).
    2. Creates gzipped snapshots in /data/YYYY-MM-DD/.
    3. Rotates snapshots older than RETENTION_DAYS.
    4. Commits and pushes to REMOTE_URL over SSH.
    5. On push failure, logs a CRITICAL alert and writes a notification file.

    Args:
        patterns_db_path: Optional override for patterns.db path.
        backup_date_ref: Optional mutable list of length 1; after a successful
            backup, the list is populated with today's date string. This lets
            callers gate on "already backed up today" without a shared class.
        branch: Git branch to push to (default: main).

    Returns:
        dict with keys: status, date, files, rotation_removed, push_success
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Skip if already done today
    if backup_date_ref is not None and len(backup_date_ref) == 1 and backup_date_ref[0] == today:
        return {"status": "skipped", "reason": "already_backed_up_today", "date": today}

    logger.info("=== Daily Backup: %s ===  remote=%s", today, REMOTE_URL)

    # ---- Ensure backup repo exists ----
    repo_dir = Path(BACKUP_REPO_DIR)
    if not REMOTE_URL:
        # Local snapshots only. Better than pushing into whichever repository
        # a hardcoded default happened to name.
        repo_dir.mkdir(parents=True, exist_ok=True)
    elif not repo_dir.exists():
        logger.info("Cloning backup repo from %s into %s ...", REMOTE_URL, repo_dir)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(
            ["git", "clone", REMOTE_URL, str(repo_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if clone.returncode != 0:
            err = clone.stderr[:200]
            logger.error("Failed to clone backup repo: %s", err)
            return {"status": "error", "error": f"Clone failed: {err}"}
    else:
        # Ensure the remote is correct (might have been HTTPS before)
        _ensure_ssh_remote(repo_dir)
        # Pull latest
        try:
            subprocess.run(
                ["git", "pull", "origin", branch, "--ff-only"],
                capture_output=True, text=True, timeout=60, cwd=str(repo_dir),
            )
        except Exception as pe:
            logger.warning("Backup repo pull failed (non-fatal): %s", pe)

    # ---- Create snapshot directory ----
    # Filed under the environment as well as the date. Paper and live share
    # one backup repo (BACKUP_REPO_DIR is $DATA_DIR/../backup_repo, and the
    # ".." collapses the segregation), so a date-only path meant a live run
    # overwrote the same day's paper snapshot and committed the loss.
    environment = current_environment()
    data_dir = repo_dir / "data" / environment
    data_dir.mkdir(parents=True, exist_ok=True)
    snap_dir = data_dir / today
    snap_dir.mkdir(parents=True, exist_ok=True)

    # ---- Create snapshots ----
    backed_up = _create_snapshots(snap_dir, source_dir=_source_dir(),
                                  patterns_db_path=patterns_db_path)

    # ---- 30-day rotation (within this environment only) ----
    removed = _rotate_old_snapshots(data_dir)
    if removed:
        logger.info("Rotated out %d old backup dir(s) (>%d days)", removed, RETENTION_DAYS)

    logger.info("Backed up %d files to %s", len(backed_up), snap_dir)

    # ---- Commit and push ----
    if not REMOTE_URL:
        logger.warning(
            "BACKUP_REMOTE_URL is not set — snapshot written to %s but NOT "
            "pushed off this machine. Set it to a backup repository you own.",
            snap_dir)
        push_ok = False
    else:
        push_ok = _commit_and_push(repo_dir, today, backed_up, branch=branch)
        if not push_ok:
            _alert_push_failure(
                f"Push to {branch} failed; backup preserved locally at {snap_dir}")

    # ---- Mark done ----
    if backup_date_ref is not None and len(backup_date_ref) == 1:
        backup_date_ref[0] = today

    return {
        "status": ("ok" if push_ok
                   else ("local_only" if not REMOTE_URL else "push_failed")),
        "date": today,
        "files": backed_up,
        "rotation_removed": removed,
        "push_success": push_ok,
    }


# ---------------------------------------------------------------------------
# Public API: backup_restore
# ---------------------------------------------------------------------------

def backup_restore(snapshot_path: str, restore_dir: Optional[str] = None) -> Dict[str, Any]:
    """Restore a sandbox's state from a daily backup snapshot."""
    snap = Path(snapshot_path)
    if not snap.exists() or not snap.is_dir():
        return {"status": "error", "error": f"Snapshot path not found: {snapshot_path}"}

    dst = Path(restore_dir) if restore_dir else DATA_SOURCES_DIR
    dst.mkdir(parents=True, exist_ok=True)

    restored = []
    for gz_path in sorted(snap.glob("*.gz")):
        # Determine output name
        name = gz_path.name
        if name.endswith(".gz"):
            name = name[:-3]

        try:
            with gzip.open(gz_path, "rb") as f_in:
                content = f_in.read()
            out_path = dst / name
            with open(out_path, "wb") as f_out:
                f_out.write(content)
            restored.append(name)
            logger.info("Restored %s (%d bytes)", name, len(content))
        except Exception as re:
            logger.error("Failed to restore %s: %s", gz_path.name, re)

    return {
        "status": "ok",
        "files_restored": restored,
        "restore_dir": str(dst),
    }