# piccolo txn-rollback + remove() → silent DUPLICATE — findings (2026-08-03)

**Target:** piccolo 1.36.0 (stock, current stable), SQLite engine, Python 3.12.
**Sweep context:** "piccolo adjacent paths" hunt — same instance-state-vs-DB
lifecycle class as the shipped `piccolo-txn-data-loss` task, harder variant.
Stop-at-first-bug: found.

## The bug (teeth-verified)

`Table.remove()` (`table.py:606`) mutates instance state **synchronously when
building the delete query** — it sets `self.<pk> = None` and
`self._exists_in_db = False` *before* the DELETE runs, and this mutation is
never reverted if the surrounding transaction rolls back.

```
1. o.save() (committed)            -> row in DB, exists=True, id=1     [correct]
2. async with DB.transaction():
       await o.remove()            -> DELETE runs; exists=False, id=None  [correct in-txn]
   ... rollback ...                -> DB row is RESTORED (rows_in_db=1)  [correct]
3. o still has exists=False, id=None  <- THE BUG: instance state not reverted
4. o.save() again (retry)          -> branches on `not _exists_in_db` = True
                                    -> takes INSERT path -> DUPLICATE row     [DATA CORRUPTION]
```

Reproduced directly:
```
after save:            exists=True  id=1    rows=1
inside txn post-remove:exists=False id=None rows=0
after rollback:        exists=False id=None rows_in_db=1   <- object desynced from DB
after re-save:         rows_in_db=2  refs=[ORD-1, ORD-1]   <- SILENT DUPLICATE
```

Teeth PASS (decisive): on the COMMIT path `remove()` → rows=0, re-save → exactly
1 row (no dup); plain no-txn `remove()` → row gone and stays gone. Only the
ROLLBACK path produces the duplicate. Trustworthy positive, not a false alarm.

## Why this is a BETTER eval bug than the shipped one (differentiation)

The shipped `piccolo-txn-data-loss` task did NOT differentiate on grade in the
A/B — both models produced the same correct fix (hook `rollback()` to revert
`_exists_in_db` on **inserted** objects, via an insert-side callback). This
`remove()` variant is a **trap for exactly that fix**:

- It flips the flag the OTHER direction (`True → False`) and nulls the PK, and
  does it **synchronously in `table.py::remove()`**, NOT via the insert path's
  `_raw_response_callback`. So a fix that only registers *inserted* objects with
  the transaction does nothing here — the duplicate still happens.
- Symptom is a silent **DUPLICATE**, the opposite of the shipped bug's silent
  **LOSS**. A grader/rubric written for "row not lost" misses it entirely — it
  needs its own gate (`REMOVE_ROLLBACK`: remove-in-rolled-back-txn then re-save
  ⇒ exactly 1 row, not 2).
- The correct fix must revert instance-state mutations in **both directions**
  on rollback (insert set True; remove set False + nulled PK) — a model that
  patches only the observed symptom fixes one direction and fails the other.
  This is the "fix the class, not the symptom" separation the shipped task
  lacked.

## Root cause location

- `table.py:606 remove()` — sets `pk=None` and `_exists_in_db=False` eagerly,
  no coupling to transaction outcome.
- Same architectural gap as the shipped bug: the `Transaction` object holds no
  references to objects whose in-memory state it mutated, so it cannot revert
  them on rollback. The fix spans engine transaction + the state-mutation sites.

## Adjacent paths noted (not yet probed) — possible even subtler variants

- **Savepoints / `rollback_to`** — partial rollback to a savepoint would need to
  revert only objects mutated *after* the savepoint; a full-`rollback()`-only
  fix misses this.
- `objects.py:282` referenced-object `_exists_in_db=True` (get_or_create /
  nested insert); `refresh.py:163`; m2m `add`/`remove`.

## Novelty check (2026-08-03) — CONFIRMED NOVEL

- GitHub issue/PR search (multiple phrasings) + direct issues-page fetch:
  **0 relevant hits**. The `remove()`-sets-`_exists_in_db=False` behavior is a
  documented *feature* in the changelog, but nobody has reported that it isn't
  reverted on rollback. Docs describe savepoints/`rollback_to` but never the
  stale-instance interaction. Not documented behavior; a silent duplicate/loss
  is a defect, not a contract.

## Fix spike (2026-08-03) — CONFIRMS non-trivial + the trap

