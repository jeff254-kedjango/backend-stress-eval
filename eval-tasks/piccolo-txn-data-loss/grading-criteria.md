# Grading criteria

For the task to be successful I'm expecting the following:

The lost record is reliably stored. After the fix, persisting a record on the
retry path — the path taken after an earlier transaction for that record was
rolled back — stores exactly one record in the database. Running the reproducer
reports one stored record, not zero. This holds whether the record is persisted
once or several times on the retry path.

Normal persistence is unchanged. Records that were never part of a rolled-back
transaction must behave exactly as before: a record saved outside any
transaction is stored, a record saved and committed inside a transaction is
stored, and updating an existing record still updates it in place rather than
creating a duplicate. The fix must not introduce duplicate rows, and must not
turn a legitimate in-place update into a second insert.

The fix addresses where the loss originates. The root cause is corrected inside
the dependency, at the layer that owns the record's persistence state across a
transaction boundary — not worked around in the reproducer or the caller's
code (for example by clearing the object, re-creating it, or issuing a raw
insert on the retry). The submitter can explain what in-memory state the record
carried out of the rolled-back transaction, why the retry then failed to store
it, and why no error surfaced.

No new failure is introduced. Committing a transaction still persists its
records, rolling one back still discards them, and nested or repeated
transactions behave as before. The fix must not leave the record in a state
where a later save raises, duplicates, or corrupts data.

It reproduces and resolves on a stock, current, supported release. The problem
is demonstrated on an unmodified current stable version of the dependency on a
supported Python, using only its public API — no pinned-old or vendored copy —
and the fix lives inside that dependency, not in test or caller code.
