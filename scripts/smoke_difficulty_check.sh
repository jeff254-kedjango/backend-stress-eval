#!/usr/bin/env bash
# smoke_difficulty_check.sh — one-shot real-claude smoke test for `bse difficulty-check`.
#
# Why: chunk B's driver is validated by unit tests using fake shell scripts.
# The first real-claude invocation may still surface quirks (headless auth
# prompts, tool-permission escalations, transcript oddities). This script
# runs a single N=1 session against the trivial fixture under
# dev-fixtures/trivial-candidate/, with a lowered ceiling so the whole
# thing takes seconds. It is safe to run on any dev machine that has
# claude on PATH and network access.
#
# This is NOT part of the check.sh gate — running headless claude in CI
# would burn tokens and rate limits. It is a manual verification tool.
#
# Exit codes match `bse difficulty-check` (see cli/main.py).

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && cd .. && pwd)"
FIXTURE="$HERE/dev-fixtures/trivial-candidate"

if [[ ! -d "$FIXTURE" ]]; then
    echo "error: fixture missing at $FIXTURE" >&2
    exit 2
fi
if ! command -v claude >/dev/null 2>&1; then
    echo "error: claude CLI not on PATH; install claude-code first" >&2
    exit 2
fi

# Activate the project venv so `bse` is on PATH.
# shellcheck disable=SC1091
source "$HERE/.venv/bin/activate"

echo "==> Running bse difficulty-check against the trivial fixture ($FIXTURE)"
echo "    N=1 attempt (via --no-ledger dry run wouldn't work; we accept ledger writes)"
echo "    Real claude session — expect ~30-90s wall clock."

# We invoke through python -c to override the N constant for this smoke
# run — the CLI has no --n flag (deliberate: knobs game the gate) so we
# monkeypatch. This is smoke-test-only; production users invoke `bse
# difficulty-check <dir>` directly and get N=3.
python <<'PY'
from pathlib import Path
import sys
from core import difficulty
difficulty.DIFFICULTY_N_ATTEMPTS = 1

from core.difficulty import run_difficulty_check
fixture = Path("dev-fixtures/trivial-candidate")
result = run_difficulty_check(
    fixture,
    n_attempts=1,
    threshold_minutes=0.0,          # any success = pass
    ceiling_minutes=5.0,            # 5-min ceiling; trivial task takes seconds
    write_ledger=False,
)
print(result.to_summary())
sys.exit(0 if result.passed else 1)
PY
