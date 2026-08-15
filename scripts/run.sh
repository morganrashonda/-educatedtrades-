#!/bin/bash
# Start the bot correctly from anywhere.
#
# Three things have to be true before main.py will run: you are in backend/,
# the venv is active, and .env is loaded. Missing any one of them produces a
# different confusing error -- "command not found", "No module named numpy",
# or a silent exit 2. This does all three and checks them first.
#
#   bash scripts/run.sh                 # run in this terminal, ctrl-c to stop
#   bash scripts/run.sh --background    # keep running after you close it
#
set -u

# Resolve the project root from the script's own location, so it does not
# matter what directory you invoke it from.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

fail() { echo "  ✗ $1" >&2; exit 1; }

# --- credentials ----------------------------------------------------------
[ -f .env ] || fail ".env not found. Run: bash scripts/setup_env.sh"
set -a
# shellcheck disable=SC1091
source .env
set +a

case "${APCA_API_KEY_ID:-}" in
    "" )                 fail "APCA_API_KEY_ID is empty. Run: bash scripts/setup_env.sh" ;;
    your_alpaca_key_id_here ) fail ".env still has placeholder keys. Run: bash scripts/setup_env.sh" ;;
esac
[ -n "${API_AUTH_TOKEN:-}" ] || fail "API_AUTH_TOKEN is empty. Run: bash scripts/setup_env.sh"

# --- packages -------------------------------------------------------------
[ -d backend/venv ] || fail "No venv. Run:
    cd backend && python3 -m venv venv && source venv/bin/activate
    pip install -r requirements.txt"
# shellcheck disable=SC1091
source backend/venv/bin/activate

python3 -c "import numpy, alpaca" 2>/dev/null || fail "Dependencies missing. Run:
    source backend/venv/bin/activate && pip install -r backend/requirements.txt"

# --- report what we are about to do, before doing it ----------------------
case "$(printf '%s' "$APCA_API_KEY_ID" | cut -c1-2 | tr '[:lower:]' '[:upper:]')" in
    PK) ENVIRONMENT="paper" ;;
    AK) ENVIRONMENT="LIVE — REAL MONEY" ;;
    *)  ENVIRONMENT="paper (unrecognised key prefix)" ;;
esac

LOG="${DATA_ROOT:-$HOME/.educated-trades}/bot.log"
mkdir -p "$(dirname "$LOG")"

echo
echo "  Educated Trades"
echo "  environment : $ENVIRONMENT"
echo "  data        : ${DATA_ROOT:-$HOME/.educated-trades}"
echo "  python      : $(python3 --version 2>&1)"
echo

cd backend || exit 1

if [ "${1:-}" = "--background" ]; then
    nohup python3 main.py --live > "$LOG" 2>&1 &
    PID=$!
    sleep 3
    if kill -0 "$PID" 2>/dev/null; then
        echo "  Running in the background (pid $PID)"
        echo
        echo "    watch : tail -f $LOG"
        echo "    stop  : kill $PID"
        echo
    else
        echo "  ✗ Exited immediately. Last lines of the log:"
        echo
        tail -20 "$LOG" | sed 's/^/    /'
        exit 1
    fi
else
    echo "  Running here. Press ctrl-c to stop."
    echo
    exec python3 main.py --live
fi
