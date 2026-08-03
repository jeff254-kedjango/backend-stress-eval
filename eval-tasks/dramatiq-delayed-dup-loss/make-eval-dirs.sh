#!/usr/bin/env bash
# make-eval-dirs.sh — assemble clean, bug-live A/B working dirs for the
# delayed-job dup/loss task (dramatiq #431, pinned).
#
# Each ~/dramatiq-eval-task-<TAG> gets ONLY what a task-taker may see:
#   - initial-prompt.md   (symptom-only — the ONE doc the model receives)
#   - src/                (the third-party dependency's source, pinned, so the
#                          model has something to patch; git history STRIPPED so
#                          the issue/commit can't be looked up)
#   - .venv/              (that src/ installed EDITABLE, so edits take effect)
#
# NOTHING that reveals the answer is copied: no grading-criteria.md, no grade.py,
# no GRADER.md, no README.md, no repro. The leak-guard below refuses to build if
# any of those land in a model dir.
#
# Redis is shared, but each model gets its OWN db number so the two runs never
# touch each other's keys (A -> db 14, B -> db 15). The db number is written
# into a tiny env note in the dir AND the prompt tells the model to use it.
#
# The dir name contains "dramatiq" (unavoidable — the model will see the package
# name in src/) but NOT the issue number or "backend-stress-eval".
#
# Usage (run from THIS dir, before hiding the harness repo):
#   ./make-eval-dirs.sh A B      # builds ~/dramatiq-eval-task-A and -B
# Defaults to "A B".

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
read -r -a TAGS <<<"${*:-A B}"

PINNED_SHA="288dc2651e3e32da3769b69c285143da8466e4ab"
PINNED_SRC="/tmp/dramatiq431-pinned"     # clean checkout with git intact
GRADER="$HERE/../../investigations/dramatiq-431-delayed-dup/grade.py"
PY="python3.12"

# Assign a distinct Redis db per tag so the runs are isolated.
declare -A DB=( [A]=14 [B]=15 [C]=16 [D]=17 )

command -v "$PY" >/dev/null 2>&1 || { echo "error: $PY not on PATH" >&2; exit 2; }
command -v redis-cli >/dev/null 2>&1 || { echo "error: redis-cli not found" >&2; exit 2; }
redis-cli -h 127.0.0.1 ping >/dev/null 2>&1 || { echo "error: Redis not answering on 127.0.0.1:6379" >&2; exit 2; }

[[ -f "$HERE/initial-prompt.md" ]] || { echo "error: missing $HERE/initial-prompt.md" >&2; exit 2; }
[[ -f "$GRADER" ]] || { echo "error: grader not found at $GRADER" >&2; exit 2; }

# The pinned source must exist at the exact SHA. Rebuild it if absent.
if [[ ! -d "$PINNED_SRC/.git" ]] || [[ "$(git -C "$PINNED_SRC" rev-parse HEAD 2>/dev/null)" != "$PINNED_SHA" ]]; then
    echo "==> (re)creating pinned source at $PINNED_SRC @ ${PINNED_SHA:0:10}"
    rm -rf "$PINNED_SRC"
    git clone -q https://github.com/Bogdanp/dramatiq "$PINNED_SRC"
    git -C "$PINNED_SRC" checkout -q "$PINNED_SHA"
fi

# Guard: prompt must stay symptom-only. Fail loudly on giveaway phrases.
# Scan only the VISIBLE prompt — strip HTML comments (<!-- ... -->) first, since
# the DRAFT scaffolding comment legitimately names things the prompt must not.
# Also refuse to ship a file that still contains a DRAFT comment at all.
if grep -q '<!--' "$HERE/initial-prompt.md"; then
    echo "error: initial-prompt.md still contains an HTML comment (DRAFT scaffolding?) — remove it before building" >&2
    exit 2
fi
for banned in dramatiq redis heartbeat requeue "delay queue" atomic idempotent \
              "issue 431" "#431" lua eta acknowledge; do
    if grep -qiF "$banned" "$HERE/initial-prompt.md"; then
        echo "error: initial-prompt.md contains giveaway phrase: '$banned' — fix the prompt first" >&2
        exit 2
    fi
