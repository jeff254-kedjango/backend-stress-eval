# anyio lifecycle leak — eval task

**Framework:** [anyio](https://github.com/agronholm/anyio) 4.14.2
**Language:** Python 3.12
**Task type:** find-and-fix (backend memory bug)
**Estimated model time:** 45 min – 2 h

## The bug

Repeatedly calling `anyio.run(coro)` where `coro` invokes
`anyio.to_thread.run_sync` — a common shape for **lifecycle soak-tests**,
**test-suite runners**, and **worker processes that re-enter fresh event
loops** — leaks Python heap at ~5 KB per invocation. Over a 500-call
loop, the process gains ~2.5 MB. Every subsequent call adds another
~5 KB; the growth is linear and does not saturate within the reproducer's
range.

The minimum reproducer is 10 lines (`minimal_repro.py` in this
directory), with **only anyio as a dependency**:

```python
import anyio

async def work():
    await anyio.to_thread.run_sync(int)

if __name__ == "__main__":
    for _ in range(500):
        anyio.run(work)
```

Running it on anyio 4.14.2 + Python 3.12.13 (Linux) with `tracemalloc`
snapshots at iteration 10 and 499 produces a heap delta of ~2549 KB,
attributed principally to five lines in
`anyio/_backends/_asyncio.py`:

| Line | Source | KB/iter |
|---:|---|---:|
| 2481 | `runner.run(wrapper())` inside `with Runner(...) as runner:` | +0.22 |
| 2598 | `idle_workers = deque()` in `run_sync_in_worker_thread` | +0.74 |
| 2599 | `workers = set()` (same fn) | +0.21 |
| 2052 | `self._borrowers: set[Any] = set()` in `CapacityLimiter.__init__` | +0.21 |
| 2053 | `self._wait_queue: OrderedDict[…] = OrderedDict()` | +0.13 |

Plus downstream allocations in `asyncio.base_events`, `asyncio.events`,
`weakref`, and `_weakrefset` — all reachable from the anyio backend's
state.

The mechanism is that each `AsyncIOBackend.run()` invocation creates a
fresh `Runner` (and thereby a fresh event loop), and the worker-pool
state (`idle_workers`, `workers`, the `CapacityLimiter`'s `_borrowers`
and `_wait_queue`) is stored in `ContextVar` slots that are re-created
per loop. Weakref bookkeeping and Future references from
`f.set_result(func(*args))` in `run_sync_from_thread` hold refs across
loop boundaries.

## Your task

Produce a patch to `anyio` that makes the minimum reproducer's
per-iteration heap growth drop **below 1 KB/iter** without regressing
any of the other grading criteria (see `RUBRIC.md`). The fix must be
contained to the `anyio` package.

Do **not** modify:
- the minimum reproducer (`minimal_repro.py`)
- the harness (`measure.py`)
- the grader (`grade.py`) or the baseline (`baseline-attribution.json`)
- the rubric (`RUBRIC.md`)

The grading is machine-checkable and objective — see `RUBRIC.md` for the
four gates and the exit-code contract.

## §Final questions

Before you start, answer these to yourself:

1. **Where does the state come from?** Which anyio backend function
   allocates the objects that survive across `anyio.run()` calls?
2. **Why doesn't `Runner.__exit__` (or `loop.close()`) release it?**
   ContextVars, weakrefs, or module-level singletons — which?
3. **What class of fix is available?** Explicit release in
   `AsyncIOBackend.run_sync_in_worker_thread`'s finalization? Cache
   reuse via `BlockingPortalProvider`? Deleting the ContextVar state on
   loop close?
4. **Does your fix regress the intent?** anyio's threadpool exists for
   a reason. If you evict too eagerly, sync-call performance across a
   single-loop lifespan degrades. What's your evidence that your fix
   is scoped to the per-loop cleanup path?

## Reproducing the bug end-to-end

```bash
pip install anyio==4.14.2
python minimal_repro.py    # runs silently; add tracemalloc/psutil to see
python measure.py --out replay/report.json    # 500-round tracemalloc report
python grade.py baseline-attribution.json replay/report.json    # FAIL, exit 1
```

## Grading

`reproduce.sh` runs the full loop. See `RUBRIC.md` for the four gates.

## Novelty declaration

Four searches conducted 2026-08-02:

- `anyio _asyncio.py memory leak repeated event loop TaskGroup portal` (github.com)
- `anyio BlockingPortal repeated start stop memory leak weakref` (github.com)
- `starlette TestClient repeated enter exit memory leak asyncio` (github.com)
- `"anyio" "_TaskGroup" OR "start_blocking_portal" leak accumulation` (github.com)

Closest CPython issue found is
[cpython#140947](https://github.com/python/cpython/issues/140947)
("context variables can leak out of asyncio.Task") — closed, but
describes a **single-event-loop** server-protocol context-isolation
bug, not the repeated-loop accumulation shape here.

anyio 4.4 added `BlockingPortalProvider` to avoid repeated portal
construction, but that PR is framed as an ergonomic improvement, not a
leak fix; its release notes and PR discussion do not describe
per-invocation heap growth on `AsyncIOBackend.run()`.

The specific attribution — anyio backend worker-pool state accumulating
across `AsyncIOBackend.run()` invocations — is not documented anywhere
we found. See `../../_shelved/fastapi-0.141.1-lifecycle-leak/SHELVED.md`
for the prior eval-task that was shelved as retrievable-from-upstream;
this task is deliberately positioned to avoid that outcome.

## Provenance

- **anyio commit hash to fix against:** whatever ships as `anyio==4.14.2`
  (upstream tag `4.14.2`).
- **Grading anyio version + Python version:** hard-baked into
  `baseline-attribution.json` and re-checked by grade.py G4.
- **Discovery harness that surfaced this:** `~/backend-stress-eval` at
  commit `8305dac` (see git log; standalone repo).
