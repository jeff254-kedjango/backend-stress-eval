Work within this directory only.

Our service caches per-user session data in an in-process cache. Under
sustained traffic the worker's memory climbs without bound and eventually the
process is killed by our memory ceiling — but the number of distinct things we
cache is bounded, and old entries are supposed to age out on their own.

We cache each entry with a short time-to-live when it is first written, and
then refresh the same entry in place as the user stays active. The cache never
holds more than a bounded working set of live users, so memory should settle.
It does not — it rises in a straight line the longer the process runs.

Functionally everything is correct. Every value we read back is right, every
entry we expect to be present is present, and our whole test suite passes. The
growth is invisible to those tests; we only see it as steadily rising RSS in
production.

We have reduced it to a small standalone reproducer with no test framework and
a single third-party dependency. Running it exhibits the same steady
per-iteration growth even though only a bounded set of keys is ever live.

Find why the process keeps growing, and stop it. After the fix, running the
workload for many iterations should hold memory flat instead of climbing, the
cached values must stay exactly as correct as they are now, and the ordinary
refresh path must not get slower. The fix must address where the growth
actually originates rather than masking it in the reproducer or caller code.
