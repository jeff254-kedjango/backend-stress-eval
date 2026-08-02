Work within this directory only.

We have a bug where records occasionally disappear, and we cannot explain it.

In our service we sometimes persist a record inside a database transaction. If
something goes wrong partway through, that transaction is rolled back — which
is expected, and correct. Later, on a retry path, we persist the same record
again, this time outside of any transaction. That second attempt reports
success and the record object looks completely normal afterwards.

But the record is not in the database. It is simply gone. No exception is
raised, nothing is logged as failed, and our tests all pass — yet the row that
we saved (on the retry) is missing. It only happens to records that went
through a rolled-back transaction earlier; records we persist normally are
always fine.

We have reduced it to a small standalone reproducer with no test framework and
a single third-party dependency, using a throwaway local database. Running it
persists one record and then reports how many records are actually stored. It
should be one. It is zero.

Find why the retry fails to store the record even though it reports success,
and fix it so that persisting a record after an earlier rolled-back transaction
reliably stores it. After the fix, a record saved on the retry path must end up
in the database exactly as a normally-saved record does, records that were
never part of a rolled-back transaction must keep working exactly as before,
and the fix must address where the loss actually originates rather than masking
it in the reproducer or caller code.
