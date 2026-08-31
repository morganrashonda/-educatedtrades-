#!/usr/bin/env bash
# Creates the branch, applies the change, runs the shadow tests.
# Does NOT touch the running bot -- restart it yourself when you're happy.
set -euo pipefail

REPO="${1:-$HOME/Downloads/Educated_Trades-main 5}"
PATCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/one-look-per-day.patch"
cd "$REPO"

if [[ -n "$(git status --porcelain -- backend/shadow_forward.py backend/tests/test_shadow_forward.py)" ]]; then
  echo "refusing to run: the two files this patch touches already have local edits" >&2
  exit 1
fi

git checkout -b fix/shadow-gate-one-look-per-day
git apply --check "$PATCH"
git apply "$PATCH"

# Run the tests before committing, so a failure leaves the branch uncommitted.
if [[ -x backend/venv/bin/python ]]; then PY=backend/venv/bin/python; else PY=python3; fi
"$PY" -m pytest backend/tests/test_shadow_forward.py -q

git add backend/shadow_forward.py backend/tests/test_shadow_forward.py
git commit -F - <<'MSG'
Evaluate shadow promotion evidence once per day

_paper_exploration_gate calls ShadowForwardStore.evidence() on every
orchestrator poll -- about 195 times a trading day at the 120s cycle --
and promotes a pattern on the first pass. Each call ran a fresh
moving-block bootstrap over overlapping data, so a pattern was never
being tested at 5%: it was getting 195 chances to clear 5%.

Against a zero-edge simulation calibrated on the current shadow
outcomes, that optional stopping raises the false-promotion rate from
3.7% to 8.5% at the preregistered 100/20 minima, and from 6.0% to 15.1%
at looser ones. It is a larger leak than any plausible change to the
sample minima, and unlike those it costs no statistical power to close.

Cache the bootstrap verdict per (pattern, side, strategy, regime,
minima) per UTC day. Only the bootstrap is frozen: completed counts,
distinct days, expectancy and profit factor stay live, so those gates
can still block intraday and the audit log still shows current data.
Cycles that never reach the bootstrap do not consume the day's look.
Stale days are evicted on write so the cache cannot grow unbounded.

Tests: five of the seven added tests fail against the previous
behaviour, including the case where eleven winning days arriving
mid-session flip a failed verdict to passing on the next cycle.
MSG

echo
echo "done -- branch fix/shadow-gate-one-look-per-day"
git --no-pager log --oneline -1
git --no-pager diff --stat HEAD~1
echo
echo "The bot is still running the old code. Restart it when you want this live."
