# L3 FastAPI interaction hunt — findings (2026-08-02)

**Target:** FastAPI 0.141.1 / Starlette 1.3.1 / anyio 4.14.2, Python 3.12
**Goal:** find a feature-*interaction* bug (Layer 3) hard enough for a 1–2h
partial-success eval — after the anyio lifecycle-leak task proved too easy
(both models fixed it in ~10 min with a byte-identical one-liner).

## What was tried

Three combination variants, each a single-route app, 50-round Layer 2
lifecycle per variant, invariants: route_registry_stable,
response_determinism, rss_slope_bounded, rss/fd_return_to_baseline.

1. `yield_dep+streaming` — yield-dependency (with teardown) feeding a
   StreamingResponse.
2. `middleware+background` — HTTP middleware wrapping an endpoint that
   schedules a Starlette BackgroundTask.
3. `nested_yield_deps` — two nested yield-dependencies (teardown ordering).

## Result — NEGATIVE

- **Zero** route-registry-drift or response-non-determinism violations across
  all three combinations. The two "silent green-test drift" invariants — the
  ones that would mark a real interaction bug — never fired.
- The only violations were `rss_slope_bounded` (the already-characterized,
  shelved anyio backend per-loop creep — background noise, not interaction).
- First run *looked* like `yield_dep+streaming` leaked ~2.5× faster, but a
  controlled isolation test (baseline / stream-only / yield-only / combined,
  80 rounds each) showed:

  | app | slope KB/iter | excess over baseline |
  |---|---:|---:|
  | baseline | 8.87 | — |
  | stream_only | 8.91 | +0.04 |
  | yield_only | 12.07 | +3.20 |
  | combined | 10.05 | +1.18 |

  Super-additive excess = **−2.06 KB/iter**. The combination leaks *less*
  than yield-only alone; the initial signal was run-to-run RSS variance, not
  an interaction. **Rejected as additive/noise.**

## Conclusion

No interaction bug found in FastAPI 0.141.1 across these three combinations
under the current invariant set. This is the third dry FastAPI hunt
(after the two shelved lifecycle tasks). Two honest next moves:

- **Widen the invariant lens.** The current invariants catch route drift,
  response drift, RSS/FD slope. They do NOT catch: teardown-ordering
  corruption of shared state, dependency-cache bleed across requests,
  exception-group swallowing in combined middleware+bg paths. A real
  interaction bug may exist but be invisible to today's checks.
- **Change target.** Per the earlier decision matrix, Starlette (thinner,
  less-picked-over) or SQLAlchemy (session/identity-map staleness — classic
  green-tests-pass interaction bugs) are more likely to yield a genuinely
  hard, novel interaction bug than a third pass at FastAPI.

Option (a) from the prior discussion — re-scope the *anyio* leak with a
symptom-only prompt + current stable release + partial-credit rubric —
remains on the table as the cheaper path to a gradeable 1–2h task.

## Step 1 — widened invariant lens (2026-08-02) — ALSO NEGATIVE

Added two probes (`widened_probes.py`) that see what route/response/RSS/FD
invariants cannot:

1. **teardown ordering** — nested yield-deps (A→B→C) + middleware; teardown
   must be strict LIFO (C,B,A), exactly once, every request.
2. **dependency-cache bleed** — per-request `Depends` cache isolation; a
   token written in request A must never be read in request B.

Result: **both green** — 600 requests each, teardown order correct on 100%,
per-request cache isolated on 100%. FastAPI 0.141.1 is correct on both
surfaces.

**Teeth verified** (this is the important part — a blind probe's green is
worthless): injected a known non-LIFO teardown and a known-bleeding reader
(`reader() -> -999`); the real `probe_cache_isolation` reported
`ok=False, 30/30 bleed`, and the teardown comparison distinguished LIFO from
non-LIFO. So these are **trustworthy negatives**, not false passes.

## Decision: step 1 failed → proceed to step 2 (SQLAlchemy)

Widening the lens on FastAPI did not surface an interaction bug. Per the
agreed fallback order (1 → 2 → 3), next is a SQLAlchemy plugin targeting
session / identity-map / expire-on-commit staleness — the classic
"unit tests green, state stale after N ops" interaction bug, on a much
less-picked-over surface.
