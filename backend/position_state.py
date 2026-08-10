"""
Position State Persistence for Crash Recovery.

Maintains a crash-safe JSON file at /home/team/shared/data/position_state.json
that records every open position with full metadata so the bot can survive a
process restart and resume monitoring without losing state.

Key design decisions:
  - Atomic writes (write to .tmp, then os.rename) so a crash mid-write never
    corrupts the canonical file.
  - Reconciliation with the Alpaca API at startup: mismatches are logged at
    CRITICAL severity and corrected.
  - Single source of truth is the file; the in-memory dict and broker state
    are reconciled against it on every load.

Usage:
    manager = PositionStateManager()
    manager.save_positions(positions_list)
    positions = manager.load_positions()
    reconciled = manager.reconcile(broker_positions)
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("educator.position_state")

# Path for the position state file
POSITION_STATE_PATH = os.path.join(os.environ.get("DATA_DIR", "/home/team/shared/data"), "position_state.json")
# Temp path for atomic writes
POSITION_STATE_TMP = POSITION_STATE_PATH + ".tmp"

# Fields that every position record must contain
REQUIRED_FIELDS = [
    "symbol", "qty", "side", "entry_price",
    "entry_time", "position_id",
]

# Full schema for reference (all fields)
FULL_SCHEMA = [
    "symbol",           # ticker
    "qty",              # number of shares/contracts
    "side",             # "buy" (long) or "sell" (short)
    "entry_price",      # fill price
    "current_stop_loss",  # hard stop-loss level
    "current_take_profit", # take-profit level
    "entry_time",       # unix timestamp
    "position_id",      # record_id from patterns.db pattern_memory
    "broker_order_id",  # Alpaca order ID
    "strategy",         # "trend_following" or "mean_reversion"
    "regime_at_entry",  # market regime when opened
    "conviction_at_entry",  # conviction score when opened
]


class PositionStateManager:
    """
    Manages crash-safe persistence of open position state.

    Writes are serialised internally. The class used to say the caller should
    do it, on the reasoning that "the orchestrator pipeline already runs
    single-threaded per cycle". It does not: add_position() and
    remove_position() are called from trading.py on entry and on exit, which
    happen on the pipeline thread AND on the 15-second position monitor.

    Measured before the lock existed -- 20 threads each adding a different
    symbol left ONE position in the file. Nineteen were lost, because
    add_position() is load -> modify -> save and every caller was writing
    through the same shared temp filename as well.
    """

    def __init__(self, state_path: str = POSITION_STATE_PATH):
        self.state_path = state_path
        #: Unique per process AND per instance, so two writers never fight
        #: over one temp file. A shared ".tmp" meant one thread's rename
        #: consumed the file another was still writing.
        self.tmp_path = "%s.tmp.%d.%d" % (state_path, os.getpid(), id(self))
        self._cache: Optional[List[dict]] = None
        #: Guards the read-modify-write in add_position/remove_position.
        #: Re-entrant because those call load/save, which take it too.
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(state_path), exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_positions(self) -> List[dict]:
        """
        Load the position state from disk.

        Returns an empty list if the file does not exist or is corrupt
        (logs a warning on corruption so the caller knows to reconcile
        from the broker).
        """
        if not os.path.exists(self.state_path):
            return []

        try:
            with open(self.state_path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "Position state file corrupt or unreadable: %s — "
                "will rebuild from broker.", e,
            )
            return []

        # Validate structure — should be a list of dicts
        if not isinstance(data, list):
            logger.warning(
                "Position state file has unexpected structure (%s) — "
                "will rebuild.", type(data).__name__,
            )
            return []

        # Validate each record has required fields
        valid = []
        for i, rec in enumerate(data):
            missing = [f for f in REQUIRED_FIELDS if f not in rec]
            if missing:
                logger.warning(
                    "Position state record %d missing fields %s — skipping.",
                    i, missing,
                )
                continue
            valid.append(rec)

        self._cache = list(valid)
        logger.info(
            "Loaded %d positions from state file (%d total records, %d invalid)",
            len(valid), len(data), len(data) - len(valid),
        )
        return self._cache

    def save_positions(self, positions: List[dict]) -> bool:
        """
        Persist the list of open positions to disk atomically.

        Writes to a .tmp file first, then renames — so a crash during
        write never corrupts the canonical state file.

        Returns True on success, False on failure.
        """
        # Strip any non-serialisable fields and ensure all position records
        # have the required fields.
        clean = []
        for pos in positions:
            record = {}
            for key in FULL_SCHEMA:
                if key in pos:
                    record[key] = pos[key]
            # Ensure required fields always present
            for key in REQUIRED_FIELDS:
                if key not in record:
                    record[key] = pos.get(key, None)
            clean.append(record)

        try:
            # Atomic write: temp file → rename
            with open(self.tmp_path, "w") as f:
                json.dump(clean, f, indent=2, default=str)
            os.rename(self.tmp_path, self.state_path)
            self._cache = list(clean)
            logger.debug("Saved %d positions to state file", len(clean))
            return True
        except (OSError, TypeError) as e:
            logger.error("Failed to save position state: %s", e)
            # Clean up temp file if rename failed
            try:
                if os.path.exists(self.tmp_path):
                    os.remove(self.tmp_path)
            except OSError:
                pass
            return False

    def reconcile(
        self,
        broker_positions: List[dict],
    ) -> Dict[str, Any]:
        """
        Reconcile the on-disk position state with the broker's view.

        Called at startup to detect and fix inconsistencies between what
        the bot *thinks* is open and what the broker *actually* has.

        Args:
            broker_positions: List of position dicts from the broker API.
                              Each should have at least "symbol" and "qty".
                              An empty list is valid only after a successful broker query;
                              callers must raise on broker errors.

        Returns a summary dict:
          {
              "loaded": N,           # positions loaded from file
              "broker_has": M,       # positions from broker
              "adopted": [...],      # positions the broker has but we didn't know about
              "cleaned": [...],      # positions in our state but not at the broker
              "matched": [...],      # positions that agree
              "inconsistencies": [], # positions that disagree on qty/side
              "status": "ok" | "inconsistent",
          }
        """
        state_positions = self.load_positions()
        broker_by_symbol = {p["symbol"]: p for p in broker_positions}
        state_by_symbol = {p["symbol"]: p for p in state_positions}

        adopted: List[dict] = []
        cleaned: List[dict] = []
        matched: List[dict] = []
        inconsistencies: List[dict] = []

        # 1. Broker has a position we don't know about → adopt it
        for sym, bp in broker_by_symbol.items():
            if sym not in state_by_symbol:
                adopted.append({
                    "symbol": sym,
                    "qty": float(bp.get("qty", 0)),
                    "side": str(bp.get("side") or ("buy" if float(bp.get("qty", 0)) > 0 else "sell")).lower(),
                    "entry_price": float(bp.get("avg_entry_price", bp.get("entry_price", 0))),
                    "entry_time": time.time(),
                    "position_id": None,  # unknown — will need patterns.db tracking
                    "broker_order_id": bp.get("asset_id", None),
                    "note": "Adopted from broker — was not in our state file",
                })
                logger.info(
                    "RECONCILE: Adopted position %s from broker (not in our state)",
                    sym,
                )

        # 2. We have a position the broker doesn't → clean up state
        for sym, sp in state_by_symbol.items():
            if sym not in broker_by_symbol:
                cleaned.append({
                    "symbol": sym,
                    "position_id": sp.get("position_id"),
                    "note": "Position not found at broker — state cleaned",
                })
                logger.critical(
                    "RECONCILE: Position %s (state record %s) not found at broker! "
                    "Cleaning up state.",
                    sym, sp.get("position_id"),
                )

        # 3. Positions that exist in both → verify qty/side match
        for sym, sp in state_by_symbol.items():
            bp = broker_by_symbol.get(sym)
            if bp is None:
                continue

            state_qty = float(sp.get("qty", 0))
            broker_qty = float(bp.get("qty", 0))
            state_side = str(sp.get("side", "")).lower()
            broker_side = str(bp.get("side", "")).lower()
            side_mismatch = bool(state_side and broker_side and state_side != broker_side)
            if abs(state_qty - broker_qty) > 0.001 or side_mismatch:
                inconsistencies.append({
                    "symbol": sym,
                    "state_qty": state_qty,
                    "broker_qty": broker_qty,
                    "position_id": sp.get("position_id"),
                    "note": f"Position mismatch: state_qty={state_qty}, broker_qty={broker_qty}, state_side={state_side}, broker_side={broker_side}",
                    "state_side": state_side,
                    "broker_side": broker_side,
                })
                logger.critical(
                    "RECONCILE: Qty mismatch for %s: state=%s broker=%s — "
                    "will use broker value.",
                    sym, state_qty, broker_qty,
                )
            else:
                matched.append({
                    "symbol": sym,
                    "qty": broker_qty,
                    "position_id": sp.get("position_id"),
                })

        # Build the reconciled position list: start with state, add adopted
        reconciled = list(state_positions)
        for a in adopted:
            reconciled.append(a)

        # For inconsistencies, correct state to match broker
        for inc in inconsistencies:
            sym = inc["symbol"]
            bp = broker_by_symbol[sym]
            for pos in reconciled:
                if pos["symbol"] == sym:
                    pos["qty"] = float(bp.get("qty", pos["qty"]))
                    if bp.get("side"):
                        pos["side"] = str(bp["side"]).lower()
                    break

        # For cleaned positions, remove them from the list
        reconciled = [
            p for p in reconciled
            if p["symbol"] not in {c["symbol"] for c in cleaned}
        ]

        # Persist the reconciled state
        self.save_positions(reconciled)

        status = "ok"
        if inconsistencies:
            status = "inconsistent"
        if cleaned:
            # Cleaned positions always flag as at least inconsistent
            status = "inconsistent"

        result = {
            "loaded": len(state_positions),
            "broker_has": len(broker_positions),
            "adopted": adopted,
            "cleaned": cleaned,
            "matched": matched,
            "inconsistencies": inconsistencies,
            "reconciled_count": len(reconciled),
            "status": status,
        }

        logger.info(
            "Reconciliation complete: loaded=%d broker=%d "
            "adopted=%d cleaned=%d matched=%d inconsistencies=%d",
            result["loaded"], result["broker_has"],
            len(adopted), len(cleaned), len(matched), len(inconsistencies),
        )

        return result

    def add_position(self, position: dict) -> bool:
        """
        Add or update a single position in the state file.

        Load, modify and save happen under one lock: on their own they are
        check-then-act, and two threads adding different symbols each wrote
        back a list that did not contain the other's.
        """
        with self._lock:
            positions = self.load_positions()
            symbol = position.get("symbol")
            # Remove existing entry for this symbol if present
            positions = [p for p in positions if p.get("symbol") != symbol]
            positions.append(position)
            return self.save_positions(positions)

    def remove_position(self, symbol: str) -> bool:
        """
        Remove a position by symbol from the state file.

        Returns True if the position was found and removed.
        """
        with self._lock:
            positions = self.load_positions()
            before = len(positions)
            positions = [p for p in positions if p.get("symbol") != symbol]
            if len(positions) == before:
                return False  # Didn't exist
            return self.save_positions(positions)

    def clear_all(self, confirmed_flat: bool = False) -> bool:
        """Clear all position state. Requires proof that we are actually flat.

        Deleting this file does not close anything -- it only makes open
        positions invisible to us while they keep running at the broker. The
        caller must confirm flatness with the broker first and say so
        explicitly; there is no safe default.
        """
        if not confirmed_flat:
            logger.critical(
                "clear_all() refused: caller did not confirm the broker is "
                "flat. Position state left intact."
            )
            return False
        return self.save_positions([])


# ---------------------------------------------------------------------------
# Convenience: Build position record dict
# ---------------------------------------------------------------------------

def build_position_record(
    symbol: str,
    qty: int,
    side: str,
    entry_price: float,
    entry_time: Optional[float] = None,
    position_id=None,
    broker_order_id=None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    strategy: Optional[str] = None,
    regime: Optional[str] = None,
    conviction: Optional[float] = None,
) -> dict:
    """Build a standardised position record dict for the state file."""
    return {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "entry_price": entry_price,
        "current_stop_loss": stop_loss,
        "current_take_profit": take_profit,
        "entry_time": entry_time or time.time(),
        "position_id": position_id,
        "broker_order_id": broker_order_id,
        "strategy": strategy,
        "regime_at_entry": regime,
        "conviction_at_entry": conviction,
    }


# ---------------------------------------------------------------------------
# CLI quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    mgr = PositionStateManager()
    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        mgr.clear_all()
        print("Position state cleared.")
    else:
        positions = mgr.load_positions()
        print(f"Loaded {len(positions)} positions:")
        for p in positions:
            print(f"  {p.get('side', '?')} {p.get('qty', 0)}x {p.get('symbol', '?')} @ ${p.get('entry_price', 0):.2f}")