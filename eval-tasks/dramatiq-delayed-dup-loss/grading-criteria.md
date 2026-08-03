# Grading criteria

For the task to be successful I'm expecting the following:

No duplication under worker hand-off. When a worker is holding a delayed job
that has not yet come due and that worker becomes unhealthy and is taken over by
another, the job is carried out **at most once** — a single execution reaches
the real work queue, never two. This is checked by driving the real hand-off
against a live instance and counting how many times the job is promoted to run;
the count must be exactly one, not two.

No loss under crash. When a worker holding a not-yet-due delayed job actually
goes away before the job comes due, the job is still carried out **exactly
once** — it is not silently dropped. This is the opposite failure from the one
above, and both must hold together: a fix that stops the duplication by letting
go of the job earlier, and thereby loses the job on a crash, does not pass. Only
a solution that keeps the job exactly-once in both situations counts.

The fix is applied where the problem originates. The hand-off of a delayed job
between workers is made correct at the layer that performs it, not worked around
in our own scheduling or caller code. The submitter can explain why the job
could previously be promoted twice (or dropped) and how the change makes a
single worker's takeover the authoritative one without a window in which two
workers both act or neither does.

It reproduces and resolves on a stock, current, supported release. The problem
is demonstrated on an unmodified current stable version of the dependency on a
supported Python, using only its public API — no pinned-old or vendored copy —
and the fix lives inside that dependency, not in test or caller code. Jobs that
never pass through a worker hand-off continue to behave exactly as before.