done

for TAG in "${TAGS[@]}"; do
    DEST="$HOME/dramatiq-eval-task-${TAG}"
    DBN="${DB[$TAG]:-}"
    [[ -n "$DBN" ]] || { echo "error: no Redis db assigned for tag '$TAG' (add it to the DB map)" >&2; exit 2; }
    [[ -e "$DEST" ]] && { echo "error: $DEST already exists — remove it before rebuilding (see restore steps)" >&2; exit 2; }

    echo "==> building $DEST  (Redis db $DBN)"
    mkdir -p "$DEST"
    cp "$HERE/initial-prompt.md" "$DEST/initial-prompt.md"

    # Copy the pinned source WITHOUT git history (so the fix/commit can't be
    # looked up), into src/.
    cp -r "$PINNED_SRC" "$DEST/src"
    rm -rf "$DEST/src/.git"

    # Record the assigned Redis db where the model (and grader) will look.
    echo "REDIS_DB=$DBN" > "$DEST/.redis-db"

    echo "    creating venv + editable install of src/"
    "$PY" -m venv "$DEST/.venv"
    "$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null
    "$DEST/.venv/bin/python" -m pip install --quiet redis >/dev/null
    "$DEST/.venv/bin/python" -m pip install --quiet -e "$DEST/src" >/dev/null

    "$DEST/.venv/bin/python" -c "import dramatiq" >/dev/null 2>&1 \
        || { echo "error: dramatiq not importable in $DEST/.venv" >&2; exit 2; }

    # INLINE bug-is-live probe: grade the FRESH src with the real grader against
    # this model's db. Must report the bug (verdict FAIL, DUP fails / dup==2).
    # The grader is NEVER copied into $DEST — it runs from the harness repo.
    echo "    probing bug-is-live (grader vs fresh src, db $DBN)"
    probe="$("$DEST/.venv/bin/python" "$GRADER" --pkg "$DEST/src" --db "$DBN" 2>/dev/null || true)"
    echo "      $probe"
    case "$probe" in
        *'"DUP_gate": "FAIL"'*'"dup_promotions": 2'*|*'"dup_promotions": 2'*'"DUP_gate": "FAIL"'*) : ;;
        *) echo "error: bug NOT live in $DEST (probe: $probe). Refusing to ship a non-buggy dir." >&2; exit 2 ;;
    esac
    redis-cli -h 127.0.0.1 -n "$DBN" flushdb >/dev/null   # leave the model a clean db

    # Leak-guard: the model dir must NOT contain any HARNESS answer-revealing
    # file. NOTE: we check the DEST top level only — the dependency's own src/
    # legitimately ships its own README/docs (those describe the library, not
    # our fix, and a real developer would have them), so src/ is NOT scanned.
    for leaked in grade.py GRADER.md grading-criteria.md RUN.md \
                  results.md make-eval-dirs.sh HUNT-STATE.md BACKPOCKET.md; do
        if [[ -e "$DEST/$leaked" ]]; then
            echo "error: leak — $leaked ended up in $DEST top level" >&2; exit 2
        fi
    done
    # Belt-and-suspenders: no stray grader/rubric anywhere under the dir.
    if find "$DEST" -maxdepth 3 \( -name grade.py -o -name GRADER.md \
            -o -name grading-criteria.md \) | grep -q .; then
        echo "error: leak — a grader/rubric file is present somewhere under $DEST" >&2; exit 2
    fi

    echo "    OK: $DEST ready (prompt + editable src + venv); bug confirmed live; db $DBN clean"
done

echo
echo "Built: ${TAGS[*]/#/\~/dramatiq-eval-task-}"
echo "Contents each model may see:  initial-prompt.md  src/  .venv/  .redis-db"
echo "Next: hide the harness repo, then run the model inside each dir."
echo "To GRADE after a run (from the harness repo, NOT copied into the dir):"
echo "  .venv/bin/python investigations/dramatiq-431-delayed-dup/grade.py \\"
echo "      --pkg ~/dramatiq-eval-task-A/src --db 14"
