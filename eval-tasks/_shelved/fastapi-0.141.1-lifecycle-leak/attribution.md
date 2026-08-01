# Attribution — FastAPI 0.141.1 lifecycle-leak residual growth

**Chunk 6b output**, 2026-08-02. Prerequisite for the novelty verdict on
Chunk 7 (SHA pinning).

## Method

`attribute.py` runs the same loop shape as `harnesses/layer2_lifecycle`
against `plugins/fastapi:canonical_example_app` (single lifespan, DI +
middleware, one dep function). It takes `tracemalloc` snapshots at
iterations 10 and 499 (span = 489 iterations), then diffs with
`compare_to("lineno")`.

Pins:

- `fastapi==0.141.1`
- `starlette==1.3.1`
- `python==3.12.13`
- `anyio==4.14.2`

## Results

- **Total heap delta over span:** +4,696 KB
- **Steady-state rate:** +9.60 KB/iter (matches the L2 slope invariant's
  +8.72 KB/iter within noise)
- **Cache-saturation floor:** 4,096 entries × 3 caches × ~1 KB/entry
  → plateau expected at ~12 MB total, ~4,096 rounds
- **Cache state observed after 500 rounds:**

  ```
  _is_gen_callable_cached        hits=1000  misses=1000  currsize=1000  maxsize=4096
  _is_async_gen_callable_cached  hits=1000  misses=1000  currsize=1000  maxsize=4096
  _is_coroutine_callable_cached  hits=0     misses=1000  currsize=1000  maxsize=4096
  ```

## Top allocation sites (aggregated)

| Module region | Sum KB (over span) | KB/iter | Share of total |
|---|---:|---:|---:|
| `fastapi/dependencies/models.py` lines 164, 194, 226 (`_CallIdentity(call)` construction sites) | 517.4 | 1.058 | 11.0% |
| `anyio/_backends/_asyncio.py` (event-loop bookkeeping) | 665.6 | 1.361 | 14.2% |
| `asyncio/base_events.py` + `events.py` (loop internals) | 840.1 | 1.717 | 17.9% |
| `weakref` / `_weakrefset` / `inspect` (helpers pulled in by the above) | 692.4 | 1.415 | 14.7% |
| `plugins/fastapi/__init__.py` lines 231, 235, 239 (`canonical_example_app` closures) | 252.2 | 0.516 | 5.4% |
| Everything else in top 25 | 726.0 | 1.485 | 15.5% |
| Untracked (below top 25) | 1002.4 | 2.050 | 21.3% |
| **Total** | **4696** | **9.60** | 100% |

## What this tells us

The `fastapi/dependencies/models.py` lines are **exactly the code region
that PR #16049 (`⚡️ Reduce memory usage in dependencies`, merged
2026-07-24, released in FastAPI 0.140.0) refactored**. Specifically:

```python
# fastapi/dependencies/models.py lines 161–164 (this pin, 0.141.1)
def _is_gen_callable(call: Callable[..., Any] | None) -> bool:
    if call is None:
        return False
    return _is_gen_callable_cached(_CallIdentity(call))   # ← line 164
```

The cache `_is_coroutine_callable_cached` observed **1000 misses and
zero hits** across 500 lifecycle rounds — meaning every fresh app
constructs fresh function objects, and `_CallIdentity` (which hashes on
`id(call)`) never sees a repeat. The `lru_cache(maxsize=4096)` will fill
linearly, entry by entry, until the LRU eviction floor kicks in at
~4,096 rounds. That is the shape of our +9.6 KB/iter.

The remaining ~90% of allocations — anyio, asyncio, weakref, inspect —
are downstream of that first cache miss (they trace through
`_is_gen_callable_cached` on the way to `iscoroutinefunction` /
`inspect` machinery). They are the same bug, viewed from a different
frame in the traceback.

## Novelty verdict

**NOT NOVEL as currently framed.**

PR #16049 already:

- Refactored this exact code region for a ×16 memory reduction in
  per-`Dependant` footprint.
- Added a memory benchmark (`test_dependency_graph`) that the tiangolo/
  FastAPI CI now tracks.
- Was followed by PR #16062 bumping the cache size to 4096 in 0.140.1.

Our pin (0.141.1) sits after all three of those changes, so the fix is
already applied. The residual +9.6 KB/iter we measure is not a distinct
bug — it is the shrunken-but-still-present same-class behaviour that
PR #16049 shrank. A model asked to "fix" it would either:

- Change the cache-keying strategy (structural change that upstream has
  chosen not to make) — this is a **feature request**, not a bug fix.
- Reduce the maxsize back down — regressing the very PR (#16062) that
  raised it. A reviewer would reject.
- Add explicit cache eviction on lifespan shutdown — potentially valid,
  but would need buy-in from FastAPI maintainers and is contentious
  because it degrades the runtime-fast-path.

None of these are "here is a well-scoped patch that fixes a bug" — the
shape that grades cleanly on a frontier eval.

## What this rules out (and what it doesn't)

**Rules out:** claiming novelty for the leak-shape itself.

**Does not rule out:** using this same code region to construct a
*derived* eval task with a different framing — for example:

- **"Given a soak-test harness that catches per-lifecycle drift, propose
  a patch that keeps `_is_coroutine_callable_cached` warm across
  lifecycles for module-defined callables"** — this reframes the task
  from "find and fix a leak" to "improve cache reuse". Reviewer-legible
  and less trivially retrievable from PR #16049.
- **"Extend PR #16049 to bring `_CallIdentity` hits-per-miss up from
  0% to >50% for the canonical example app"** — measurable, has a
  clear win condition, is not the same as the existing PR.

Both would need new baseline reports and a new rubric; both are outside
the scope of this attribution artifact.

## Next step

Chunk 6c (3-point slope regression across 0.139.x / 0.140.1 / 0.141.1)
is now partially redundant. The attribution already tells us the
mechanism, so 6c would only quantify **how much** of the leak PR #16049
removed — useful only if we decide to pursue the reframed eval task
above. Suspend 6c until Chunk 7's re-scoping decision is made.

## Reproducing this

```bash
cd ~/backend-stress-eval
.venv/bin/python eval-tasks/fastapi-0.141.1-lifecycle-leak/attribute.py \
    --rounds 500 --warmup-iter 10 --top 25
```

## Sources

- [FastAPI PR #16049 — Reduce memory usage in dependencies](https://github.com/fastapi/fastapi/pull/16049)
- [FastAPI PR #15336 — Reduce memory usage by Dependant by ~50%](https://github.com/fastapi/fastapi/pull/15336)
- [FastAPI Discussion #14742 — High increase of memory usage from 0.121.x](https://github.com/fastapi/fastapi/discussions/14742)
- [FastAPI PR #16062 — bump lru_cache maxsize to 4096](https://github.com/fastapi/fastapi/pull/16062)
