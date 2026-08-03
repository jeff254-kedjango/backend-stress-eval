#!/usr/bin/env bash
# Grade the piccolo transaction-state-desync eval (graduated, multi-gate).
#
# One root: a transaction / savepoint reverts the DB but never the in-memory
# instance state (the Transaction holds no object back-references). It surfaces
# as THREE silent symptoms. A fix that patches only the symptom the model
# localized first passes one gate and fails the others; only the general fix
# (revert all provisional instance mutations on rollback AND savepoint) passes
# all three.
#
# Objective gates (a full fix passes ALL; a partial/masking fix fails one):
#   INSERT_LOSS   — save() in a rolled-back txn, then re-save → exactly 1 row.
#                   (Stock: 0 rows, silently lost. The original bug direction.)
#   REMOVE_DUP    — remove() in a rolled-back txn, then re-save → exactly 1 row.
#                   (Stock: 2 rows, silent DUPLICATE. Missed by an insert-only fix.)
#   SAVEPOINT_LOSS— save() after rollback_to(savepoint), then re-save → row present.
#                   (Stock: lost. Missed by any full-rollback-only fix.)
#   COMMIT        — save+commit inside a txn still persists.
#   PLAIN         — save outside any txn still persists.
#   UPDATE        — an existing row updated in place stays 1 row (no dup).
#   ROLLBACK      — a rolled-back txn still discards its row.
# Plus a diff vs a clean piccolo 1.36.0 baseline (fix size/location signal).
set -euo pipefail

EVAL_DIR="${1:-$HOME/eval-outputs}"

find_dir() { find "$EVAL_DIR" -maxdepth 1 -type d -name "$1" 2>/dev/null | sort -r | head -n1; }
MODEL_A_DIR=$(find_dir "*piccolo*desync*model-A*"); [[ -z "$MODEL_A_DIR" ]] && MODEL_A_DIR=$(find_dir "*piccolo*model-A*")
MODEL_B_DIR=$(find_dir "*piccolo*desync*model-B*"); [[ -z "$MODEL_B_DIR" ]] && MODEL_B_DIR=$(find_dir "*piccolo*model-B*")
[[ -z "$MODEL_A_DIR" ]] && MODEL_A_DIR="$HOME/piccolo-desync-eval-task-A"
[[ -z "$MODEL_B_DIR" ]] && MODEL_B_DIR="$HOME/piccolo-desync-eval-task-B"

if [[ ! -d "$MODEL_A_DIR" || ! -d "$MODEL_B_DIR" ]]; then
    echo "Error: could not locate model A/B dirs (looked in $EVAL_DIR and ~/piccolo-desync-eval-task-*)"
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

    # INSERT_LOSS: save in a rolled-back txn, then re-save → 1 row.
    r = Rec(ref="ins")
    try:
        async with DB.transaction():
            await r.save(); raise RuntimeError("x")
    except RuntimeError: pass
    await r.save()
    res["INSERT_LOSS"] = (await Rec.count().where(Rec.ref == "ins")) == 1

    # REMOVE_DUP: remove in a rolled-back txn, then re-save → 1 row (not 2).
    d = Rec(ref="rem"); await d.save()
    try:
        async with DB.transaction():
            await d.remove(); raise RuntimeError("x")
    except RuntimeError: pass
    await d.save()
    res["REMOVE_DUP"] = (await Rec.count().where(Rec.ref == "rem")) == 1

    # SAVEPOINT_LOSS: save after rollback_to(savepoint), then re-save → present.
    async with DB.transaction() as txn:
        keep = Rec(ref="sp_keep"); await keep.save()
        sp = await txn.savepoint()
        b = Rec(ref="sp_b"); await b.save()
        await sp.rollback_to()
    await b.save()
    res["SAVEPOINT_LOSS"] = (await Rec.count().where(Rec.ref == "sp_b")) == 1

    # COMMIT: save+commit inside a txn persists.
    async with DB.transaction():
        await Rec(ref="commit").save()
    res["COMMIT"] = (await Rec.count().where(Rec.ref == "commit")) == 1

    # PLAIN: save outside any txn persists.
    await Rec(ref="plain").save()
    res["PLAIN"] = (await Rec.count().where(Rec.ref == "plain")) == 1

    # UPDATE: update an existing row in place — stays 1, not duplicated.
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
    print(" ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in res.items()))
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
        result=$(timeout 120 "$py" -c "$PROBE" 2>/dev/null | tr '\n' ' ' || echo "CRASH")
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
echo "        PICCOLO TXN-STATE-DESYNC (graduated) — EVALUATION COMPARISON SUMMARY          "
echo "======================================================================================"
for r in "$res_a" "$res_b"; do
    IFS='|' read -r n status diff <<< "$r"
    printf "%-9s | %s\n" "$n" "$status"
    printf "%-9s   (%s)\n" "" "$diff"
done
echo "======================================================================================"
echo "PASS requires ALL gates. The three desync gates are graduated:"
echo "  INSERT_LOSS  — the base bug (save in rolled-back txn → loss)."
echo "  REMOVE_DUP   — missed by an insert-only fix (remove in rolled-back txn → duplicate)."
echo "  SAVEPOINT_LOSS — missed by any full-rollback-only fix (rollback_to a savepoint → loss)."
echo "A model that patches one symptom passes one gate; the general fix passes all three."
echo "Note: only table.py diff shown — a full fix also touches engine transaction + insert;"
echo "review by hand against grading-criteria.md."