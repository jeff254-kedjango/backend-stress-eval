# piccolo transaction-rollback + object-identity — findings (2026-08-03)

**Target:** piccolo 1.36.0 (stock, current stable), asyncpg, Postgres 14.23,
Python 3.12. **1,934 GitHub stars** (live). Reuses the `bse_hunt` substrate.
**Sweep context:** ascending-maturity ≥1000★, PIVOTED to L3 feature-interaction
(two individually-correct ops corrupting shared state only in combination) —
not another leak. Stop-at-first-bug.

## *** STRONGEST CANDIDATE OF THE PROJECT — silent L3 data-loss bug ***

Unlike anyio/aiocache (real leaks but one-line fixes, low differentiation) and
every dry surface, this is a **silent data-loss interaction bug**: no
exception, tests green, the object even reports a valid id — yet the row is
gone. Symptom (data vanished) is maximally far from cause (a stale flag
surviving rollback). This is the L3 shape the whole strategy targets.

## The bug (teeth-verified)

```
1. obj.save() INSIDE a transaction runs INSERT, assigns obj.id, and sets
   obj._exists_in_db = True.                                        [correct]
2. the transaction ROLLS BACK — the row is gone from the DB.        [correct]
3. obj._exists_in_db STAYS True (and obj.id stays set) — never reverted
   when the transaction that created the row rolled back.           [THE BUG]
4. obj.save() AGAIN (outside the txn): piccolo's save() branches on
   `if not self._exists_in_db` -> False -> takes the UPDATE path ->
   `UPDATE ... WHERE pk == <phantom id>` -> matches 0 rows -> the object
   is SILENTLY LOST. No error. total rows = 0, but obj.id = 1.      [DATA LOSS]
```

Root cause located to `piccolo/table.py::save()`: the insert/update decision
is driven by the instance flag `self._exists_in_db`, which is set on a
successful insert but **not rolled back with the enclosing transaction**.

Verified directly:
```
before save:            _exists_in_db = False
inside txn after save:  _exists_in_db = True  id = 1
after rollback:         _exists_in_db = True  id = 1   <- stale True is the bug
```

## Measurement (Rule 9 — reproduced before theorising)

| Probe | Result |
|---|---|
| P1 phantom id survives rollback | `obj.id` still set after rollback = **True** |
| P2 re-save after rollback | `total=0 named_payload=0 obj_id=1` — **row silently lost** |

Teeth PASS (decisive): on the COMMIT path the probe reports `total=1` (row
genuinely persisted, re-save updates it correctly); only the ROLLBACK path
reports `total=0`. So the probe distinguishes real data loss from correct
persistence — not a false positive.

## Novelty (checked, not assumed)

