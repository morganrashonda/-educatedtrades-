#!/bin/zsh

set -euo pipefail
umask 077

export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPYCACHEPREFIX="/private/tmp/main5-accepted-break-forward-pycache"

repo_dir="/Users/shaym/Downloads/Educated_Trades-main 5"
env_file="${repo_dir}/.env"
python_bin="${repo_dir}/backend/venv/bin/python"
observer_data_dir="${OPENING_FORWARD_DATA_DIR:-/Users/shaym/.educated-trades/research}"
observer_db="${observer_data_dir}/opening_accepted_break_forward.db"

if [[ ! -r "${env_file}" ]]; then
  print -u2 -- "observer refused: missing readable ${env_file}"
  exit 78
fi
if [[ ! -x "${python_bin}" ]]; then
  print -u2 -- "observer refused: missing executable ${python_bin}"
  exit 78
fi

set -a
source "${env_file}"
set +a

: "${DATABENTO_API_KEY:?observer refused: DATABENTO_API_KEY is not set}"
: "${APCA_API_KEY_ID:?observer refused: APCA_API_KEY_ID is not set}"
: "${APCA_API_SECRET_KEY:?observer refused: APCA_API_SECRET_KEY is not set}"

mkdir -p "${observer_data_dir}"
cd "${repo_dir}"

if [[ "${1:-}" == "--check" ]]; then
  exec "${python_bin}" -m backend.research.opening_accepted_break_forward \
    --db "${observer_db}" \
    --check
fi

session_date="$(TZ=America/New_York date +%F)"
exec "${python_bin}" -m backend.research.opening_accepted_break_forward \
  --db "${observer_db}" \
  --session-date "${session_date}"
