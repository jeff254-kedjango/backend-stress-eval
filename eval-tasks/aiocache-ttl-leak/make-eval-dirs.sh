#!/usr/bin/env bash
# make-eval-dirs.sh — assemble clean, answer-free working dirs for the run.
#
# Each ~/aiocache-eval-task-<TAG> gets ONLY what a task-taker may see:
#   - initial-prompt.md   (symptom-only)
#   - minimal_repro.py
#   - .venv/ with aiocache==0.12.3 installed (something to patch)
#
# The leak-guard below refuses to build if the prompt names the cause, or if
# any answer-revealing file (grader/rubric/findings) lands in the model's dir.
#
# The target dir name contains "aiocache" (that's the task) but NOT
# "ttl-leak" or "backend-stress-eval", so the paranoia gate stays clean.
#
# Run from the harness repo BEFORE hiding the repo. Usage:
#   ./make-eval-dirs.sh A B      # builds ~/aiocache-eval-task-A and -B
# Defaults to A B if no tags given.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
read -r -a TAGS <<<"${*:-A B}"

AIOCACHE_VERSION="0.12.3"
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

# Guard: the prompt must not name the cause. Fail loudly on any giveaway that
# would let the model skip localization (the whole point of the task).
for banned in "_handlers" "TimerHandle" "timer handle" "call_later" \
    "cancel" "SimpleMemoryBackend" "_cache" "handler dict" "pop"; do
    if grep -qiF "$banned" "$HERE/initial-prompt.md"; then
        echo "error: initial-prompt.md contains a cause-revealing phrase: '$banned'" >&2
        echo "       refusing to build a leaky eval dir. Fix the prompt first." >&2
        exit 2
    fi
done

for TAG in "${TAGS[@]}"; do
    DEST="$HOME/aiocache-eval-task-${TAG}"
    if [[ -e "$DEST" ]]; then
        echo "error: $DEST already exists — remove it before rebuilding" >&2
        exit 2
    fi
    echo "==> building $DEST"
    mkdir -p "$DEST"
    cp "$HERE/initial-prompt.md" "$DEST/initial-prompt.md"
    cp "$HERE/minimal_repro.py" "$DEST/minimal_repro.py"

    echo "    creating venv + installing aiocache==${AIOCACHE_VERSION}"
    "$PY" -m venv "$DEST/.venv"
    "$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null
    "$DEST/.venv/bin/python" -m pip install --quiet "aiocache==${AIOCACHE_VERSION}" >/dev/null

    if ! "$DEST/.venv/bin/python" -c "import aiocache" >/dev/null 2>&1; then
        echo "error: aiocache not importable in $DEST/.venv" >&2
        exit 2
    fi
    for leaked in measure.py RUBRIC.md grade.py grading-criteria.md \
        findings.md README.md probes.py; do
        if [[ -e "$DEST/$leaked" ]]; then
            echo "error: leak — $leaked ended up in $DEST" >&2
            exit 2
        fi
    done
    echo "    OK: $DEST ready (prompt + minimal_repro.py + aiocache ${AIOCACHE_VERSION})"
done

echo
echo "Built: ${TAGS[*]/#/\~/aiocache-eval-task-}"
echo "Next: hide the harness repo, then run 'claude' inside each dir."
