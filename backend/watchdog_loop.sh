#!/bin/bash
# Watchdog loop — runs watchdog.py every 60 seconds.
#
# The PID file used to be a fixed /tmp/watchdog_loop.pid, and startup kills
# whatever PID it finds there. That was fine while one orchestrator existed.
# Paper and live now have separate data directories and can run side by side,
# and with a shared PID file each instance would kill the other's watchdog on
# start -- leaving whichever started first unmonitored, silently. So the lock
# is keyed to the data directory, which is exactly what distinguishes them.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${DATA_DIR:-${DATA_ROOT:-/home/team/shared/data}/paper}"

# One key per environment. Slashes become underscores so it is a valid name.
KEY="$(printf '%s' "$DATA_DIR" | tr '/' '_')"
PID_FILE="/tmp/watchdog_loop${KEY}.pid"
LOG_FILE="/tmp/watchdog${KEY}.log"

if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    # Only kill a process that is actually still ours. A recycled PID
    # belonging to something else must not be signalled.
    if [ -n "${OLD_PID}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        case "$(ps -p "$OLD_PID" -o args= 2>/dev/null || true)" in
            *watchdog_loop.sh*) kill "$OLD_PID" 2>/dev/null || true ;;
        esac
    fi
fi
echo $$ > "$PID_FILE"

CHILD_PID=""

cleanup() {
    rm -f "$PID_FILE"
}

shutdown() {
    # A trap that only performs cleanup returns to the infinite loop. Stop the
    # command currently being supervised, then exit so the EXIT trap removes
    # the PID file and the watchdog cannot silently continue running.
    trap - INT TERM
    if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
    exit 0
}

trap cleanup EXIT
trap shutdown INT TERM

while true; do
    cd "$SCRIPT_DIR"
    python3 watchdog.py >> "$LOG_FILE" 2>&1 &
    CHILD_PID=$!
    wait "$CHILD_PID" || true
    CHILD_PID=""

    sleep 60 &
    CHILD_PID=$!
    wait "$CHILD_PID" || true
    CHILD_PID=""
done