- GitHub issue/PR search across 4 phrasings ("rollback save id", "transaction
  rollback object id", "save after rollback update", "phantom id rollback"):
  **0 relevant hits**. The one match (#1324) is an unrelated migration issue.
- `save()`'s docstring documents the `columns=` param only — nothing about
  `_exists_in_db` semantics across transaction boundaries. Not documented
  behaviour; an ORM silently dropping a `save()` is a defect, not a contract.

## Why this differentiates models (vs. anyio/aiocache)

- **Not a one-liner-obvious fix.** The naive fix "reset `_exists_in_db` on
  rollback" requires the transaction machinery to track which objects it
  touched and revert their flags on rollback — piccolo's `Transaction` does
  not currently hold object references. That is a real design change spanning
  `engine/*` (transaction) and `table.py` (object state), NOT a single line.
  A weaker model is likely to "fix" it by patching the reproducer (re-inserting
  manually) or resetting the flag in the wrong place; a stronger model must
  design the object<->transaction back-reference. Symptom↔cause distance is
  large and the correct fix has architectural choices — the differentiation
  the anyio/aiocache one-liners lacked.
- **Objective, silent grade.** After a fix: save-in-rolled-back-txn then
  re-save MUST leave exactly 1 row (INSERT, not UPDATE-into-void). A wrong or
  masking fix leaves 0 rows or throws. No throughput/memory measurement needed
  — a correctness assertion the model either passes or fails.

## Open validation risks (must test before shipping as an eval)

1. **Confirm the fix is genuinely non-trivial** — spike the real fix to see if
   it spans transaction+table or collapses to a one-liner after all.
2. **Symptom-only prompt** must describe only "data occasionally disappears
   after a rolled-back transaction" without naming `_exists_in_db`, save's
   insert/update branch, or rollback-flag-reset.
3. **Real A/B run** — the simulated-fix trap from aiocache: two plausible fixes
   may still both pass a naive grader. Need a grader that also rejects
   symptom-masking fixes (e.g. one that resets the flag but breaks the normal
   commit path — teeth already cover this direction).

## Fix spike (2026-08-03) — CONFIRMS the fix is non-trivial

Spiked the real fix end-to-end. It works (rollback→re-save now re-INSERTs,
`total=1`, no loss; commit path still `total=1` — normal saves intact). The
spike's value is proving the fix is NOT a one-liner. It spans **2 files, 4
sites, and hit a design constraint a naive attempt trips on**:

1. `engine/postgres.py` `PostgresTransaction.__slots__` — the class uses
   `__slots__`, so you CANNOT just `self._inserted_objects = []`; it raises
   `AttributeError` until the slot is declared. A weak attempt fails here with
   a confusing error and no obvious cause. (+1 line)
2. `__init__` — initialise the tracking list before the nested-txn branch. (+1)
3. new `register_inserted()` method + revert loop in `rollback()`. (+7)
4. `query/methods/insert.py` `_raw_response_callback` — the INSERT success
   path must look up the *active* transaction via
   `self.table._meta.db.current_transaction.get()` and register the flipped
   object with it. (+3)

Crucially, the transaction object holds NO object references today — the fix
introduces an object↔transaction back-reference that does not exist in the
codebase. That is a design decision (where to track, when to clear, slots
constraint, and the same change is needed in the SQLite engine + the m2m /
objects.py insert paths), not a mechanical edit. This is exactly the
investigation+design depth anyio's and aiocache's one-liners lacked.

**Differentiation ceiling: HIGH (est.).** A weaker model is likely to (a) reset
`obj.id`/`_exists_in_db` in the wrong place (e.g. in `save()` or the reproducer),
(b) trip the `__slots__` constraint and give up or hack around it, or (c) mask
the symptom by re-inserting in caller code — all of which a correctness+teeth
grader rejects. A stronger model must locate the stale flag AND design the
back-reference. Whether this bears out needs a real A/B, but unlike the prior
two candidates the fix is genuinely architectural.

Stock piccolo restored after the spike; bug reproduces.

## Eval task built + grader validated (2026-08-03)

Shipped `eval-tasks/piccolo-txn-data-loss/` (symptom-only prompt, reproducer,
5-axis rubric, clean-room builder) + `scripts/grade_piccolo_evals.sh`.

**Self-contained on SQLite.** The bug reproduces identically on piccolo's
SQLite engine (`piccolo[sqlite]`, on-disk temp file, no server) — `total=0`,
silent loss. So the eval needs no Postgres. Confirmed the SQLite engine shares
the same architecture (`__slots__`, no object refs, same `rollback`), so the
fix is equally non-trivial there.

**Prompt is symptom-only (audited).** Describes only "records disappear after a
rolled-back transaction, retry reports success, tests pass." Zero cause-terms:
audited against a giveaway list (`_exists_in_db`, insert/update, phantom, flag,
stale, id, back-reference, `__slots__`, `current_transaction`, …) — all absent.
`make-eval-dirs.sh` re-runs that guard and refuses to build if any leaks in.

**Grader DISCRIMINATES (the property aiocache lacked).** Five gates: RETRY (the
bug), COMMIT, PLAIN, UPDATE (rejects always-insert), ROLLBACK (rejects
disabling rollback). Validation:
- Baseline (both dirs unpatched): `RETRY=FAIL VERDICT=FAIL` — bug detected.
- Correct fix (the spike, on sqlite.py + insert.py): `VERDICT=PASS`, all gates.
- Masking cheat #1 (force-always-INSERT in save()): CRASH — `UNIQUE constraint
  failed: rec.id` on the re-insert. The DB itself rejects the duplicate.
- Masking cheat #2 (weaken the insert guard): CRASH.

Unlike the aiocache grader (where a correct fix AND a lazy fix both passed every
gate), here the correct fix passes and both obvious cheats fail. The UPDATE gate
specifically kills the "just always insert" shortcut. This is the first grader
in the project that separates a real fix from a plausible-but-wrong one.

Clean-room dirs restored to unpatched baseline; ready for a real Bonsai-CLI A/B.

## Status: STRONG L3 CANDIDATE — task built, grader validated, ready for real A/B

Per stop-at-first-bug, the sweep pauses. This is the first bug whose FIX is
plausibly non-trivial (not a one-liner), which is the property anyio/aiocache
lacked. Next step: spike the real fix + build the symptom-only task + A/B —
same validation pipeline, higher expected ceiling.

## Sweep scoreboard

| # | Target | Stars | Class | Result |
|---|---|---:|---|---|
| 1 | odmantic | 1,174 | — | skipped (MongoDB) |
| 2 | aiocache | 1,435 | L2 leak | real leak, one-line fix, low differentiation |
| 3 | ormar | 1,804 | L3 relation | dry (documented) |
| 4 | **piccolo** | **1,934** | **L3 txn+identity** | **SILENT DATA LOSS — strong candidate (this doc)** |
| — | fastapi/sqlalchemy/starlette | — | — | dry/correct |
| — | anyio | — | L2 leak | real, too easy |
