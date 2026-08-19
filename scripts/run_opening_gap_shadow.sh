#!/bin/sh
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$REPO_DIR/.env"
PYTHON_BIN="$HOME/.educated-trades/opening-gap-venv/bin/python"
SHADOW_DIR="$HOME/.educated-trades/research"
SHADOW_DB="$SHADOW_DIR/opening_gap_shadow.db"

if [ ! -f "$ENV_FILE" ]; then
    echo "FATAL: missing $ENV_FILE" >&2
    exit 1
fi
if [ ! -x "$PYTHON_BIN" ]; then
    echo "FATAL: isolated opening-gap Python runtime is missing: $PYTHON_BIN" >&2
    exit 1
fi

mkdir -p "$SHADOW_DIR"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

cd "$REPO_DIR"
exec "$PYTHON_BIN" -m backend.research.opening_gap_databento --db "$SHADOW_DB"
