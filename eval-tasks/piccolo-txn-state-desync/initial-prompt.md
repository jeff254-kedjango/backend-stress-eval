Work within this directory only.

We keep hitting a family of bugs we can't explain: after certain database
operations, records either silently duplicate or silently vanish. No exception
is ever raised, nothing is logged as failed, and our tests all pass — yet the
data on disk is wrong.

We have narrowed it to three situations, and they only go wrong in these
specific combinations:

1. We persist a record inside a database transaction, something fails partway,
   and that transaction is rolled back — which is expected and correct. Later,
   on a retry, we persist the same record again outside any transaction. The
   retry reports success and the record object looks completely normal — but the
   record is not in the database. It is simply gone.

2. We delete an existing record inside a transaction, and then that transaction
   is rolled back — so the record should still be there, and in the database it
   is. Later we persist that same record again. This reports success too — but
   now there are two copies of it in the database where there should be one.

3. We persist several records inside one transaction, undo part of that
   transaction back to an earlier checkpoint we had marked (discarding only the
   most recent work), and then let the rest of the transaction complete
   normally. Later we persist one of the discarded records again. Success is
   reported — but that record never makes it into the database.

In every case the operations are individually correct: rolling back is correct,
undoing to a checkpoint is correct, deleting is correct, and persisting is
correct. The data only goes wrong when they are combined, and only for records
that passed through one of these rolled-back or partially-undone flows.
Records that never went through such a flow are always fine.

We have a throwaway local database and a single third-party dependency. Find why
these retries report success while duplicating or losing the record, and fix it
so that after any of these flows, persisting a record again reliably stores
exactly one copy of it — just as it would for a record that was never part of a
rolled-back or partially-undone flow. Records that never went through such a
flow must keep working exactly as before, and the fix must address where the
problem actually originates rather than masking it in the caller's code.
