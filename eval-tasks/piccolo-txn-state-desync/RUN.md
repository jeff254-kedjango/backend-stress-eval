# RUN.md — how to run the piccolo txn-state-desync (graduated) A/B eval

> **Answer-revealing file.** This names the solution sentinels
> (`add_rollback_callback`, `_rollback_callbacks`). It must NEVER end up in a
> model's working dir. `make-eval-dirs.sh` copies only the prompt and its
> leak-check rejects `RUN.md` explicitly.

This is the graduated multi-gate successor to `piccolo-txn-data-loss`. It ships
the SAME root cause across three mutation paths, so the exact insert-only fix
both models produced on the previous task scores **VERDICT=FAIL** here. **Do not
reuse the old piccolo template verbatim** — paths, working-dir names, sentinels,
archive names, and grader are all different (see the table at the bottom).

**Prompt-only (Rule 10):** the model gets ONLY `initial-prompt.md`. No
reproducer is shipped — it would pre-localize the bug. The bug-is-live check is
an inline probe inside `make-eval-dirs.sh`; there is nothing to run by hand.
**Time-to-fix and turn count are graded metrics** — record them in `results.md`.

**Do NOT use `_exists_in_db` as a sentinel** — it is stock piccolo (8× in every
model `.venv`) and would false-positive. The two sentinels above are OUR fix
code and exist nowhere in stock piccolo.

---

## Phase 1 — Build both working folders

Use `make-eval-dirs.sh` — it copies ONLY the prompt, builds the venv, scrubs the
pyvenv.cfg provenance line (so no `backend-stress-eval` path leaks), and runs an
inline probe asserting all three bugs are live (`ins:0 rem:2 sp:0`).

```bash
cd ~/backend-stress-eval/eval-tasks/piccolo-txn-state-desync
./make-eval-dirs.sh A B     # builds ~/piccolo-desync-eval-task-A and -B
```

Only 1 file is copied into each dir — `initial-prompt.md`. NOT `minimal_repro.py`
(none exists), `grading-criteria.md`, `README.md`, or this `RUN.md`.

```bash
# Verify contents — expect exactly: .venv  initial-prompt.md   (NO repro)
ls -A ~/piccolo-desync-eval-task-A ~/piccolo-desync-eval-task-B
```

> **Build venvs with a system `python3.12`, not the harness `.venv`** if you can;
> the script auto-scrubs the harness path from `pyvenv.cfg` either way, but
> `deactivate` first if the harness venv is active.

## Phase 2 — Hide solution sources

```bash
mv ~/backend-stress-eval ~/backend-stress-eval.HIDDEN
mv ~/.claude ~/.claude.HIDDEN
mv ~/.cache ~/.cache.HIDDEN 2>/dev/null || true
mv ~/eval-outputs ~/eval-outputs.HIDDEN 2>/dev/null || true   # prior outputs contain fix code
mv ~/.vscode-server ~/.vscode-server.HIDDEN 2>/dev/null || true
# investigations/ notes name the fix too — they live inside backend-stress-eval,
# so the first mv already hides them.
```

## Phase 3 — Paranoia gate (every line must print 0)

```bash
grep -rlF "piccolo-txn"           ~ 2>/dev/null | grep -v "\.HIDDEN" | wc -l
grep -rlF "backend-stress-eval"   ~ 2>/dev/null | grep -v "\.HIDDEN" | wc -l
grep -rlF "add_rollback_callback" ~ 2>/dev/null | grep -v "\.HIDDEN" | wc -l
grep -rlF "_rollback_callbacks"   ~ 2>/dev/null | grep -v "\.HIDDEN" | wc -l
```

The last two are this task's solution sentinels — OUR fix code, nowhere in stock
piccolo. If any line is nonzero, re-run it WITHOUT `| wc -l` to see the file
path, then hide/remove it before starting `claude`.

## Phase 4 — Run each model

```bash
cd ~/piccolo-desync-eval-task-A
source .venv/bin/activate
# Start a timer — time-to-fix is a graded metric (Rule 10). Note wall-clock + turns.
claude        # feed it initial-prompt.md, let it run to completion
deactivate
mkdir -p ~/eval-outputs
cp -r ~/piccolo-desync-eval-task-A ~/eval-outputs/piccolo-desync-model-A-2026-08-03

cd ~/piccolo-desync-eval-task-B
source .venv/bin/activate
claude
deactivate
cp -r ~/piccolo-desync-eval-task-B ~/eval-outputs/piccolo-desync-model-B-2026-08-03
```

Archive names `piccolo-desync-model-A/B-*` match the grader's `find` pattern
(`*piccolo*desync*model-A*`) so it picks them up automatically.

## Phase 5 — Restore your environment

```bash
rm -rf ~/.claude && mv ~/.claude.HIDDEN ~/.claude
mv ~/backend-stress-eval.HIDDEN ~/backend-stress-eval
mv ~/.cache.HIDDEN ~/.cache 2>/dev/null || true
# restore prior outputs to a DIFFERENT name so they don't clobber the new run:
mv ~/eval-outputs.HIDDEN ~/eval-outputs.pre-desync 2>/dev/null || true
mv ~/.vscode-server.HIDDEN ~/.vscode-server 2>/dev/null || true
```

## Phase 6 — Grade + verify

```bash
cd ~/backend-stress-eval
source .venv/bin/activate
bash scripts/grade_piccolo_desync_evals.sh    # auto-finds the piccolo-desync-model-A/B dirs
pytest                                          # harness still green
```

**Grade meaning:** each model PASSes only if ALL seven gates pass — the three
graduated desync gates (`INSERT_LOSS`, `REMOVE_DUP`, `SAVEPOINT_LOSS`) plus
`COMMIT`, `PLAIN`, `UPDATE`, `ROLLBACK`. The insert-only fix (what both models
shipped on the previous task) scores `INSERT_LOSS=PASS REMOVE_DUP=FAIL
SAVEPOINT_LOSS=FAIL → VERDICT=FAIL`. Only a general fix covering all three
mutation paths passes. Then hand-record time-to-fix + turns per model in
`results.md`, and review by hand against `grading-criteria.md`.

---

## Key differences from the old `piccolo-txn-data-loss` template

| | old piccolo template | **piccolo-desync (this run)** |
|---|---|---|
| Source path | `piccolo-txn-data-loss/` | `piccolo-txn-state-desync/` |
| Working dirs | `~/piccolo-eval-task-A/B` | `~/piccolo-desync-eval-task-A/B` |
| Files shipped | prompt **+ `minimal_repro.py`** | **prompt ONLY** (no repro — Rule 10) |
| Bug-live check | repro → `orders stored: 0` | inline probe at build → `ins:0 rem:2 sp:0` |
| Sentinels | `register_inserted`, `_inserted_objects` | **`add_rollback_callback`, `_rollback_callbacks`** |
| Archive name | `piccolo-model-A/B-*` | **`piccolo-desync-model-A/B-*`** |
| Grader | `grade_piccolo_evals.sh` | **`grade_piccolo_desync_evals.sh`** |
| Restore-to name | `.anyio-previous` | **`.pre-desync`** |
