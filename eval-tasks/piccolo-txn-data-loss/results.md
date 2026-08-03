# piccolo txn-rollback data-loss — run results

Per-task run log. **Time-to-fix and turns are first-class metrics** — two
models can reach the same passing grade with very different effort, and that
effort gap is a differentiation signal the 5 pass/fail gates alone miss.

## Protocol note

- **prompt-only** (current standard): the model gets ONLY `initial-prompt.md`.
  No reproducer. This is the faithful L3 protocol — the model must localize the
  loss itself.
- **assisted** (deprecated): an earlier run also shipped `minimal_repro.py`,
  which pre-localizes the bug. Times from assisted runs are **not comparable**
  to prompt-only times and are flagged below.

Fill `time` and `turns` in by hand from the run (wall-clock minutes; message/
turn count from the transcript).

## Runs

| date | model | protocol | time (min) | turns | 5-gate verdict | notes |
|------|-------|----------|-----------|-------|----------------|-------|
| 2026-08-03 | A | assisted (with repro) | 4 | — | PASS (all 5) | repro shipped → bug pre-localized; 4 min not comparable to prompt-only |
| 2026-08-03 | B | prompt-only | longer than A | — | PASS (all 5) | repro deleted before run; took noticeably longer to debug — the intended difficulty |

## A-vs-B fix comparison (2026-08-03)

Both models converged on the **same architecturally-correct fix** and passed
all five gates (RETRY / COMMIT / PLAIN / UPDATE / ROLLBACK). Both touched the
same four files; neither touched `table.py`:

- `engine/base.py` — add a rollback-callback registry to `BaseTransaction`.
- `engine/sqlite.py` + `engine/postgres.py` — run the callbacks in `rollback()`.
- `query/methods/insert.py` — on insert-inside-txn, register an undo that
  reverts `_exists_in_db` + PK if the transaction rolls back.

Difference in care (not caught by the gates):

- **Model B** added `_rollback_hooks` to `__slots__` on *both* concrete
  transaction classes and initialized it in `__init__` — clean.
- **Model A** left the concrete `__slots__` alone, added `_rollback_callbacks`
  to the *abstract* `BaseTransaction.__slots__`, and lazy-inits via
  `try/except AttributeError` — works, slightly hackier.

Both avoided the `__slots__` trap the task was designed to expose.

## Known harness gaps

- **Grader diff signal is blind.** `grade_piccolo_evals.sh` only diffs
  `table.py`, which the correct fix does not touch → it printed `+0/-0` for both
  models and gave zero location signal. The real fix lives in `engine/*` +
  `query/methods/insert.py`. Diff those (or the whole package) by hand.
- **Timing/turns are manual.** Recorded by hand in the table above.

## Bottom line

The 5 gates do not differentiate frontier models on this bug — both produce the
same correct architecture. The differentiation lives in **time-to-fix under the
prompt-only protocol** (and in fix-quality details like `__slots__` handling),
which is why the reproducer was removed from the harness.
