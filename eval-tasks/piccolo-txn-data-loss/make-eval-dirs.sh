#!/usr/bin/env bash
# make-eval-dirs.sh — assemble clean, answer-free working dirs for the run.
#
# Each ~/piccolo-eval-task-<TAG> gets ONLY what a task-taker may see:
#   - initial-prompt.md   (symptom-only — the ONE file the model receives)
#   - .venv/ with piccolo[sqlite]==1.36.0 installed (something to patch)
#
# NO reproducer is shipped. Handing the model a runnable minimal_repro.py
# pre-localizes the bug — it does the hardest part of the L3 task (finding
# where the loss originates) for the model, which collapses every task to
# "both pass, no differentiation". The model gets symptoms only and must
# localize the loss itself. The bug-is-live sanity check below runs an
# INLINE probe against the fresh venv; it is never written to the model dir.
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

if [[ ! -f "$HERE/initial-prompt.md" ]]; then
    echo "error: expected source file missing: $HERE/initial-prompt.md" >&2
    exit 2
fi

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

# INLINE bug-is-live probe. Mirrors the RETRY gate in
# scripts/grade_piccolo_evals.sh. Run against the fresh venv to confirm the
# loss is present BEFORE the model touches it. NEVER written to the model dir.
read -r -d '' LIVE_PROBE <<'PY' || true
import asyncio, os, tempfile
from piccolo.columns import Varchar
from piccolo.engine.sqlite import SQLiteEngine
from piccolo.table import Table

async def main():
    p = tempfile.mktemp(suffix=".sqlite")
    DB = SQLiteEngine(path=p)
    class Rec(Table, db=DB):
        ref = Varchar()
    await Rec.create_table(if_not_exists=True)
    r = Rec(ref="retry")
    try:
        async with DB.transaction():
            await r.save(); raise RuntimeError("x")
    except RuntimeError:
        pass
    await r.save()
    stored = await Rec.count().where(Rec.ref == "retry")
    os.path.exists(p) and os.unlink(p)
    print(f"stored:{stored}")

asyncio.run(main())
PY

for TAG in "${TAGS[@]}"; do
    DEST="$HOME/piccolo-eval-task-${TAG}"
    if [[ -e "$DEST" ]]; then
        echo "error: $DEST already exists — remove it before rebuilding" >&2
        exit 2
    fi
    echo "==> building $DEST"
    mkdir -p "$DEST"
    cp "$HERE/initial-prompt.md" "$DEST/initial-prompt.md"

    echo "    creating venv + installing piccolo[sqlite]==${PICCOLO_VERSION}"
    "$PY" -m venv "$DEST/.venv"
    "$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null
    "$DEST/.venv/bin/python" -m pip install --quiet "piccolo[sqlite]==${PICCOLO_VERSION}" >/dev/null

    if ! "$DEST/.venv/bin/python" -c "import piccolo, aiosqlite" >/dev/null 2>&1; then
        echo "error: piccolo/aiosqlite not importable in $DEST/.venv" >&2
        exit 2
    fi
    # Sanity: the loss must actually be present on this fresh venv. Probe runs
    # inline — nothing is written into $DEST beyond the prompt.
    out="$("$DEST/.venv/bin/python" -c "$LIVE_PROBE" 2>/dev/null || true)"
    if [[ "$out" != *"stored:0"* ]]; then
        echo "error: bug not live in $DEST (probe got: $out, expected stored:0)" >&2
        exit 2
    fi
    # The model dir must contain ONLY the prompt + venv. Anything else — a
    # reproducer, rubric, findings, or grader — is a leak.
    for leaked in minimal_repro.py measure.py RUBRIC.md grade.py \
        grading-criteria.md findings.md README.md RUN.md probes.py; do
        if [[ -e "$DEST/$leaked" ]]; then
            echo "error: leak — $leaked ended up in $DEST" >&2
            exit 2
        fi
    done
    echo "    OK: $DEST ready (prompt + piccolo ${PICCOLO_VERSION}); loss confirmed inline"
done

echo
echo "Built: ${TAGS[*]/#/\~/piccolo-eval-task-}"
echo "Next: hide the harness repo, then run 'claude' inside each dir."
