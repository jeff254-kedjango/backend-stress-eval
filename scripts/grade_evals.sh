#!/usr/bin/env bash
set -euo pipefail

EVAL_DIR="${1:-$HOME/eval-outputs}"

# Locate model output directories
MODEL_A_DIR=$(find "$EVAL_DIR" -maxdepth 1 -type d -name "model-A*" | sort -r | head -n 1)
MODEL_B_DIR=$(find "$EVAL_DIR" -maxdepth 1 -type d -name "model-B*" | sort -r | head -n 1)

if [[ -z "$MODEL_A_DIR" || -z "$MODEL_B_DIR" ]]; then
    echo "Error: Could not locate model-A or model-B directories in $EVAL_DIR"
    exit 1
fi

# Create a clean reference baseline for diff calculations
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

python3 -m venv "$TEMP_DIR/clean_venv" >/dev/null 2>&1
"$TEMP_DIR/clean_venv/bin/pip" install -q anyio==4.14.2 >/dev/null 2>&1
CLEAN_ASYNCIO=$(find "$TEMP_DIR/clean_venv" -name "_asyncio.py" -path "*/anyio/_backends/*" | head -n 1)

evaluate_model() {
    local model_name="$1"
    local model_dir="$2"

    local py_bin="$model_dir/.venv/bin/python"
    local repro_script="$model_dir/minimal_repro.py"
    local model_asyncio=$(find "$model_dir/.venv" -name "_asyncio.py" -path "*/anyio/_backends/*" 2>/dev/null | head -n 1)

    # 1. Run reproducer test
    local repro_status="FAIL"
    if [[ -x "$py_bin" && -f "$repro_script" ]]; then
        if timeout 15 "$py_bin" "$repro_script" >/dev/null 2>&1; then
            repro_status="PASS"
        fi
    else
        repro_status="MISSING"
    fi

    # 2. Calculate code diff
    local added=0
    local removed=0
    local total=0

    if [[ -n "$model_asyncio" && -f "$model_asyncio" && -f "$CLEAN_ASYNCIO" ]]; then
        added=$(diff -u "$CLEAN_ASYNCIO" "$model_asyncio" | grep -c "^+[^+]" || true)
        removed=$(diff -u "$CLEAN_ASYNCIO" "$model_asyncio" | grep -c "^-[^-]" || true)
        total=$((added + removed))
    fi

    echo "$model_name|$repro_status|+$added / -$removed ($total total)"
}

res_a=$(evaluate_model "Model A" "$MODEL_A_DIR")
res_b=$(evaluate_model "Model B" "$MODEL_B_DIR")

IFS='|' read -r name_a status_a lines_a <<< "$res_a"
IFS='|' read -r name_b status_b lines_b <<< "$res_b"

echo ""
echo "========================================================================"
echo "                     EVALUATION COMPARISON SUMMARY                      "
echo "========================================================================"
printf "%-12s | %-18s | %-30s\n" "Model" "Reproducer Status" "Lines Changed (Diff)"
echo "-------------+--------------------+-------------------------------------"
printf "%-12s | %-18s | %-30s\n" "$name_a" "$status_a" "$lines_a"
printf "%-12s | %-18s | %-30s\n" "$name_b" "$status_b" "$lines_b"
echo "========================================================================"
echo ""