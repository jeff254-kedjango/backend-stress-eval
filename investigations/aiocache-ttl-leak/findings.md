# aiocache SimpleMemoryCache TTL-handler leak — findings (2026-08-02)

**Target:** aiocache 0.12.3 (stock, current stable), Python 3.12. In-memory
backend, **no service required**. **1,435 GitHub stars** (live).
**Sweep context:** ascending-maturity, ≥1000★ floor, stop-at-first-bug.
Second rung (odmantic 1,174★ skipped — needs MongoDB).

## *** FIRST LIVE CANDIDATE OF THE SWEEP — teeth-verified REAL leak ***

Unlike every prior surface (fastapi/sqlalchemy/starlette/ormar — all
documented-or-on-sight), this is a genuine defect not explained by any
documented contract, and **not fixed on the current default branch**.

## The bug

`SimpleMemoryBackend._set` cancels an existing TTL handle but only *re-stores*
into `self._handlers` on the `if ttl:` branch. Overwriting a key that currently
has a TTL **without** a ttl cancels the old `TimerHandle` but leaves it in
`self._handlers[key]` forever:

```python
async def _set(self, key, value, ttl=None, ...):
    if key in self._handlers:
        self._handlers[key].cancel()      # cancels, does NOT pop
    self._cache[key] = value
    if ttl:                               # only this branch touches _handlers
        self._handlers[key] = loop.call_later(ttl, self.__delete, key)
    # no-ttl path: the cancelled handle is orphaned in _handlers[key]
```

`__delete` (the TTL callback) and `delete()`/`clear()` DO pop both dicts
correctly — so the leak is specifically the **`set(ttl)` → `set(no ttl)`
overwrite while the key stays live** path.

## Measurement (Rule 9 — reproduced before theorising)

| Probe | Result |
|---|---|
| P1: stale handle after ttl→no-ttl overwrite | **cancelled handle retained in `_handlers['k']` = True** |
| P2: 2000 distinct keys, each ttl→no-ttl | `_cache=2000`, `_handlers=2000`, **all 2000 handles cancelled (dead weight)** |

Teeth PASS (decisive): the probe reports **False** for the legit
`set(ttl)`→`set(ttl)` replace path (handle is live, correctly replaced) and
**True** only for the leak path. So it discriminates a real leak from correct
replacement — not a false positive.

## Scope / honesty about severity

- **Not strictly unbounded.** `delete(key)` and `clear()` reclaim the handle;
  a later `set(key, ttl=...)` overwrites it. The leak pins one dead
  `TimerHandle` per key **for as long as that key stays live** after a
  ttl→no-ttl overwrite. Normal `get()` never reclaims it.
- For a long-lived process caching many keys under a "set with ttl, later
  refresh without ttl" pattern, `_handlers` accumulates one dead handle per
  such key — real retained memory, invisible to any functional test (values
  in `_cache` are 100% correct; only the shadow `_handlers` dict grows).
- This is exactly the discovery-strategy target shape: **green tests, state
  accumulates over repeated ops, failure is memory not correctness.**

## Novelty (checked, not assumed)

