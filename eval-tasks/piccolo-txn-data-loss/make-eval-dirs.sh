#!/usr/bin/env bash
# make-eval-dirs.sh — assemble clean, answer-free working dirs for the run.
#
# Each ~/piccolo-eval-task-<TAG> gets ONLY what a task-taker may see:
#   - initial-prompt.md   (symptom-only)
#   - minimal_repro.py
#   - .venv/ with piccolo[sqlite]==1.36.0 installed (something to patch)
#
# The guard refuses to build if the prompt names the cause, or if any
# answer-revealing file lands in the model's dir.
#
# The target dir name contains "piccolo" (that's the task) but NOT
# "txn-data-loss" or "backend-stress-eval", so the paranoia gate stays clean.
#
# Run from the harness repo BEFORE hiding the repo. Usage:
#   ./make-eval-dirs.sh A B      # builds ~/piccolo-eval-task-A and -B
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
read -r -a TAGS <<<"${*:-A B}"

PICCOLO_VERSION="1.36.0"
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

# Guard: the prompt must reveal only the SYMPTOM. Any term that points at the
# cause (the stale persistence flag, the insert-vs-update decision, the phantom
# id, or the rollback-reset fix) fails the build. The whole task is that the
# model localizes the loss itself.
for banned in \
    "_exists_in_db" "exists_in_db" "_exists" \
    "insert" "update path" "insert vs" "update vs" "in place" "in-place" \
    "primary key" "phantom" "flag" "stale" \
    "identity" "re-insert" "reinsert" "rollback-reset" \
    "save() branch" "back-reference" "back reference" "__slots__" \
    "current_transaction" "register_inserted"; do
    if grep -qiF "$banned" "$HERE/initial-prompt.md"; then
        echo "error: initial-prompt.md contains a cause-revealing phrase: '$banned'" >&2
        echo "       refusing to build a hinted eval dir. Fix the prompt first." >&2
        exit 2
    fi
done

for TAG in "${TAGS[@]}"; do
    DEST="$HOME/piccolo-eval-task-${TAG}"
    if [[ -e "$DEST" ]]; then
        echo "error: $DEST already exists — remove it before rebuilding" >&2
        exit 2
    fi
    echo "==> building $DEST"
    mkdir -p "$DEST"
    cp "$HERE/initial-prompt.md" "$DEST/initial-prompt.md"
    cp "$HERE/minimal_repro.py" "$DEST/minimal_repro.py"

    echo "    creating venv + installing piccolo[sqlite]==${PICCOLO_VERSION}"
    "$PY" -m venv "$DEST/.venv"
    "$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null
    "$DEST/.venv/bin/python" -m pip install --quiet "piccolo[sqlite]==${PICCOLO_VERSION}" >/dev/null

    if ! "$DEST/.venv/bin/python" -c "import piccolo, aiosqlite" >/dev/null 2>&1; then
        echo "error: piccolo/aiosqlite not importable in $DEST/.venv" >&2
        exit 2
    fi
    # Sanity: the reproducer must actually exhibit the loss on this fresh venv.
    out="$("$DEST/.venv/bin/python" "$DEST/minimal_repro.py" 2>/dev/null || true)"
    if [[ "$out" != *"orders stored: 0"* ]]; then
        echo "error: reproducer did not exhibit the loss in $DEST (got: $out)" >&2
        exit 2
    fi
    for leaked in measure.py RUBRIC.md grade.py grading-criteria.md \
        findings.md README.md probes.py; do
        if [[ -e "$DEST/$leaked" ]]; then
            echo "error: leak — $leaked ended up in $DEST" >&2
            exit 2
        fi
    done
    echo "    OK: $DEST ready (prompt + minimal_repro.py + piccolo ${PICCOLO_VERSION}); loss confirmed"
done

echo
echo "Built: ${TAGS[*]/#/\~/piccolo-eval-task-}"
echo "Next: hide the harness repo, then run 'claude' inside each dir."
