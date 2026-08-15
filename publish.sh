#!/usr/bin/env bash
# Rebuild the dashboard and start the production server in the background.
# The build runs in the foreground so errors surface here rather than in a log.
#
# This does NOT take over the port from an existing server — see serve.ts for
# why. If the port is busy, stop the old server first or set DASHBOARD_PORT.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${DASHBOARD_PORT:-3000}"
HOST="${DASHBOARD_HOST:-127.0.0.1}"
mkdir -p .run

if lsof -t -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use by:" >&2
  lsof -iTCP:"$PORT" -sTCP:LISTEN >&2
  echo "Stop it, or run with DASHBOARD_PORT=3001 $0" >&2
  exit 1
fi

bun run build

# nohup + disown keeps it alive after this shell exits. macOS has no setsid.
nohup bun run start > .run/server.log 2>&1 < /dev/null &
disown

for _ in $(seq 1 50); do
  if curl -sf -o /dev/null "http://$HOST:$PORT"; then
    echo "dashboard serving on http://$HOST:$PORT"
    exit 0
  fi
  sleep 0.2
done

echo "warning: server started but is not answering — check .run/server.log" >&2
exit 1