- GitHub issue/PR search across several query phrasings: **0 hits** for this
  leak. The one related closed issue (#807) is a *different* leak in
  `_redlock_release`.
- **Current default-branch `_set` still has the identical pattern** (fetched
  from HEAD): cancels-without-pop, `_handlers` written only under `if ttl`.
  So the bug is present on stock 0.12.3 AND unfixed upstream. (HEAD adds an
  LRU `_evict_if_needed` feature absent in 0.12.3 — a separate surface worth
  probing, but the criteria require stock current stable = 0.12.3.)

## Eval-task suitability assessment

Promising, but must be validated for the reviewer bar before packaging:

**For it:**
- Real, not documented-away. Reproduces deterministically on stock 0.12.3.
- The symptom (memory grows, all tests green) is far from the cause (a
  missing `pop` on one branch of `_set`) — the localization work is real.
- Objective grade: after a fix, `_handlers` size must equal live-ttl-key
  count (0 in the ttl→no-ttl scenario), memory flat over N cycles.

**Open risks (must test before shipping):**
1. **Is it too easy?** The fix is a one-liner (`self._handlers.pop(key, None)`
   on the no-ttl path). Need to check whether a frontier model, given only
   the SYMPTOM (memory growth), localizes it in ~minutes or ~an hour. The
   anyio task failed exactly here — right-layer one-liner solved fast.
2. **Does it differentiate two models?** The whole point. Must run A/B like
   the anyio task. A one-line fix in a 100-line file may not separate models
   any better than anyio did.
3. Symptom-only prompt must not name `_handlers`/TimerHandle/TTL.

## Validation result (2026-08-02) — task BUILT, differentiation UNPROVEN

Built the full eval task (`eval-tasks/aiocache-ttl-leak/`): symptom-only
prompt (giveaway-guarded — bans `_handlers`, `TimerHandle`, `call_later`,
`cancel`, `pop`, `SimpleMemoryBackend`, …), `minimal_repro.py` (real
set-ttl→refresh-no-ttl workload, +57 MB over 60k iters on a fresh clean-room
venv), prose `grading-criteria.md` (5 axes), `make-eval-dirs.sh` clean-room
builder, and `scripts/grade_aiocache_evals.sh`.

**Grader validated on the leak:** unpatched clean-room dirs both report
`HANDLERS=40000` (leaked). The objective un-foolable signal is the shadow
`_handlers` dict size, which a real fix drives to ~0. An `EXPIRE` check catches
a TTL-disabling cheat; a value check catches data corruption; a `REFRESH_NS`
per-op timing was added for the throughput axis.

**But it did NOT demonstrate model differentiation** — same failure mode as
anyio. I simulated the two most plausible fixes:
- A = correct one-liner (`self._handlers.pop(key).cancel()`).
- B = over-eager cache-wide `_handlers` rebuild on the no-ttl set (the shape
  `grading-criteria.md` says should fail the throughput axis).

Both passed every objective gate: `REPRO=PASS EXPIRE=PASS HANDLERS=0`. The
grade separated them only on **diff size** (A +1/-1 vs B +6/-0) and marginally
on `REFRESH_NS` (12148 vs 13937 ns — ~15%, within noise). Honest reason: the
ttl→no-ttl workload leaves few *live* ttl-handles, so B's O(keyset) rebuild
never actually scans a large dict — my "bad" fix wasn't bad enough to trip the
throughput gate. Tuning the grader further to manufacture a gap would be
gaming my own eval, not measuring the models.

### Verdict: same class of outcome as anyio
This IS a real, novel, unfixed leak on a ≥1000★ library — a better *bug* than
anyio (novel + unfixed upstream, vs. anyio's known-shape). But as an
eval-*task* it has the same weakness: **the fix is a one-liner both frontier
models will likely find and apply correctly**, so it is unlikely to separate
them on any grading criterion. The differentiation must come from a REAL
Bonsai-CLI A/B run (no aiocache model outputs exist in ~/eval-outputs yet);
the simulated fixes suggest the ceiling is low.

## Status: HUNT HIT — candidate found, differentiation UNPROVEN (likely low)

Per stop-at-first-bug, the sweep pauses here. Next step is NOT more hunting —
it is to **validate this candidate against the reviewer bar** (build a
symptom-only repro + A/B the two models), the same pipeline that graded anyio.
Only if it proves too-easy/non-differentiating do we resume the sweep at
piccolo (1,934★).

## Sweep scoreboard

| # | Target | Stars | Result |
|---|---|---:|---|
| 1 | odmantic | 1,174 | skipped (MongoDB) |
| 2 | **aiocache** | **1,435** | **REAL leak — candidate (this doc)** |
| 3 | ormar | 1,804 | dry (documented) |
| — | fastapi/sqlalchemy/starlette | — | dry/correct (prior hunts) |
| — | anyio | — | real but too easy |
