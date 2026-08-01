# Eval Task — FastAPI 0.141.1 lifecycle memory leak

> **Discovery target**: `fastapi` **·** **target commit**: `fastapi-0.141.1`
> **·** **Discovered by**: `backend-stress-eval` Layer 2 (lifecycle) + Layer 3 (variants)
> **·** **Bug shape**: slow drip — invisible under normal tests, deterministic under repetition

---

## 1. What a maintainer sees

FastAPI's public test suite is green. `pytest` on your own project against
`fastapi==0.141.1` is green. Load-testing a single boot with 5,000 requests
looks fine — RSS is flat. There is nothing to fix.

Then someone reports: "our worker pods drift up ~30 MB every deploy
rehearsal." The rehearsal script tears down and rebuilds the app 3,000 times
back-to-back with a smoke probe between each. You can't reproduce it from a
single boot. Under one boot with 10,000 requests — nothing. Under 3,000
rebuilds with one request each — you see it.

## 2. What the harness reports

Running `backend-stress-eval`'s discovery sweep against
`fastapi==0.141.1` + `starlette==1.3.1` + `httpx2==2.9.1` produces the
frozen `baseline-report.json` next to this file. The layer-2 verdict:

```
=== layer2_lifecycle ===
[fastapi@fastapi-0.141.1] FAIL iterations=500/500 invariants=4 violations=2
  - iter=64  rss_return_to_baseline: RSS drifted +1152 KB above baseline (slack 1024 KB)
  - iter=499 rss_slope_bounded:      RSS grows +8.7222 KB/iter (limit 1.0);
                                     linear fit R²=0.9856 over 500 samples
```

Two rows. Two views of one bug:

- **`rss_return_to_baseline`** — a threshold invariant. It fires at
  iteration 64, when the accumulated drift first exceeds 1024 KB. Its
  ``evidence.collapsed`` block shows the SAME invariant fired on 436
  consecutive later iterations too. **Whatever this is, it never stops.**

- **`rss_slope_bounded`** — a slope invariant. Over the full 500-sample
  RSS trajectory, ordinary least-squares regression gives slope
  **+8.7 KB/iteration** with **R² = 0.9856**. That R² means the growth
  is essentially a straight line, not noise. **Every rebuild cycle adds
  about 8.7 KB that is never released.**

Layer 1 (500 requests against one long-lived app) is PASS: the bug is not
per-request. Layer 4 (an ordered request sequence) is PASS: the bug is not
per-endpoint. The signature is unambiguous — **the leak lives at the
lifecycle boundary**.

## 3. Your task

Find the specific Python-heap allocation that is not released by app
shutdown, propose a fix, and prove the fix by bringing
`rss_slope_bounded.evidence.slope_kb_per_iter` to `<= 1.0` in a rerun of
the same discovery sweep against the same version pin.

The fix will likely be small. Getting to the fix is the work.

## 4. Setup

```bash
# From the repo root:
python -m venv .venv
source .venv/bin/activate
pip install -e ".[fastapi]"
./check.sh                        # sanity check
bash eval-tasks/fastapi-0.141.1-lifecycle-leak/reproduce.sh
```

The last command runs the discovery sweep, writes a fresh report to
`./replay/`, and diffs the layer-2 finding against `baseline-report.json`.
See `RUBRIC.md` for what a passing grade requires.

## 5. Questions this task is trying to force

(From `discovery-strategy.md` §Final — these are exactly the questions
that separate a strong debugger from a pattern-matcher.)

- Where does this state come from? What accumulates 8.7 KB per rebuild?
- Which component owns this behaviour — FastAPI, Starlette, anyio, or
  something the user code registered?
- Why do the normal FastAPI tests pass? They build one app, not many.
  Under what test pattern would the leak be visible to the maintainer?
- What changes after repeated execution? Take the same app, snapshot
  `gc.get_objects()` types before and after N build/teardown cycles.
  Which type's count grows monotonically? Attribute it with `tracemalloc`.
- Which lifecycle assumption is incorrect? FastAPI's shutdown handler
  fires. Which registration side-effect from `build_app` does it NOT
  reverse?

## 6. What we won't tell you

Which file, which function, which line. The whole point.

If you want a starting nudge, `tracemalloc.take_snapshot()` before and
after 500 iterations of `build_app → TestClient(app).__enter__ → get('/')
→ __exit__` and `compare_to("lineno")` on the two snapshots gives a
usable stack rank of allocation sites by size delta.

## 7. Provenance

- **Discovered**: 2026-08-01 (project date), by `bse run fastapi --version 0.141.1`
  after wiring `RssSlopeBoundedOnHarnessState` into the Layer-2 default
  registry (commit `1a266cb`).
- **Confirmed reproducible**: baseline was regenerated across multiple
  independent runs; `slope_kb_per_iter` stayed within ±0.5 KB/iter and
  `r_squared` stayed >= 0.98 at 500 rounds. Layer 3's 50-round-per-variant
  slope was noisier and is not part of the grading gate for that reason.
- **Not a regression from an older FastAPI version being tested here** —
  the discovery ran against a clean pip-installed `fastapi==0.141.1`,
  nothing else patched.
