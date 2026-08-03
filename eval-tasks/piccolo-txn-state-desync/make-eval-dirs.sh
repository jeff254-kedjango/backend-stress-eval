#!/usr/bin/env bash
# make-eval-dirs.sh — assemble clean, answer-free working dirs for the run.
#
# Each ~/piccolo-desync-eval-task-<TAG> gets ONLY what a task-taker may see:
#   - initial-prompt.md   (symptom-only — the ONE file the model receives)
#   - .venv/ with piccolo[sqlite]==1.36.0 installed (something to patch)
#
# NO reproducer is shipped (Rule 10). A runnable repro pre-localizes the bug and
# collapses the task to "both pass". The model gets symptoms only and must
# localize the three desync symptoms itself. The bug-is-live check below is an
# INLINE probe run against the fresh venv; it is never written to the model dir.
# (The grader `scripts/grade_piccolo_desync_evals.sh` uses its own independent
# probe, so it never reads a model-side repro either.)
#
# The guard refuses to build if the prompt names the cause, or if any
# answer-revealing file lands in the model's dir.
#
# The target dir name contains "piccolo" + "desync" (that's the task) but NOT
# "state-desync" (the eval-tasks path) or "backend-stress-eval", so the paranoia
# gate stays clean.
#
# Run from the harness repo BEFORE hiding the repo. Usage:
#   ./make-eval-dirs.sh A B      # builds ~/piccolo-desync-eval-task-A and -B
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

# Guard: the prompt must reveal only the SYMPTOMS. Any term that points at the
# cause (the stale persistence flag, insert/update/delete decision, phantom id,
# savepoint/rollback internals, or the back-reference fix) fails the build.
for banned in \
    "_exists_in_db" "exists_in_db" "_exists" \
    "insert" "update" "in place" "in-place" \
    "primary key" "phantom" "flag" "stale" \
    "identity" "re-insert" "reinsert" "rollback-reset" \
    "back-reference" "back reference" "__slots__" \
    "current_transaction" "register" "savepoint" "rollback_to" \
    "instance state" "in-memory" "callback"; do
    if grep -qiF "$banned" "$HERE/initial-prompt.md"; then
        echo "error: initial-prompt.md contains a cause-revealing phrase: '$banned'" >&2
        echo "       refusing to build a hinted eval dir. Fix the prompt first." >&2
        exit 2
    fi
done

# INLINE bug-is-live probe. Asserts all THREE desync symptoms are present on the
# fresh venv (mirrors the grader's three desync gates). NEVER written to the dir.
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

    # 1. insert-loss: rolled-back save then re-save → 0 rows (lost)
    r = Rec(ref="ins")
    try:
        async with DB.transaction():
            await r.save(); raise RuntimeError("x")
    except RuntimeError: pass
    await r.save()
    ins = await Rec.count().where(Rec.ref == "ins")

    # 2. remove-dup: rolled-back remove then re-save → 2 rows (duplicate)
    d = Rec(ref="rem"); await d.save()
    try:
        async with DB.transaction():
            await d.remove(); raise RuntimeError("x")
    except RuntimeError: pass
    await d.save()
    rem = await Rec.count().where(Rec.ref == "rem")

    # 3. savepoint-loss: save after rollback_to then re-save → 0 rows (lost)
    async with DB.transaction() as txn:
        keep = Rec(ref="k"); await keep.save()
        sp = await txn.savepoint()
        b = Rec(ref="sp"); await b.save()
        await sp.rollback_to()
    await b.save()
    spl = await Rec.count().where(Rec.ref == "sp")

    os.path.exists(p) and os.unlink(p)
    # live == all three broken: insert lost(0), remove dup(2), savepoint lost(0)
    print(f"ins:{ins} rem:{rem} sp:{spl}")

asyncio.run(main())
PY

for TAG in "${TAGS[@]}"; do
    DEST="$HOME/piccolo-desync-eval-task-${TAG}"
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

    # Scrub the venv provenance line: `python3.12 -m venv` records the building
    # interpreter's path in pyvenv.cfg's `command =`. If $PY is the harness
    # venv's python, that path contains "backend-stress-eval" and would trip the
    # paranoia gate. Rewrite it to the resolved BASE interpreter (functionally
    # irrelevant — a comment — but keeps the dir answer-free).
    BASE="$("$DEST/.venv/bin/python" -c 'import sys; print(sys.base_prefix + "/bin/python3.12")')"
    CFG="$DEST/.venv/pyvenv.cfg"
    if [[ -f "$CFG" ]]; then
        tmp_cfg="$(mktemp)"
        while IFS= read -r line; do
            [[ "$line" == command\ =* ]] && line="command = $BASE -m venv $DEST/.venv"
            printf '%s\n' "$line"
        done < "$CFG" > "$tmp_cfg"
        mv "$tmp_cfg" "$CFG"
    fi
    if grep -qF "backend-stress-eval" "$CFG" 2>/dev/null; then
        echo "error: harness path still present in $CFG after scrub" >&2
        exit 2
    fi

    if ! "$DEST/.venv/bin/python" -c "import piccolo, aiosqlite" >/dev/null 2>&1; then
        echo "error: piccolo/aiosqlite not importable in $DEST/.venv" >&2
        exit 2
    fi
    # Sanity: all three symptoms must be live on this fresh venv. Probe inline.
    out="$("$DEST/.venv/bin/python" -c "$LIVE_PROBE" 2>/dev/null || true)"
    if [[ "$out" != "ins:0 rem:2 sp:0" ]]; then
        echo "error: not all three bugs live in $DEST (probe got: '$out', expected 'ins:0 rem:2 sp:0')" >&2
        exit 2
    fi
    # The model dir must contain ONLY the prompt + venv.
    for leaked in minimal_repro.py measure.py RUBRIC.md grade.py \
        grading-criteria.md findings.md findings-remove.md README.md RUN.md probes.py; do
        if [[ -e "$DEST/$leaked" ]]; then
            echo "error: leak — $leaked ended up in $DEST" >&2
            exit 2
        fi
    done
    echo "    OK: $DEST ready (prompt + piccolo ${PICCOLO_VERSION}); all 3 desync bugs confirmed inline"
done

echo
echo "Built: ${TAGS[*]/#/\~/piccolo-desync-eval-task-}"
echo "Next: hide the harness repo, then run 'claude' inside each dir."
