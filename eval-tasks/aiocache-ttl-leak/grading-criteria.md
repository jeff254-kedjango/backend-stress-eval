# Grading criteria

For the task to be successful I'm expecting the following:

The process holds flat over a long run. After the fix, running the workload
for many tens of thousands of iterations shows memory settling to a stable
baseline instead of climbing — the steady per-iteration growth is gone, not
merely reduced, and it does not creep back over a longer run. Because only a
bounded set of keys is ever live, the internal bookkeeping the cache keeps for
those keys must also stay bounded, not grow once per refresh.

The fix is applied at the layer where the growth actually originates. The
retained state is released at the source — inside the cache backend — rather
than worked around in the reproducer or in the caller's code. The submitter
can explain what was being retained across refreshes and why the ordinary
teardown/expiry path did not release it.

Cached values stay exactly correct. The fix must not change what the cache
returns: every key that should be present is still present, every value read
back is unchanged, and entries that carry a real TTL still expire on time. A
"fix" that drops or corrupts cached data, or that disables expiry, does not
count.

The refresh path stays fast. The in-place refresh that the workload performs
on every iteration is the hot path — it must show no throughput regression
compared to before the fix. A "fix" that reclaims the growth by tearing down
and rebuilding cache-wide structures on every write does not count.

It reproduces and resolves on a stock, current, supported release. The problem
is demonstrated on an unmodified current stable version of the dependency on a
supported Python, using only its public API — no pinned-old or vendored copy —
and the fix lives inside that dependency, not in test or caller code.
