#!/usr/bin/env bash
# Replay the FastAPI 0.141.1 lifecycle-leak discovery sweep and grade it.
#
# From the repo root:
#   ./eval-tasks/fastapi-0.141.1-lifecycle-leak/reproduce.sh
#
# Produces ./replay/report.json + ./replay/summary.txt in this directory,
# then runs the four RUBRIC.md grading queries via grade.py and prints
# PASS/FAIL for each plus an overall verdict.
#
# Requirements: bash and `bse` on PATH (i.e. venv activated with
# `pip install -e ".[fastapi]"` from the repo root). No jq — grade.py is
# pure Python using only the stdlib.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASELINE="$HERE/baseline-report.json"
REPLAY_DIR="$HERE/replay"
REPLAY_JSON="$REPLAY_DIR/report.json"

if ! command -v bse >/dev/null 2>&1; then
    echo "error: 'bse' not on PATH. Have you activated the venv and" \
         "run 'pip install -e \".[fastapi]\"'?" >&2
    exit 2
fi

if [[ ! -f "$BASELINE" ]]; then
    echo "error: baseline-report.json missing — expected at $BASELINE" >&2
    exit 2
fi

# Same scale as the frozen baseline. Do NOT change these — the rubric is
# calibrated to the L2 500-round window.
echo "==> discovery sweep (fastapi 0.141.1, L1=5000, L2=500, L3=50 x variants)"
bse run fastapi \
    --version 0.141.1 \
    --iterations 5000 \
    --rounds-l2 500 \
    --no-install \
    --out "$REPLAY_DIR" \
    >/dev/null

echo "==> replay summary"
cat "$REPLAY_DIR/summary.txt"

echo
echo "==> grading against baseline"
exec python3 "$HERE/grade.py" "$BASELINE" "$REPLAY_JSON"
