Work within this directory only.

Our CI has started failing a memory ceiling. The job runs our test suite —
a few thousand small tests — and the worker process now finishes far heavier
than it started. On a trimmed-down run it climbs by roughly five kilobytes
per test and never levels off; over the full suite that is enough to trip the
limit and kill the job.

The tests pass. Each one passes on its own, and every fixture we own tears
down cleanly. We have gone through our own code twice: no global list is
growing, no cache is unbounded, no fixture is holding a reference it
shouldn't. Memory still climbs, one test at a time, in a straight line.

We have reduced it to a small standalone reproducer with no test framework
and a single third-party dependency. Running it exhibits the same steady
per-iteration growth.

Find why the process keeps growing, and stop it. Once one iteration finishes,
whatever it allocated should be released before the next begins, so the
process holds flat over thousands of iterations. The fix must address where
the growth actually originates rather than masking it, and it must not slow
down the repeated work the iterations perform.