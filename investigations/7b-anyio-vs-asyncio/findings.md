# Chunk 7b-4 — cut anyio out to isolate lifecycle-drift attribution

**Question:** in the FastAPI 0.115.0 rediscovery result showing ~+6-7 KB/iter
lifecycle drift with attribution spanning anyio + asyncio + weakref + FastAPI,
which component is actually responsible?

## Method

Two comparable 500-round runs on the same fastapi 0.115.0 scratch venv,
tracemalloc snapshots at iter 10 vs iter 499, `compare_to("lineno")`:

1. **Baseline (anyio-included).** Uses ``fastapi.testclient.TestClient``,
   which invokes ``anyio.from_thread.start_blocking_portal`` internally.
   App has one sync ``def _root()`` handler — Starlette dispatches it through
   ``run_in_threadpool`` → ``anyio.to_thread.run_sync``.
2. **Anyio-cut.** Uses raw ``asyncio.new_event_loop()`` + direct ASGI
   protocol calls (``app({"type": "lifespan"}, receive, send)``, then
   ``app({"type": "http", ...}, receive, send)``). App has one ``async def
   _root()`` handler — no sync bridge, no anyio worker pool invocation.

Both runs identical otherwise: same fastapi 0.115.0, same starlette 0.38.6,
same anyio 4.14.2, same Python 3.12.13, same 500 rounds, same iter 10 → 499
snapshot span.

## Results

|                     | With anyio (TestClient) | Anyio-cut (raw asyncio + async handlers) |
|---------------------|------------------------:|-----------------------------------------:|
| Steady-state slope  |         +6.38 KB/iter   |                          **+0.98 KB/iter** |
| Total delta         |              +3,122 KB  |                                    +477 KB |
| Top attribution     | anyio + asyncio + weakref dominate | fastapi + starlette + pydantic dominate |
| `anyio/_backends/_asyncio.py` in top-25? | yes (5 lines, ~35% of total) | **absent** |
| `asyncio/base_events.py` in top-25? | yes (3 lines, ~27% of total) | **absent** |

## Interpretation

**~85% of the lifecycle drift on fastapi 0.115.0 attributes to anyio's
asyncio-backend scaffolding.** Cut it out (async handlers, direct ASGI,
no TestClient / no BlockingPortal) and the drift collapses to under the
slope invariant's 1.0 KB/iter grade line.

The remaining +0.98 KB/iter is fastapi/starlette/pydantic setup
allocations that persist across lifecycles — small enough to grade as
PASS on the existing invariant, so it's not a bug worth chasing further.

### Specific anyio lines implicated

From the anyio-included run (fastapi 0.115.0, top-attribution over
489 iterations):

| Line | Source | KB/iter | What it is |
|---|---|---:|---|
| `_backends/_asyncio.py:2481` | `return runner.run(wrapper())` inside `with Runner(...) as runner:` | +0.22 | Per-`AsyncIOBackend.run()` fresh event-loop scaffolding |
| `_backends/_asyncio.py:2598` | `idle_workers = deque()` in `run_sync_in_worker_thread` | +0.74 | Fresh per-loop threadpool state (stored in a ContextVar) |
| `_backends/_asyncio.py:2599` | `workers = set()` (same fn) | ← same allocation | ← paired |
| `_backends/_asyncio.py:2052` | `self._borrowers: set[Any] = set()` in `CapacityLimiter.__init__` | +0.21 | Per-loop limiter state |
| `_backends/_asyncio.py:2701` | `f.set_result(func(*args))` inside `run_sync_from_thread` wrapper | +0.24 | Future set from worker; Futures hold loop refs |

Consistent with the mechanism the searches surfaced: repeated `Runner`
construction leaves scaffolding referenced through ContextVars and
weakref-attached bookkeeping, which asyncio's own `weakref` /
`_weakrefset` structures capture across loop boundaries.

## Novelty status

Improved. Instead of "leak spans anyio + asyncio + weakref", the sharpened
claim is **"anyio's asyncio backend leaks per-loop threadpool + limiter
state on repeated ``AsyncIOBackend.run()`` invocations."** Still not
documented anywhere the four novelty searches found (see 7b-3 task and
[[backend-stress-eval-6b-attribution]]).

## What this DOESN'T prove

- Whether the leak is anyio-specific or is a symptom of anyio's use of
  `asyncio.Runner` (the underlying mechanism could still be a cpython
  refcycle in `Runner.__exit__`). To distinguish, we'd have to also cut
  `Runner` and use raw `asyncio.new_event_loop() + loop.run_until_complete
  + loop.close()` — but the anyio-cut run already uses that pattern in
  `_one_lifecycle()`, and it comes up clean. So it's very likely anyio.
- ~~Whether the leak reproduces without the FastAPI/Starlette layer at all
  (pure `anyio.run(some_async_fn)` in a loop). Not measured here.~~
  **RESOLVED — see 7b-5 addendum below.**
- Whether newer anyio versions (e.g., a hypothetical 4.15) already fix
  this. anyio 4.14.2 is the current stable at time of writing.

## 7b-5 addendum — bare-anyio reproducer confirms end-to-end

Written 2026-08-02 alongside `bare_anyio_repro.py`. Two modes:

- **`run-only`** — `anyio.run(async def f: pass)` in a loop; no worker pool
- **`run-and-thread`** — `anyio.run(coro)` where the coro does one
  `await anyio.to_thread.run_sync(int)`; invokes the worker pool once/iter

Both modes tested at 500 rounds, snapshot iter 10 → 499, anyio 4.14.2 in
`.venv-fastapi-0.115.0/` (the only anyio present in the env — FastAPI +
Starlette are installed but never imported by this script).

| Mode | Slope | Top anyio backend lines |
|---|---:|---|
| `run-only`      | **+0.40 KB/iter** | Only L2457 (Runner scaffold), low share |
| `run-and-thread`| **+5.21 KB/iter** | L2598 (workers=set), L2599 (idle_workers=deque), L2481 (runner.run), L2052 (limiter borrowers), L2053 (limiter wait_queue) — the exact list from the FastAPI attribution |

**Conclusion:** the leak is entirely in `anyio.to_thread.run_sync` /
worker-pool state. `asyncio.Runner` alone is nearly clean (+0.40 KB/iter
is within noise). FastAPI/Starlette are only ever consumers that route
sync handlers through `run_in_threadpool` → `anyio.to_thread.run_sync`
and thereby expose the leak.

**Minimum reproducer** (10 lines, no third-party deps other than anyio):

```python
import anyio

async def f():
    await anyio.to_thread.run_sync(int)

if __name__ == "__main__":
    for _ in range(500):
        anyio.run(f)
    # Now inspect RSS or run under tracemalloc — ~2.5 MB grew above baseline.
```

That's exactly the shape a frontier-eval task wants: single file, single
dependency, one-page task statement.

### Reproducing 7b-5

```bash
.venv-fastapi-0.115.0/bin/python investigations/7b-anyio-vs-asyncio/bare_anyio_repro.py \
    --mode run-and-thread --rounds 500 --warmup-iter 10 --top 15
```

## Reproducing

```bash
# From ~/backend-stress-eval, with the .venv-fastapi-0.115.0/ scratch venv already built:
.venv-fastapi-0.115.0/bin/python investigations/7b-anyio-vs-asyncio/raw_asyncio_attribute.py \
    --rounds 500 --warmup-iter 10 --top 25
```

For the anyio-included baseline number, use the shelved
``attribute.py`` (also runs against the scratch venv, since the plugin
imports work unchanged there):

```bash
.venv-fastapi-0.115.0/bin/python eval-tasks/_shelved/fastapi-0.141.1-lifecycle-leak/attribute.py \
    --rounds 500 --warmup-iter 10 --top 25
```

## Decision this feeds

Chunk 8 (rubric axes) can now target a **clean, single-vendor eval-task
scope**: "reduce anyio-attributed per-lifecycle drift below 1 KB/iter",
with 3-4 orthogonal gates. Chunk 7 (new eval-task packaging) is unblocked.

## v2 model comparison — long-run flatness (2026-08-02)

Graded the two Bonsai-CLI outputs (`~/eval-outputs/model-{A,B}-2026-08-02`)
for the v2 symptom-only anyio task. Both patch the same root cause — the
finished root task strong-refs the per-loop `RunVar` mapping — but at
opposite ends of the cycle:

- **Model A** (23-line diff): unrolls the `Runner` context manager and, in a
  `finally`, manually evicts the loop's entry via the private
  `RunVar._clear_token(loop)`. Post-hoc cleanup; depends on teardown order +
  a private API.
- **Model B** (9-line diff): stores `_root_task` as a `weakref.ref` and
  dereferences on read in `find_root_task()`. Removes the strong reference by
  construction; no manual eviction.

Ran `scripts/flatness_check.py` (RSS-slope + live-loop probe) at increasing
scale. Clean unpatched anyio 4.14.2 is the leaking baseline.

| Scale | Model | Slope KB/iter | RSS growth | Live loops |
|---|---|---:|---:|---:|
| 4,000    | clean (unpatched) | 5.27  | +19,712 KB | 4,000  |
| 4,000    | A                 | 0.05  | +512 KB    | 0      |
| 4,000    | B                 | 0.05  | +512 KB    | 0      |
| 400,000  | A                 | 0.004 | +2,232 KB  | 0      |
| 400,000  | B                 | 0.002 | +2,256 KB  | 0      |

**Conclusion:** both fixes hold flat at 100× scale. Slope *drops* with more
iterations (0.05 → ~0.003), the signature of a one-time page-pool bump being
amortized, not a slow leak — the unpatched baseline would have grown ~2 GB
over 400k calls; both grew ~2 MB. `live_loops` is 0 at every scale (vs.
== round-count when leaking): every event loop is collected, so the leak is
gone by construction, not merely slowed. At 400k the A-vs-B difference (~24 KB
total) is within run-to-run RSS noise.

**Eval-quality caveat:** both models fully pass ALL FOUR grading criteria at
any iteration count. The task does NOT differentiate them on the criteria —
only on fix economy/fragility (B's 9-line weakref vs A's 23-line private-API
cleanup), which the prose rubric doesn't score. By the strict "models must
perform differently on at least some grading criterion" bar, v2 is a WEAK
differentiator: the anyio leak is still too tractable for frontier models.
See [[backend-stress-eval-6b-attribution]].
