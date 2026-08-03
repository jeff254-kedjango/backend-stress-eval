# Grading criteria

For the task to be successful I'm expecting the following:

**All three flows reliably store exactly one record.** After the fix, each of
the three described flows — persisting a record whose earlier transaction was
rolled back, persisting a record whose in-transaction deletion was rolled back,
and persisting a record that was discarded by undoing part of a transaction to
an earlier checkpoint — must, on the retry, leave exactly one copy of that
record in the database. Not zero (silently lost) and not two (silently
duplicated). This holds whether the record is persisted once or several times on
the retry path.

**Normal persistence is unchanged.** Records that never went through a
rolled-back or partially-undone flow must behave exactly as before: a record
saved outside any transaction is stored, a record saved and committed inside a
transaction is stored, deleting a record and committing removes it, and updating
an existing record still updates it in place rather than creating a duplicate.
The fix must not introduce duplicate rows on the normal path, and must not turn a
legitimate in-place update into a second insert.

**The fix addresses where the problem originates — one root, not three
symptoms.** All three flows share a single underlying cause: an operation
mutates the record's in-memory persistence state, that mutation is only
provisional until the surrounding transaction (or the work since a checkpoint)
is committed, and when it is instead discarded the in-memory state is never
brought back in line with the database. A strong solution corrects this at the
layer that owns persistence state across a transaction/checkpoint boundary, so
all three flows are fixed by the same mechanism — not patched one symptom at a
time in the caller, and not by clearing/re-creating the object or issuing a raw
insert on the retry. The submitter can explain what state each record carried
out of the discarded work, why the retry then duplicated or lost it, and why no
error surfaced.

**No new failure is introduced.** Committing a transaction still persists its
records, rolling one back still discards them, undoing to a checkpoint still
discards only the work after it, and nested or repeated transactions behave as
before. The fix must not leave a record in a state where a later save raises,
duplicates, or corrupts data.

**It reproduces and resolves on a stock, current, supported release.** The
problem is demonstrated on an unmodified current stable version of the
dependency on a supported Python, using only its public API — no pinned-old or
vendored copy — and the fix lives inside that dependency, not in test or caller
code.
