#!/bin/zsh

set -euo pipefail
umask 077

export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPYCACHEPREFIX="/private/tmp/main5-opening-forward-pycache"

repo_dir="/Users/shaym/Downloads/Educated_Trades-main 5"
env_file="${repo_dir}/.env"
python_bin="${repo_dir}/backend/venv/bin/python"
observer_data_dir="${OPENING_FORWARD_DATA_DIR:-/Users/shaym/.educated-trades/research}"
observer_db="${observer_data_dir}/opening_nq_qqq_forward.db"
observer_raw_dir="${observer_data_dir}/opening_nq_qqq_forward_raw"

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

mkdir -p "${observer_data_dir}" "${observer_raw_dir}"
cd "${repo_dir}"

if [[ "${1:-}" == "--check" ]]; then
  exec "${python_bin}" -m backend.research.opening_nq_qqq_forward \
    --db "${observer_db}" \
    --check
fi

session_date="${1:-$(TZ=America/New_York date +%F)}"
if [[ ! "${session_date}" =~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' ]]; then
  print -u2 -- "observer refused: session date must be YYYY-MM-DD"
  exit 64
fi

# Provider DNS and short-lived historical-data availability failures must not
# silently consume the day's only scheduled attempt. Terminal results are
# immutable, so retrying after COMPLETE/NO_SIGNAL is harmless but unnecessary.
# Every refusal remains in the append-only event ledger.
retry_delays=(0 60 300 900)
for attempt in {1..4}; do
  delay="${retry_delays[$attempt]}"
  if (( delay > 0 )); then
    print -u2 -- "observer transient retry ${attempt}/4 for ${session_date} in ${delay}s"
    sleep "${delay}"
  fi

  rc=0
  output="$("${python_bin}" -m backend.research.opening_nq_qqq_forward \
    --db "${observer_db}" \
    --session-date "${session_date}" \
    --raw-dir "${observer_raw_dir}" 2>&1)" || rc=$?
  print -r -- "${output}"

  if (( rc != 0 )); then
    if (( attempt == 4 )); then
      print -u2 -- "observer failed after 4 attempts for ${session_date} (exit ${rc})"
      exit "${rc}"
    fi
    continue
  fi

  observer_status="$(print -r -- "${output}" | "${python_bin}" -c \
    'import json,sys; print(json.load(sys.stdin).get("status", ""))' \
    2>/dev/null || true)"
  case "${observer_status}" in
    COMPLETE|NO_SIGNAL|REFUSED_ROLL_TRANSITION)
      exit 0
      ;;
    REFUSED_CALENDAR_SOURCE|REFUSED_NQ_SOURCE|REFUSED_QQQ_SOURCE)
      if (( attempt == 4 )); then
        print -u2 -- "observer exhausted transient retries for ${session_date}: ${observer_status}"
        exit 75
      fi
      ;;
    *)
      print -u2 -- "observer returned unrecognized status for ${session_date}: ${observer_status:-MISSING}"
      exit 70
      ;;
  esac
done
