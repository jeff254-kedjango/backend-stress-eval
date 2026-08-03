#!/usr/bin/env bash
# Grade the anyio lifecycle-leak eval: compare model-A vs model-B outputs.
#
# Independent probe (mirrors grade_aiocache_evals.sh / grade_piccolo_evals.sh):
# the grader does NOT run the model's reproducer — no reproducer is shipped
# under the prompt-only protocol. Instead it runs its own inline workload
# against each model's patched anyio and reads the un-foolable signal:
#
#   LOOPS=<n>  — event loops retained after 200 anyio.run() calls. Stock 4.14.2
#                retains one per call (~200, leak live). A correct fix releases
#                each loop, so this settles low (single digits). This is the
#                pass/fail gate: LEAK=PASS iff LOOPS is small.
#   RUN=<PASS|FAIL> — the workload still runs (fix didn't break the API).
#
# Plus a diff vs a clean anyio 4.14.2 baseline (fix size/location signal).
set -euo pipefail

EVAL_DIR="${1:-$HOME/eval-outputs}"

# Locate model output directories (anyio-tagged first, then bare model-*).
find_dir() { find "$EVAL_DIR" -maxdepth 1 -type d -name "$1" 2>/dev/null | sort -r | head -n1; }
MODEL_A_DIR=$(find_dir "*anyio*model-A*"); [[ -z "$MODEL_A_DIR" ]] && MODEL_A_DIR=$(find_dir "model-A*")
MODEL_B_DIR=$(find_dir "*anyio*model-B*"); [[ -z "$MODEL_B_DIR" ]] && MODEL_B_DIR=$(find_dir "model-B*")
[[ -z "$MODEL_A_DIR" ]] && MODEL_A_DIR="$HOME/anyio-eval-task-A"
[[ -z "$MODEL_B_DIR" ]] && MODEL_B_DIR="$HOME/anyio-eval-task-B"

if [[ ! -d "$MODEL_A_DIR" || ! -d "$MODEL_B_DIR" ]]; then
    echo "Error: could not locate model A/B dirs (looked in $EVAL_DIR and ~/anyio-eval-task-*)"
    exit 1
fi

# Clean reference baseline for the diff column.
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT
python3 -m venv "$TEMP_DIR/clean_venv" >/dev/null 2>&1
"$TEMP_DIR/clean_venv/bin/pip" install -q anyio==4.14.2 >/dev/null 2>&1
CLEAN_ASYNCIO=$(find "$TEMP_DIR/clean_venv" -name "_asyncio.py" -path "*/anyio/_backends/*" | head -n1)

# The grading probe: independent workload, reports RUN=<..> LOOPS=<n>.
read -r -d '' PROBE <<'PY' || true
import anyio, asyncio, gc

async def work():
    await anyio.to_thread.run_sync(int)

try:
    for _ in range(200):
        anyio.run(work)
    run_ok = True
except Exception:
    run_ok = False

gc.collect()
loops = sum(1 for o in gc.get_objects() if isinstance(o, asyncio.AbstractEventLoop))
print(f"RUN={'PASS' if run_ok else 'FAIL'} LOOPS={loops}")
PY

evaluate_model() {
    local name="$1" dir="$2"
    local py="$dir/.venv/bin/python"
    local model_asyncio
    model_asyncio=$(find "$dir/.venv" -name "_asyncio.py" -path "*/anyio/_backends/*" 2>/dev/null | head -n1)

    local result="RUN=MISSING LOOPS=?"
    if [[ -x "$py" ]]; then
        result=$(timeout 60 "$py" -c "$PROBE" 2>/dev/null | tr '\n' ' ' || echo "RUN=CRASH LOOPS=?")
    fi

    # Derive LEAK verdict: fixed iff retained loops stay small (<=5).
    local loops="${result##*LOOPS=}"; loops="${loops%% *}"
    local leak="?"
    if [[ "$loops" =~ ^[0-9]+$ ]]; then
        if [[ "$loops" -le 5 ]]; then leak="PASS"; else leak="FAIL"; fi
    fi

    local added=0 removed=0
    if [[ -n "$model_asyncio" && -f "$model_asyncio" && -f "$CLEAN_ASYNCIO" ]]; then
        added=$(diff -u "$CLEAN_ASYNCIO" "$model_asyncio" | grep -c "^+[^+]" || true)
        removed=$(diff -u "$CLEAN_ASYNCIO" "$model_asyncio" | grep -c "^-[^-]" || true)
    fi
    echo "$name|LEAK=$leak $result|+$added / -$removed"
}

res_a=$(evaluate_model "Model A" "$MODEL_A_DIR")
res_b=$(evaluate_model "Model B" "$MODEL_B_DIR")

echo ""
echo "========================================================================"
echo "               ANYIO LIFECYCLE-LEAK — EVALUATION SUMMARY               "
echo "========================================================================"
printf "%-9s | %-34s | %s\n" "Model" "Leak / Run / Loops" "Diff"
echo "----------+------------------------------------+------------------------"
for r in "$res_a" "$res_b"; do
    IFS='|' read -r n status diff <<< "$r"
    printf "%-9s | %-34s | %s\n" "$n" "$status" "$diff"
done
echo "========================================================================"
echo "PASS = LEAK=PASS (<=5 loops retained after 200 runs) and RUN=PASS."
echo "The grader runs its OWN workload — no model-side reproducer is used."
echo "Diff is only _asyncio.py; a full fix may touch other anyio files — review by hand."