Spiked the correct `remove()`-rollback fix on a clean copy. Works:
`after rollback: exists=True id=1`, re-save → `rows=1` (no dup); commit/plain
teeth still green. It required **3 sites / 2 files**: (1) `engine/base.py`
`BaseTransaction.__slots__` + a rollback-callback registry (hits the SAME
`__slots__` constraint as the shipped bug), (2) `engine/sqlite.py` run
callbacks in `rollback()`, (3) `table.py::remove()` register a revert with the
active transaction. Subtlety unique to this path: **`remove()` is synchronous**
— it nulls the PK and returns a `Delete` query BEFORE the DELETE awaits, so the
revert must capture the old pk/flag at `remove()` time.

**The trap is REAL (measured):** Model A's shipped insert-rollback fix — the
exact fix BOTH frontier models produced for the original task — leaves this bug
**completely unfixed** (`rows_in_db=2`). A's fix only registers *inserted*
objects via `insert.py::_raw_response_callback`; `remove()` mutates via a
different path it never sees. Conversely my remove-spike leaves the original
insert-loss bug broken (`rows=0`). The two paths are orthogonal; the GENERAL
fix (transaction reverts all provisional instance mutations on rollback) covers
both — so a grader checking both directions rewards the model that generalizes.

## Savepoint variant (2026-08-03) — a THIRD, subtler sibling: silent LOSS

`rollback_to(savepoint)` is a *partial* rollback via `Savepoint.rollback_to()`,
NOT the full `rollback()`. An object inserted after a savepoint, then discarded
by `rollback_to`, keeps `_exists_in_db=True` + phantom id → later `save()`
UPDATEs a phantom row → **silent LOSS** (expected A+B=2 rows, got 1: A only).

Measured: survives Model A's shipped fix AND my remove-spike (both hook full
`rollback()` only) → `rows=1`, B lost. Teeth green: without the savepoint
rollback, B commits and re-save is a no-op update → `rows=2`, no loss. Real,
not a false positive.

This is the subtlest of the three: same "loss" symptom as the original, but the
correct fix must ALSO hook `Savepoint.rollback_to()` and revert only objects
mutated *after* that savepoint — a strictly harder design than "revert on full
rollback."

## The bug family (all teeth-verified, same root, ascending subtlety)

| # | Trigger | Symptom | Root mutation site | Missed by shipped fix? |
|---|---|---|---|---|
| 0 | save() in rolled-back txn | silent LOSS | insert.py callback (True) | — (this IS the shipped bug) |
| 1 | **remove() in rolled-back txn** | silent **DUPLICATE** | table.py::remove() (False+pk=None) | **YES** |
| 2 | **save() after savepoint rollback_to** | silent LOSS | insert callback, partial rollback | **YES** (both #0 and #1 fixes) |

Root (all three): the transaction/savepoint reverts the DB but never the
in-memory instance state; the `Transaction` holds no object back-references.

## Eval task BUILT + grader validated (2026-08-03)

Shipped `eval-tasks/piccolo-txn-state-desync/` as a **graduated multi-gate**
task (user's chosen scope) + `scripts/grade_piccolo_desync_evals.sh`.

- **Prompt-only** (Rule 10): only `initial-prompt.md` shipped; behavioral
  symptom-only framing of all three flows (no savepoint API names, no
  `_exists_in_db`, audited — giveaway guard verified to fire on a leaky prompt).
- **Grader DISCRIMINATES across every partial-fix state** (measured):

  | fix state | INSERT_LOSS | REMOVE_DUP | SAVEPOINT_LOSS | VERDICT |
  |---|---|---|---|---|
  | stock | FAIL | FAIL | FAIL | FAIL |
  | remove-only | FAIL | PASS | FAIL | FAIL |
  | **insert-only (what BOTH models shipped last time)** | PASS | FAIL | FAIL | **FAIL** |
  | general fix (insert+remove+savepoint) | PASS | PASS | PASS | **PASS** |

  Normal gates (COMMIT/PLAIN/UPDATE/ROLLBACK) green throughout. The general fix
  reaching PASS confirms the task is solvable, not just a gauntlet.
- **Live-probe** (`make-eval-dirs.sh`, inline) asserts `ins:0 rem:2 sp:0`.

## Status: TASK BUILT, grader validated, ready for a real A/B (prompt-only).

The key property the shipped `piccolo-txn-data-loss` lacked: the exact fix both
frontier models converged on there scores VERDICT=FAIL here. Differentiation
ceiling is now measured, not estimated.
