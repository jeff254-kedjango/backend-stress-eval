#!/usr/bin/env bash
# Grade the piccolo transaction-rollback data-loss eval: model-A vs model-B.
#
# Objective gates (a fix must pass ALL; a symptom-mask fails one):
#   RETRY   — save-in-rolled-back-txn then re-save stores exactly 1 row (the bug).
#   COMMIT  — save+commit inside a txn still persists (fix didn't break commit).
#   PLAIN   — save outside any txn still persists (baseline path intact).
#   UPDATE  — an existing row updated in place stays 1 row, not duplicated
#             (rejects a "always INSERT" fix that turns updates into dupes).
#   ROLLBACK— a rolled-back txn still discards its row (fix didn't disable rollback).
# Plus a diff vs a clean piccolo 1.36.0 baseline (fix size/location signal).
set -euo pipefail

EVAL_DIR="${1:-$HOME/eval-outputs}"

find_dir() { find "$EVAL_DIR" -maxdepth 1 -type d -name "$1" 2>/dev/null | sort -r | head -n1; }
MODEL_A_DIR=$(find_dir "*piccolo*model-A*"); [[ -z "$MODEL_A_DIR" ]] && MODEL_A_DIR=$(find_dir "model-A*piccolo*")
MODEL_B_DIR=$(find_dir "*piccolo*model-B*"); [[ -z "$MODEL_B_DIR" ]] && MODEL_B_DIR=$(find_dir "model-B*piccolo*")
[[ -z "$MODEL_A_DIR" ]] && MODEL_A_DIR="$HOME/piccolo-eval-task-A"
[[ -z "$MODEL_B_DIR" ]] && MODEL_B_DIR="$HOME/piccolo-eval-task-B"

if [[ ! -d "$MODEL_A_DIR" || ! -d "$MODEL_B_DIR" ]]; then
    echo "Error: could not locate model A/B dirs (looked in $EVAL_DIR and ~/piccolo-eval-task-*)"
    exit 1
fi

TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT
python3 -m venv "$TEMP_DIR/clean" >/dev/null 2>&1
"$TEMP_DIR/clean/bin/pip" install -q "piccolo[sqlite]==1.36.0" >/dev/null 2>&1
CLEAN_TABLE=$(find "$TEMP_DIR/clean" -name "table.py" -path "*/piccolo/*" | head -n1)

read -r -d '' PROBE <<'PY' || true
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
    res = {}

    # RETRY: the bug — save in a rolled-back txn, then re-save.
    r = Rec(ref="retry")
    try:
        async with DB.transaction():
            await r.save(); raise RuntimeError("x")
    except RuntimeError: pass
    await r.save()
    res["RETRY"] = (await Rec.count().where(Rec.ref == "retry")) == 1

    # COMMIT: save+commit inside a txn persists.
    async with DB.transaction():
        await Rec(ref="commit").save()
    res["COMMIT"] = (await Rec.count().where(Rec.ref == "commit")) == 1

    # PLAIN: save outside any txn persists.
    await Rec(ref="plain").save()
    res["PLAIN"] = (await Rec.count().where(Rec.ref == "plain")) == 1

    # UPDATE: update an existing row in place — must stay 1, not duplicate.
    u = Rec(ref="upd"); await u.save()
    u.ref = "upd2"; await u.save()
    res["UPDATE"] = (await Rec.count().where((Rec.ref == "upd") | (Rec.ref == "upd2"))) == 1

    # ROLLBACK: a rolled-back txn still discards its row.
    try:
        async with DB.transaction():
            await Rec(ref="gone").save(); raise RuntimeError("x")
    except RuntimeError: pass
    res["ROLLBACK"] = (await Rec.count().where(Rec.ref == "gone")) == 0

    os.path.exists(p) and os.unlink(p)
    print(" ".join(f"{k}={'PASS' if v else 'FAIL'}" for k,v in res.items()))
    print("VERDICT=" + ("PASS" if all(res.values()) else "FAIL"))

asyncio.run(main())
PY

grade_one() {
    local name="$1" dir="$2"
    local py="$dir/.venv/bin/python"
    local tbl
    tbl=$(find "$dir/.venv" -name "table.py" -path "*/piccolo/*" 2>/dev/null | head -n1)

    local result="probe did not run"
    if [[ -x "$py" ]]; then
        result=$(timeout 90 "$py" -c "$PROBE" 2>/dev/null | tr '\n' ' ' || echo "CRASH")
    fi
    local added=0 removed=0
    if [[ -n "$tbl" && -f "$tbl" && -f "$CLEAN_TABLE" ]]; then
        added=$(diff -u "$CLEAN_TABLE" "$tbl" | grep -c "^+[^+]" || true)
        removed=$(diff -u "$CLEAN_TABLE" "$tbl" | grep -c "^-[^-]" || true)
    fi
    echo "$name|$result|table.py +$added/-$removed"
}

res_a=$(grade_one "Model A" "$MODEL_A_DIR")
res_b=$(grade_one "Model B" "$MODEL_B_DIR")

echo ""
echo "======================================================================================"
echo "          PICCOLO TXN-ROLLBACK DATA-LOSS — EVALUATION COMPARISON SUMMARY              "
echo "======================================================================================"
for r in "$res_a" "$res_b"; do
    IFS='|' read -r n status diff <<< "$r"
    printf "%-9s | %s\n" "$n" "$status"
    printf "%-9s   (%s)\n" "" "$diff"
done
echo "======================================================================================"
echo "PASS requires ALL five gates. RETRY=fixed-bug; UPDATE guards against a"
echo "\"always-insert\" cheat; ROLLBACK guards against disabling rollback; COMMIT/PLAIN"
echo "guard the normal paths. Note: only table.py diff is shown — a full fix also touches"
echo "the engine transaction class; review both by hand against grading-criteria.md."
