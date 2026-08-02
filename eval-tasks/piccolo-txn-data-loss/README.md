# piccolo transaction-rollback data-loss — eval task

**Framework:** [piccolo](https://github.com/piccolo-orm/piccolo) 1.36.0 (stock, current stable)
**Language:** Python 3.12
**Task type:** find-and-fix (silent data-loss interaction bug)
**Estimated model time:** 1–2 h
**Discovered by:** the ascending-maturity sweep, L3-interaction pivot — see
`investigations/piccolo-txn-identity/findings.md`. piccolo = 1,934 GitHub stars
(above the reviewer's ≥1000★ floor).

## The bug in one paragraph

`obj.save()` inside a transaction runs an INSERT and sets the instance flag
`obj._exists_in_db = True`. If that transaction rolls back, the DB row is gone
but `_exists_in_db` stays `True`. A later `obj.save()` then branches on that
stale flag, takes the UPDATE path, and issues `UPDATE ... WHERE pk = <phantom
id>` — which matches zero rows. The record is **silently lost**: no exception,
the object still reports a valid id, tests pass. Each operation (rollback,
save) is individually correct; only the combination loses data.

Root cause: the transaction never reverts `_exists_in_db` on rollback, and the
transaction object holds no references to the objects it touched — so the fix
must introduce an object↔transaction back-reference that does not exist today
(spans `engine/*` transaction + the insert path; the class uses `__slots__`,
which a naive attempt trips on). **Not a one-liner** — see the fix spike in
the findings. Novelty checked: 0 relevant upstream issues/PRs.

## Why this task (vs. the anyio / aiocache leaks)

The anyio and aiocache leaks were real but had one-line fixes, so both frontier
models fixed them fully — no differentiation. This is an L3 interaction bug:
silent (no crash, tests green), symptom (data vanished) far from cause (stale
persistence flag), and — confirmed by a fix spike — the correct fix is
architectural, not a one-liner. That gives a weaker model believable ways to
get it wrong (reset state in the wrong place, trip `__slots__`, or mask the
symptom in caller code) while a stronger model designs the back-reference. The
A/B run decides whether it actually differentiates.

## The task, in golden-standard format

- `initial-prompt.md` — symptom-only. Describes only "records disappear after a
  rolled-back transaction, retry reports success, tests pass." Names NOTHING
  about the cause: no `_exists_in_db`, no insert-vs-update, no phantom id, no
  flag, no rollback-reset. Audited against a giveaway list. The model must
  localize the loss itself.
- `grading-criteria.md` — prose outcome expectations: the record is reliably
  stored, normal persistence unchanged (no duplicates, updates still update),
  fix at the source (not masked in caller code), no new failure, stock current
  stable.
- `minimal_repro.py` — standalone reproducer, one dependency (`piccolo[sqlite]`,
  self-contained on-disk SQLite, no server). Reads like ordinary app code;
  prints the stored-record count (0, expected 1).

## Running it (clean-room isolation)

`make-eval-dirs.sh` builds an isolated working dir per model with only the
prompt + reproducer + a piccolo 1.36.0 venv to patch, and refuses to build if
the prompt contains any cause-revealing giveaway:

```bash
./make-eval-dirs.sh A B        # builds ~/piccolo-eval-task-A and -B
```

Then hide this harness repo, run the model inside each dir, and grade with
`scripts/grade_piccolo_evals.sh` + review by hand against `grading-criteria.md`.
