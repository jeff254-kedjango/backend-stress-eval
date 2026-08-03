#!/usr/bin/env bash
# make-eval-dirs.sh — assemble clean, leak-free working dirs for the v2 run.
#
# Each ~/anyio-eval-task-<TAG> gets ONLY what a task-taker is allowed to see:
#   - initial-prompt.md   (v2, symptom-only — the ONE file the model receives)
#   - .venv/ with anyio==4.14.2 installed (something to patch)
#
# NO reproducer is shipped. Handing the model a runnable minimal_repro.py
# pre-localizes the leak and collapses the task to "both pass". The model gets
# symptoms only and must localize the leak itself. The bug-is-live check below
# is an INLINE probe run against the fresh venv (counts retained event loops,
# the un-foolable signal); it is never written to the model dir.
#
# It copies ONLY the prompt. The leak-guard below also refuses to build if any
# answer-revealing file (a grader, rubric, baseline, a reproducer, or a
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

if [[ ! -f "$HERE/initial-prompt.md" ]]; then
    echo "error: expected source file missing: $HERE/initial-prompt.md" >&2
    exit 2
fi

# Guard: the prompt the model sees must be the symptom-only v2 prompt.
# Fail loudly if it still contains the v1 giveaways.
for banned in "worker pool" "worker thread" "event loop" "loop boundaries" "async runtime"; do
    if grep -qiF "$banned" "$HERE/initial-prompt.md"; then
        echo "error: initial-prompt.md contains a v1 giveaway phrase: '$banned'" >&2
        echo "       refusing to build a leaky eval dir. Fix the prompt first." >&2
        exit 2
    fi
done

# INLINE bug-is-live probe. The un-foolable signal (per the reproducer's own
# analysis): stock anyio 4.14.2 retains one event loop per anyio.run() call
# instead of releasing it, so live-loop count climbs 1:1 with iterations. Run
# 60 iterations and confirm many loops are retained. NEVER written to the dir.
read -r -d '' LIVE_PROBE <<'PY' || true
import anyio, asyncio, gc

async def work():
    await anyio.to_thread.run_sync(int)

for _ in range(60):
    anyio.run(work)

gc.collect()
loops = sum(1 for o in gc.get_objects() if isinstance(o, asyncio.AbstractEventLoop))
print(f"loops:{loops}")
PY

for TAG in "${TAGS[@]}"; do
    DEST="$HOME/anyio-eval-task-${TAG}"
    if [[ -e "$DEST" ]]; then
        echo "error: $DEST already exists — remove it before rebuilding" >&2
        exit 2
    fi
    echo "==> building $DEST"
    mkdir -p "$DEST"
    cp "$HERE/initial-prompt.md" "$DEST/initial-prompt.md"

    echo "    creating venv + installing anyio==${ANYIO_VERSION}"
    "$PY" -m venv "$DEST/.venv"
    "$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null
    "$DEST/.venv/bin/python" -m pip install --quiet "anyio==${ANYIO_VERSION}" >/dev/null

    if ! "$DEST/.venv/bin/python" -c "import anyio" >/dev/null 2>&1; then
        echo "error: anyio not importable in $DEST/.venv" >&2
        exit 2
    fi
    # Sanity: the leak must actually be present on this fresh venv. Probe runs
    # inline — nothing is written into $DEST beyond the prompt. 60 iterations
    # on stock anyio retain ~60 loops; a value near 1 would mean no leak.
    out="$("$DEST/.venv/bin/python" -c "$LIVE_PROBE" 2>/dev/null || true)"
    loops="${out#loops:}"
    if [[ "$out" != loops:* || "${loops:-0}" -lt 30 ]]; then
        echo "error: leak not live in $DEST (probe got: $out, expected loops:~60)" >&2
        exit 2
    fi
    # The model dir must contain ONLY the prompt + venv. A reproducer, grader,
    # rubric, baseline, or measurement helper landing here is a leak.
    for leaked in minimal_repro.py measure.py RUBRIC.md grade.py bench.py \
        bench-floor.json baseline-attribution.json diagnosis.md; do
        if [[ -e "$DEST/$leaked" ]]; then
            echo "error: leak — $leaked ended up in $DEST" >&2
            exit 2
        fi
    done
    echo "    OK: $DEST ready (prompt + anyio ${ANYIO_VERSION}); leak confirmed inline (loops=$loops)"
done

echo
echo "Built: ${TAGS[*]/#/\~/anyio-eval-task-}"
echo "Next: hide the harness repo, then run 'claude' inside each dir."
