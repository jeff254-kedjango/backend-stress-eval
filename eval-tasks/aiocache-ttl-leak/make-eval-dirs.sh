#!/usr/bin/env bash
# make-eval-dirs.sh — assemble clean, answer-free working dirs for the run.
#
# Each ~/aiocache-eval-task-<TAG> gets ONLY what a task-taker may see:
#   - initial-prompt.md   (symptom-only — the ONE file the model receives)
#   - .venv/ with aiocache==0.12.3 installed (something to patch)
#
# NO reproducer is shipped. Handing the model a runnable minimal_repro.py
# pre-localizes the bug and collapses the task to "both pass, no
# differentiation". The model gets symptoms only and must localize the leak
# itself. The bug-is-live check below is an INLINE probe run against the fresh
# venv; it is never written to the model dir. (The aiocache grader uses its own
# independent PROBE, so it never reads a model-side repro either.)
#
# The leak-guard below refuses to build if the prompt names the cause, or if
# any answer-revealing file (grader/rubric/findings/repro) lands in the model's dir.
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

if [[ ! -f "$HERE/initial-prompt.md" ]]; then
    echo "error: expected source file missing: $HERE/initial-prompt.md" >&2
    exit 2
fi

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

# INLINE bug-is-live probe. Mirrors the grader's HANDLERS signal: on stock
# aiocache 0.12.3 the internal handler dict grows once per ttl'd set and never
# shrinks on a no-ttl refresh, so it climbs unbounded. Confirm that growth is
# present in the fresh venv BEFORE the model touches it. NEVER written to the
# model dir.
read -r -d '' LIVE_PROBE <<'PY' || true
import asyncio
from aiocache import Cache

async def main():
    cache = Cache(Cache.MEMORY)
    for i in range(2000):
        k = f"user:{i}:session"
        await cache.set(k, {"n": i}, ttl=30)   # write with expiry
        await cache.set(k, {"n": i, "seen": 1})  # refresh, no ttl
    handlers = len(getattr(cache, "_handlers", {}))
    print(f"handlers:{handlers}")

asyncio.run(main())
PY

for TAG in "${TAGS[@]}"; do
    DEST="$HOME/aiocache-eval-task-${TAG}"
    if [[ -e "$DEST" ]]; then
        echo "error: $DEST already exists — remove it before rebuilding" >&2
        exit 2
    fi
    echo "==> building $DEST"
    mkdir -p "$DEST"
    cp "$HERE/initial-prompt.md" "$DEST/initial-prompt.md"

    echo "    creating venv + installing aiocache==${AIOCACHE_VERSION}"
    "$PY" -m venv "$DEST/.venv"
    "$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null
    "$DEST/.venv/bin/python" -m pip install --quiet "aiocache==${AIOCACHE_VERSION}" >/dev/null

    if ! "$DEST/.venv/bin/python" -c "import aiocache" >/dev/null 2>&1; then
        echo "error: aiocache not importable in $DEST/.venv" >&2
        exit 2
    fi
    # Sanity: the leak must actually be present on this fresh venv. Probe runs
    # inline — nothing is written into $DEST beyond the prompt. With 2000 ttl'd
    # sets, stock aiocache retains a handler per key (~2000); a value near 0
    # would mean the leak isn't live.
    out="$("$DEST/.venv/bin/python" -c "$LIVE_PROBE" 2>/dev/null || true)"
    handlers="${out#handlers:}"
    if [[ "$out" != handlers:* || "${handlers:-0}" -lt 1000 ]]; then
        echo "error: leak not live in $DEST (probe got: $out, expected handlers:~2000)" >&2
        exit 2
    fi
    # The model dir must contain ONLY the prompt + venv. Anything else — a
    # reproducer, rubric, findings, or grader — is a leak.
    for leaked in minimal_repro.py measure.py RUBRIC.md grade.py \
        grading-criteria.md findings.md README.md probes.py; do
        if [[ -e "$DEST/$leaked" ]]; then
            echo "error: leak — $leaked ended up in $DEST" >&2
            exit 2
        fi
    done
    echo "    OK: $DEST ready (prompt + aiocache ${AIOCACHE_VERSION}); leak confirmed inline (handlers=$handlers)"
done

echo
echo "Built: ${TAGS[*]/#/\~/aiocache-eval-task-}"
echo "Next: hide the harness repo, then run 'claude' inside each dir."
