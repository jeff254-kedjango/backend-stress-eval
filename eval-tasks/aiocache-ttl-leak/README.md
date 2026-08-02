# aiocache TTL-handler leak — eval task

**Framework:** [aiocache](https://github.com/aio-libs/aiocache) 0.12.3 (stock, current stable)
**Language:** Python 3.12
**Task type:** find-and-fix (backend memory bug)
**Estimated model time:** 1–2 h
**Discovered by:** the ascending-maturity sweep — see
`investigations/aiocache-ttl-leak/findings.md`. aiocache = 1,435 GitHub stars,
above the reviewer's ≥1000★ popularity floor.

## The bug in one paragraph

`SimpleMemoryBackend._set` cancels a key's existing TTL `TimerHandle` but only
re-stores into `self._handlers` on the `if ttl:` branch. Writing a key with a
TTL and then refreshing it in place *without* a TTL (while the key stays live)
cancels the old handle but leaves it orphaned in `self._handlers[key]` forever.
Over many keys, `_handlers` accumulates one dead handle per key — real retained
memory, invisible to any functional test. Present on stock 0.12.3 AND on the
current default branch; no prior issue/PR found (novelty checked).

## The task, in golden-standard format

- `initial-prompt.md` — the prompt given to the model. Symptom only (memory
  climbs, tests green, bounded keyset). Does NOT name the cache internals,
  the handler dict, TimerHandle, expiry timers, or the refresh-without-ttl
  trigger. The model must localize the leak itself.
- `grading-criteria.md` — prose outcome expectations a human reviewer checks:
  memory flat over a long run, fix inside the backend at the source, cached
  values still correct + TTLs still expire, refresh hot path not slowed,
  reproduces/resolves on stock current stable.
- `minimal_repro.py` — the standalone reproducer in the model's working dir.
  One dependency, no framework; models a real "cache with ttl, refresh without
  ttl" workload that drives RSS up in a straight line.

## Why this task (vs. the anyio one)

The anyio lifecycle-leak task was a real leak but did not differentiate two
frontier models — both fixed it fully; they differed only in fix elegance,
which the rubric doesn't score. This task is the sweep's attempt to find a
leak that separates models on a grading criterion. Whether it actually does is
still under validation (`investigations/aiocache-ttl-leak/findings.md` §Eval-
task suitability) — the fix is a one-liner, so the open risk is the same
"too-easy" failure mode. The A/B grade is what decides it.

## Running it (clean-room isolation)

`make-eval-dirs.sh` builds an isolated working dir per model containing only
the prompt + reproducer + an aiocache 0.12.3 venv to patch, and refuses to
build if the prompt still contains a cause-revealing giveaway phrase:

```bash
./make-eval-dirs.sh A B        # builds ~/aiocache-eval-task-A and -B
```

Then hide this harness repo, run the model inside each dir, and archive the
result. Grade each output by hand against `grading-criteria.md`, and diff the
patched `aiocache/backends/memory.py` against a clean 0.12.3 baseline.
