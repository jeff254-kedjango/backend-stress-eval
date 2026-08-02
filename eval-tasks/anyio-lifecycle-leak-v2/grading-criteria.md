# Grading criteria

For the task to be successful I'm expecting the following:

The process holds flat over a long run. After the fix, running the workload
for several thousand iterations shows memory settling to a stable baseline
instead of climbing — the steady per-iteration growth is gone, not merely
reduced, and it does not creep back over a longer run.

The fix is applied at the layer where the growth actually originates. The
retained state is released at the source rather than worked around in the
reproducer or in the caller's code. The submitter can explain what was being
retained across iterations and why ordinary teardown did not release it.

Repeated work stays fast. The mechanism that offloads and repeats work within
a single run is untouched in the hot path — a run that performs the same
operation many times shows no throughput regression compared to before the
fix. A "fix" that clears the growth by tearing down and rebuilding shared
machinery on every operation does not count.

It reproduces and resolves on a stock, current, supported release. The problem
is demonstrated on an unmodified current stable version of the dependency on a
supported Python, using only its public API — no pinned-old or vendored copy —
and the fix lives inside that dependency, not in test or caller code.
