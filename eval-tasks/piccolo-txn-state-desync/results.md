# piccolo txn-state-desync (graduated) — run results

Per-task run log. **Time-to-fix and turns are first-class metrics** (Rule 10).

## Protocol note

- **prompt-only** (standard): the model gets ONLY `initial-prompt.md`. No repro.
- **assisted** (deprecated): never use for this task.

Fill `time` and `turns` in by hand from each run.

## Runs

| date | model | protocol | time (min) | turns | verdict (INSERT_LOSS/REMOVE_DUP/SAVEPOINT_LOSS/COMMIT/PLAIN/UPDATE/ROLLBACK) | notes |
|------|-------|----------|-----------|-------|-------------------------------------------------------------------------------|-------|
| — | A | prompt-only | — | — | — | pending run |
| — | B | prompt-only | — | — | — | pending run |

## Why this task should differentiate (unlike the shipped piccolo task)

The shipped `piccolo-txn-data-loss` task did NOT differentiate — both frontier
models produced the same insert-only rollback fix and both PASSed. This task
graduates the SAME root cause across three mutation paths, and the grader was
validated to discriminate every partial-fix state:

| fix state | INSERT_LOSS | REMOVE_DUP | SAVEPOINT_LOSS | VERDICT |
|---|---|---|---|---|
| stock (unpatched) | FAIL | FAIL | FAIL | FAIL |
| remove-only fix | FAIL | PASS | FAIL | FAIL |
| **insert-only fix (what BOTH models shipped last time)** | **PASS** | **FAIL** | **FAIL** | **FAIL** |
| general fix (all paths + savepoint) | PASS | PASS | PASS | PASS |

So the exact solution both models converged on for the previous task now scores
VERDICT=FAIL here. To pass, a model must generalize the fix to cover:
1. insert-in-rolled-back-txn (the base bug),
2. remove-in-rolled-back-txn (silent duplicate — different mutation site,
   `table.py::remove()`, invisible to an insert-side callback),
3. save-after-`rollback_to(savepoint)` (silent loss — partial rollback via
   `Savepoint.rollback_to()`, invisible to a full-`rollback()`-only fix).

Normal-path gates (COMMIT/PLAIN/UPDATE/ROLLBACK) stay green across all states,
so the desync gates are the discriminator, not noise.

## Known harness notes

- Grader `scripts/grade_piccolo_desync_evals.sh` uses its own independent probe;
  never reads a model-side repro.
- `make-eval-dirs.sh` confirms all three bugs live via an inline probe
  (`ins:0 rem:2 sp:0`).
- Investigation + fix spike + novelty check:
  `investigations/piccolo-txn-identity/findings-remove.md`.
