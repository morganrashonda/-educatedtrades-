#!/bin/bash
# Interactive .env setup — asks for your Alpaca keys and writes the file.
#
# Nothing is sent anywhere. This runs entirely on your machine, the secret is
# typed with echo disabled, and the resulting file is chmod 600 so only your
# user can read it. Run it again any time you want to switch accounts.
#
#   bash scripts/setup_env.sh

set -u
cd "$(dirname "$0")/.." || exit 1
ENV_FILE=".env"

echo
echo "  Educated Trades — environment setup"
echo "  ───────────────────────────────────"
echo

# --- Back up an existing file rather than clobbering it -------------------
if [ -f "$ENV_FILE" ]; then
    BACKUP="${ENV_FILE}.backup-$(date +%Y%m%d-%H%M%S)"
    cp "$ENV_FILE" "$BACKUP"
    chmod 600 "$BACKUP"
    echo "  Existing .env backed up to $BACKUP"
    echo
fi

# --- Alpaca key ID --------------------------------------------------------
echo "  Get your keys at https://app.alpaca.markets"
echo "  Use the PAPER tab unless you specifically intend to trade real money."
echo
read -r -p "  Alpaca API Key ID: " KEY_ID
KEY_ID="$(printf '%s' "$KEY_ID" | tr -d '[:space:]')"

if [ -z "$KEY_ID" ]; then
    echo "  No key entered — nothing written."
    exit 1
fi

# --- Secret, typed blind --------------------------------------------------
# -s disables echo, so the secret never appears on screen or in scrollback.
read -r -s -p "  Alpaca API Secret Key (typing is hidden): " SECRET
echo
SECRET="$(printf '%s' "$SECRET" | tr -d '[:space:]')"

if [ -z "$SECRET" ]; then
    echo "  No secret entered — nothing written."
    exit 1
fi

# --- Which environment do these keys select? ------------------------------
PREFIX="$(printf '%s' "$KEY_ID" | cut -c1-2 | tr '[:lower:]' '[:upper:]')"
case "$PREFIX" in
    PK) ENVIRONMENT="paper" ;;
    AK) ENVIRONMENT="live"  ;;
    *)  ENVIRONMENT="paper (key prefix '$PREFIX' is unrecognised — resolving to paper, which is the safe direction)" ;;
esac

# --- API token, generated rather than invented ----------------------------
if command -v openssl >/dev/null 2>&1; then
    API_TOKEN="$(openssl rand -hex 32)"
else
    API_TOKEN="$(head -c 32 /dev/urandom | xxd -p | tr -d '\n')"
fi

# --- Data root ------------------------------------------------------------
DEFAULT_ROOT="$HOME/.educated-trades"
read -r -p "  Data directory [$DEFAULT_ROOT]: " DATA_ROOT
DATA_ROOT="${DATA_ROOT:-$DEFAULT_ROOT}"

# --- Write ----------------------------------------------------------------
umask 077                       # the file is private from the moment it exists
cat > "$ENV_FILE" <<EOF
# Written by scripts/setup_env.sh on $(date)
# This file contains credentials. It is git-ignored. Do not commit or share it.

APCA_API_KEY_ID=$KEY_ID
APCA_API_SECRET_KEY=$SECRET

# Required — the control API can change mode and place trades.
API_AUTH_TOKEN=$API_TOKEN

# The bot appends /paper or /live itself, derived from the key prefix above.
DATA_ROOT=$DATA_ROOT

# Optional: news context. Free key at https://finnhub.io/register
FINNHUB_API_KEY=

# Optional: a backup repository you own. Unset = local snapshots only.
BACKUP_REMOTE_URL=
EOF
chmod 600 "$ENV_FILE"

mkdir -p "$DATA_ROOT"

echo
echo "  ───────────────────────────────────"
echo "  Written to $ENV_FILE (readable only by you)"
echo "  Environment : $ENVIRONMENT"
echo "  Data goes to: $DATA_ROOT/${ENVIRONMENT%% *}"
echo "  API token   : generated"
echo
echo "  Next:"
echo "    set -a; source .env; set +a"
echo "    cd backend && python3 main.py --live"
echo
if [ "$PREFIX" = "AK" ]; then
    echo "  ⚠  These are LIVE keys. Orders will use real money."
    echo
fi
