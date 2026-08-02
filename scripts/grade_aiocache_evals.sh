#!/usr/bin/env bash
# Grade the aiocache TTL-handler-leak eval: compare model-A vs model-B outputs.
#
# Objective signals (mirrors scripts/grade_evals.sh for anyio):
#   1. Reproducer still runs (didn't break the API).
#   2. Correctness preserved: a real-TTL entry still expires; values read back
#      unchanged. A fix that disables expiry or corrupts data FAILS here.
#   3. Leak fixed: internal handler bookkeeping stays bounded over a long run
#      (the un-foolable signal — the shadow dict must not grow once per refresh).
#   4. Diff size/location vs a clean aiocache 0.12.3 baseline.
set -euo pipefail

EVAL_DIR="${1:-$HOME/eval-outputs}"

MODEL_A_DIR=$(find "$EVAL_DIR" -maxdepth 1 -type d -name "aiocache-*model-A*" -o -maxdepth 1 -type d -name "model-A*aiocache*" 2>/dev/null | sort -r | head -n1)
MODEL_B_DIR=$(find "$EVAL_DIR" -maxdepth 1 -type d -name "aiocache-*model-B*" -o -maxdepth 1 -type d -name "model-B*aiocache*" 2>/dev/null | sort -r | head -n1)
# Fallback to the clean-room build dirs if outputs not yet archived.
[[ -z "$MODEL_A_DIR" ]] && MODEL_A_DIR="$HOME/aiocache-eval-task-A"
[[ -z "$MODEL_B_DIR" ]] && MODEL_B_DIR="$HOME/aiocache-eval-task-B"

if [[ ! -d "$MODEL_A_DIR" || ! -d "$MODEL_B_DIR" ]]; then
    echo "Error: could not locate model A/B dirs (looked in $EVAL_DIR and ~/aiocache-eval-task-*)"
    exit 1
fi

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT
python3 -m venv "$TEMP_DIR/clean" >/dev/null 2>&1
"$TEMP_DIR/clean/bin/pip" install -q aiocache==0.12.3 >/dev/null 2>&1
CLEAN_MEM=$(find "$TEMP_DIR/clean" -name "memory.py" -path "*/aiocache/backends/*" | head -n1)

# The grading probe: runs against a model's python, reports
#   REPRO=<PASS|FAIL>  EXPIRE=<PASS|FAIL>  HANDLERS=<n>  RSS_GROWTH_KB=<n>
read -r -d '' PROBE <<'PY' || true
import asyncio, gc, resource, sys
import aiocache
from aiocache import Cache
import time
async def main():
    cache = Cache(Cache.MEMORY)
    # correctness: a real-TTL-only entry must still expire
    await cache.set("ephemeral", "v", ttl=1)
    await asyncio.sleep(1.3)
    expired = (await cache.get("ephemeral")) is None
    N=40000
    for i in range(N):
        k=f"user:{i}:session"
        await cache.set(k, {"n":i}, ttl=30)
        await cache.set(k, {"n":i,"seen":1})
        if i < 3 and (await cache.get(k)) != {"n":i,"seen":1}:
            print("REPRO=FAIL EXPIRE=? HANDLERS=? REFRESH_NS=?"); return
    gc.collect()
    handlers = len(getattr(cache, "_handlers", {}))
    # THROUGHPUT axis: with a large live keyset already present, time the
    # no-ttl refresh hot path. A correct fix is O(1)/op; a fix that rebuilds
    # cache-wide structures on every no-ttl set is O(keyset) and blows up here.
    M=3000
    t0=time.perf_counter_ns()
    for j in range(M):
        await cache.set(f"user:{j}:session", {"n":j,"seen":2})  # refresh, no ttl
    refresh_ns=(time.perf_counter_ns()-t0)//M
    print(f"REPRO=PASS EXPIRE={'PASS' if expired else 'FAIL'} HANDLERS={handlers} REFRESH_NS={refresh_ns}")
asyncio.run(main())
PY

grade_one() {
    local name="$1" dir="$2"
    local py="$dir/.venv/bin/python"
    local mem
    mem=$(find "$dir/.venv" -name "memory.py" -path "*/aiocache/backends/*" 2>/dev/null | head -n1)

    local result="REPRO=MISSING EXPIRE=? HANDLERS=? RSS_GROWTH_KB=?"
    if [[ -x "$py" ]]; then
        result=$(timeout 120 "$py" -c "$PROBE" 2>/dev/null || echo "REPRO=CRASH EXPIRE=? HANDLERS=? RSS_GROWTH_KB=?")
    fi

    local added=0 removed=0
    if [[ -n "$mem" && -f "$mem" && -f "$CLEAN_MEM" ]]; then
        added=$(diff -u "$CLEAN_MEM" "$mem" | grep -c "^+[^+]" || true)
        removed=$(diff -u "$CLEAN_MEM" "$mem" | grep -c "^-[^-]" || true)
    fi
    echo "$name|$result|+$added / -$removed"
}

res_a=$(grade_one "Model A" "$MODEL_A_DIR")
res_b=$(grade_one "Model B" "$MODEL_B_DIR")

echo ""
echo "======================================================================================"
echo "                 AIOCACHE TTL-LEAK — EVALUATION COMPARISON SUMMARY                    "
echo "======================================================================================"
printf "%-9s | %-58s | %s\n" "Model" "Repro / Expire / Handlers / Refresh-ns" "Diff"
echo "----------+------------------------------------------------------------+--------------"
for r in "$res_a" "$res_b"; do
    IFS='|' read -r n status diff <<< "$r"
    printf "%-9s | %-58s | %s\n" "$n" "$status" "$diff"
done
echo "======================================================================================"
echo "PASS = REPRO=PASS, EXPIRE=PASS, HANDLERS ~0. REFRESH_NS separates O(1) fixes from O(keyset) hot-path-teardown fixes."
