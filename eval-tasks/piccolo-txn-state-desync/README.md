# piccolo transaction state-desync (graduated) — eval task

**Framework:** [piccolo](https://github.com/piccolo-orm/piccolo) 1.36.0 (stock, current stable)
**Language:** Python 3.12
**Task type:** find-and-fix (silent data-corruption interaction bug family)
**Estimated model time:** 1–2 h
**Discovered by:** the "piccolo adjacent paths" hunt off the shipped
`piccolo-txn-data-loss` task — see
`investigations/piccolo-txn-identity/findings-remove.md`. piccolo = 1,934★.

## The bug family in one paragraph

Piccolo tracks whether an object is persisted with an in-memory flag
(`_exists_in_db`) plus its primary key. Several operations mutate that state
provisionally, and the transaction/savepoint machinery reverts the **database**
on rollback but never the **in-memory object** — the `Transaction` holds no
references to the objects it touched. Three flows expose this:

| # | Trigger | Symptom | Mutation site |
|---|---|---|---|
| 0 | save() in a rolled-back txn | silent **LOSS** (retry UPDATEs a phantom id) | insert callback sets `True` |
| 1 | remove() in a rolled-back txn | silent **DUPLICATE** (retry INSERTs again) | `table.py::remove()` sets `False`, nulls pk |
| 2 | save() after `rollback_to(savepoint)` | silent **LOSS** (partial rollback) | insert callback + `Savepoint.rollback_to()` |

Each individual operation is correct; only the combination corrupts data. No
exception, tests green, the object reports a normal id.

## Why this task (vs. the shipped piccolo task)

The shipped `piccolo-txn-data-loss` task shipped bug #0 only and **did not
differentiate** — both frontier models produced the identical insert-only
rollback fix and both passed. This task graduates the **same root** across all
three paths. The measured trap: the exact insert-only fix both models shipped
passes only gate #0 and **fails #1 and #2** (VERDICT=FAIL). A model that patches
one symptom passes one gate; only the general fix (revert all provisional
instance mutations on rollback AND on savepoint rollback) passes all three. See
the discrimination table in `results.md`.

## The task, in golden-standard format

The model receives exactly **one** file: `initial-prompt.md`. No reproducer is
shipped (Rule 10) — it would pre-localize the bug and collapse differentiation.

- `initial-prompt.md` — **the only file the model gets.** Symptom-only,
  behavioral: describes the three observed flows ("an insert we rolled back", "a
  delete we rolled back", "undoing part of a transaction to an earlier
  checkpoint") without naming `_exists_in_db`, insert/update/delete branches,
  phantom ids, or savepoint APIs. Audited against a giveaway list.
- `grading-criteria.md` — harness-side only, never shipped. Prose outcome axes.
- `results.md` — per-task run log (timing, turns, verdict) + discrimination table.
- Bug-is-live check is an **inline probe** in `make-eval-dirs.sh` and the
  grader's own independent probe — never a file in the model's dir.

## Running it (clean-room isolation)

`make-eval-dirs.sh` builds an isolated working dir per model with only the
prompt + a piccolo 1.36.0 venv. It confirms all three bugs are live via an
inline probe (`ins:0 rem:2 sp:0`) and refuses to build if the prompt contains a
cause-revealing giveaway:

```bash
./make-eval-dirs.sh A B        # builds ~/piccolo-desync-eval-task-A and -B
```

Then hide this harness repo, run the model inside each dir, and grade with
`scripts/grade_piccolo_desync_evals.sh` + review by hand against
`grading-criteria.md`.

**Grade meaning:** a model PASSes only if ALL gates pass — the three graduated
desync gates (INSERT_LOSS, REMOVE_DUP, SAVEPOINT_LOSS) plus COMMIT, PLAIN,
UPDATE, ROLLBACK. A partial or symptom-masking fix fails a gate.
