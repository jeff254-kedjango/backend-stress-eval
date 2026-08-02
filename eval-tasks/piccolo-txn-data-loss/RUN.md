# RUN.md — how to run the piccolo txn-rollback data-loss A/B eval

> **Answer-revealing file.** This names the solution sentinels
> (`register_inserted`, `_inserted_objects`). It must NEVER end up in a model's
> working dir. `make-eval-dirs.sh` copies only the prompt + reproducer and its
> leak-check rejects `RUN.md` explicitly.

This is the piccolo analogue of the anyio run. **Do not reuse the anyio
template verbatim** — the paths, package, and (critically) the paranoia
sentinels are all different. The anyio sentinels (`_drop_loop_run_vars`,
`_run_vars.pop`) are anyio-fix code and would falsely print 0 for a piccolo run.

## What's different from the anyio template

| | anyio | **piccolo (this run)** |
|---|---|---|
| Source dir | `anyio-lifecycle-leak-v2/` | `eval-tasks/piccolo-txn-data-loss/` |
| Package | `anyio==4.14.2` | `piccolo[sqlite]==1.36.0` |
| Sentinels | `_drop_loop_run_vars`, `_run_vars.pop` | **`register_inserted`, `_inserted_objects`** |
| Bug-live check | — | `minimal_repro.py` prints `orders stored: 0` |
| Grader | `scripts/grade_evals.sh` | `scripts/grade_piccolo_evals.sh` |

**Do NOT use `_exists_in_db` as a sentinel** — it is stock piccolo (appears 7×
in every model `.venv`) and would false-positive. The two sentinels above are
OUR fix code and exist nowhere in stock piccolo.

---

## Phase 1 — Build both working folders

```bash
# Model A
mkdir -p ~/piccolo-eval-task-A
cp ~/backend-stress-eval/eval-tasks/piccolo-txn-data-loss/initial-prompt.md ~/piccolo-eval-task-A/
cp ~/backend-stress-eval/eval-tasks/piccolo-txn-data-loss/minimal_repro.py  ~/piccolo-eval-task-A/
python3.12 -m venv ~/piccolo-eval-task-A/.venv
~/piccolo-eval-task-A/.venv/bin/pip install -q "piccolo[sqlite]==1.36.0"

# Model B (identical)
mkdir -p ~/piccolo-eval-task-B
cp ~/backend-stress-eval/eval-tasks/piccolo-txn-data-loss/initial-prompt.md ~/piccolo-eval-task-B/
cp ~/backend-stress-eval/eval-tasks/piccolo-txn-data-loss/minimal_repro.py  ~/piccolo-eval-task-B/
python3.12 -m venv ~/piccolo-eval-task-B/.venv
~/piccolo-eval-task-B/.venv/bin/pip install -q "piccolo[sqlite]==1.36.0"
```

Only 2 files copied — prompt + reproducer. NOT `grading-criteria.md`,
`README.md`, or this `RUN.md`.

> **Build venvs with a system `python3.12`, not the harness `.venv`.** If the
> active shell has the harness venv sourced, `deactivate` first — otherwise
> `pyvenv.cfg` records a `backend-stress-eval` path and trips the gate. If
> `/usr/bin/python3.12` is missing, plain `python3.12` / `python3` is fine, as
> long as it is not the harness venv's python.

```bash
# Verify contents — expect exactly: .venv  initial-prompt.md  minimal_repro.py
ls -A ~/piccolo-eval-task-A ~/piccolo-eval-task-B

# Confirm the bug is live in each (expect "orders stored: 0" both times)
~/piccolo-eval-task-A/.venv/bin/python ~/piccolo-eval-task-A/minimal_repro.py
~/piccolo-eval-task-B/.venv/bin/python ~/piccolo-eval-task-B/minimal_repro.py
```

## Phase 2 — Hide solution sources

```bash
mv ~/backend-stress-eval ~/backend-stress-eval.HIDDEN
mv ~/.claude ~/.claude.HIDDEN
mv ~/.cache ~/.cache.HIDDEN 2>/dev/null || true
mv ~/eval-outputs ~/eval-outputs.HIDDEN 2>/dev/null || true   # prior outputs contain fix code
mv ~/.vscode-server ~/.vscode-server.HIDDEN 2>/dev/null || true
```

## Phase 3 — Paranoia gate (every line must print 0)

```bash
grep -rlF "piccolo-txn"         ~ 2>/dev/null | grep -v "\.HIDDEN" | wc -l
grep -rlF "backend-stress-eval" ~ 2>/dev/null | grep -v "\.HIDDEN" | wc -l
grep -rlF "register_inserted"   ~ 2>/dev/null | grep -v "\.HIDDEN" | wc -l
grep -rlF "_inserted_objects"   ~ 2>/dev/null | grep -v "\.HIDDEN" | wc -l
```

If any line is nonzero, re-run it WITHOUT `| wc -l` to see the file path, then
hide/remove it before starting `claude`.

## Phase 4 — Run each model

```bash
cd ~/piccolo-eval-task-A
source .venv/bin/activate
claude        # feed it initial-prompt.md, let it run to completion
deactivate
mkdir -p ~/eval-outputs
cp -r ~/piccolo-eval-task-A ~/eval-outputs/piccolo-model-A-2026-08-03

cd ~/piccolo-eval-task-B
source .venv/bin/activate
claude
deactivate
cp -r ~/piccolo-eval-task-B ~/eval-outputs/piccolo-model-B-2026-08-03
```

Archive names `piccolo-model-A/B-*` match the grader's `find` pattern
(`*piccolo*model-A*`) so it picks them up automatically.

## Phase 5 — Restore your environment

```bash
rm -rf ~/.claude && mv ~/.claude.HIDDEN ~/.claude
mv ~/backend-stress-eval.HIDDEN ~/backend-stress-eval
mv ~/.cache.HIDDEN ~/.cache 2>/dev/null || true
# restore prior outputs to a DIFFERENT name so they don't clobber the new run:
mv ~/eval-outputs.HIDDEN ~/eval-outputs.anyio-previous 2>/dev/null || true
mv ~/.vscode-server.HIDDEN ~/.vscode-server 2>/dev/null || true
```

## Phase 6 — Grade + verify

```bash
cd ~/backend-stress-eval
source .venv/bin/activate
bash scripts/grade_piccolo_evals.sh    # auto-finds the piccolo-model-A/B dirs
pytest                                  # harness still green
```

**Grade meaning:** each model PASSes only if ALL five gates pass —
`RETRY` (the bug fixed), `COMMIT`, `PLAIN`, `UPDATE` (no always-insert dupes),
`ROLLBACK` (rollback still discards). A symptom-masking or always-insert "fix"
fails a gate (or crashes on a UNIQUE-constraint re-insert). See
`grading-criteria.md` for the prose axes to review by hand.
