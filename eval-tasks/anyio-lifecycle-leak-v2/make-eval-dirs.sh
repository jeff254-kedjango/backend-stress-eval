#!/usr/bin/env bash
# make-eval-dirs.sh — assemble clean, leak-free working dirs for the v2 run.
#
# Each ~/anyio-eval-task-<TAG> gets ONLY what a task-taker is allowed to see:
#   - initial-prompt.md   (v2, symptom-only)
#   - minimal_repro.py
#   - .venv/ with anyio==4.14.2 installed (something to patch)
#
# It copies ONLY the prompt + reproducer. The leak-guard below also refuses to
# build if any answer-revealing file (a grader, rubric, baseline, or a
# measurement/attribution helper) somehow lands in the model's dir.
#
# The target dir name contains "anyio" (that's the task) but NOT
# "lifecycle-leak" or "backend-stress-eval", so the paranoia gate stays clean.
#
# Run this from the harness repo BEFORE hiding the repo for the run. Usage:
#   ./make-eval-dirs.sh A B      # builds ~/anyio-eval-task-A and -B
# Defaults to A B if no tags given.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
TAGS=("${@:-A B}")
# Re-split in case caller passed "A B" as one arg via the default.
read -r -a TAGS <<<"${TAGS[*]}"

ANYIO_VERSION="4.14.2"
PY="python3.12"

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "error: $PY not on PATH — the eval targets Python 3.12" >&2
    exit 2
fi

for f in initial-prompt.md minimal_repro.py; do
    if [[ ! -f "$HERE/$f" ]]; then
        echo "error: expected source file missing: $HERE/$f" >&2
        exit 2
    fi
done

# Guard: the prompt the model sees must be the symptom-only v2 prompt.
# Fail loudly if it still contains the v1 giveaways.
for banned in "worker pool" "worker thread" "event loop" "loop boundaries" "async runtime"; do
    if grep -qiF "$banned" "$HERE/initial-prompt.md"; then
        echo "error: initial-prompt.md contains a v1 giveaway phrase: '$banned'" >&2
        echo "       refusing to build a leaky eval dir. Fix the prompt first." >&2
        exit 2
    fi
done

for TAG in "${TAGS[@]}"; do
    DEST="$HOME/anyio-eval-task-${TAG}"
    if [[ -e "$DEST" ]]; then
        echo "error: $DEST already exists — remove it before rebuilding" >&2
        exit 2
    fi
    echo "==> building $DEST"
    mkdir -p "$DEST"
    cp "$HERE/initial-prompt.md" "$DEST/initial-prompt.md"
    cp "$HERE/minimal_repro.py" "$DEST/minimal_repro.py"

    echo "    creating venv + installing anyio==${ANYIO_VERSION}"
    "$PY" -m venv "$DEST/.venv"
    "$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null
    "$DEST/.venv/bin/python" -m pip install --quiet "anyio==${ANYIO_VERSION}" >/dev/null

    # Sanity: leak reproduces here, and none of the grading files leaked in.
    if ! "$DEST/.venv/bin/python" -c "import anyio" >/dev/null 2>&1; then
        echo "error: anyio not importable in $DEST/.venv" >&2
        exit 2
    fi
    for leaked in measure.py RUBRIC.md grade.py bench.py bench-floor.json \
        baseline-attribution.json diagnosis.md; do
        if [[ -e "$DEST/$leaked" ]]; then
            echo "error: leak — $leaked ended up in $DEST" >&2
            exit 2
        fi
    done
    echo "    OK: $DEST ready (prompt + minimal_repro.py + anyio ${ANYIO_VERSION})"
done

echo
echo "Built: ${TAGS[*]/#/\~/anyio-eval-task-}"
echo "Next: hide the harness repo, then run 'claude' inside each dir."
