# Hunt state checkpoint (2026-08-03)

Durable record of WHERE THE HUNT STOPPED so we never re-search. Pairs with
BACKPOCKET.md (per-candidate ledger). Read this before resuming any hunting.

## Decision in force
Hunting is PAUSED. We fall back to our best banked option and work with it.
Resume hunting ONLY if the fallback A/B comes up null (per user 2026-08-03).

## What we proved this session
1. **dramatiq #845** — DEMOTED. Async-exc deadlock not reliably reproducible on
   stock deps (stdlib logging + loguru both try/finally release the lock).
   Banked; revisit only if a stock dep without lock try/finally appears.
2. **dramatiq #431** — grader VALIDATED (structure-agnostic, baseline FAIL/dup=2,
   fixes PASS). 3 blind attempts CONVERGED on approach (atomic srem-guarded Lua
   promote). RANKED FALLBACK #1. Files: investigations/dramatiq-431-delayed-dup/.
3. **procrastinate #1495** — repro'd on real PG, 3-gate grader VALIDATED. 2 blind
   attempts CONVERGED on approach (SQL-fn on-conflict reclaim). RANKED FALLBACK
   #2. Files: investigations/procrastinate-1495-periodic-loss/.
4. **litestar #3772** — REJECT-REPRO. Fixed at the 3.0.0b0 pin (issue filed vs
   2.12.1; the 2.12→3.0b streaming refactor made generator `finally` run on
   disconnect — verified normal + blocked-await/blpop cases).
5. **litestar #4700 / #4894** — skipped: single clear root cause (convergent);
   #4894 also has open PR #4895 (novelty dent).

## Two key learnings (drive any future hunt)
- **Divergence needs DIAGNOSIS ambiguity, not fix-space width.** Strong models,
  once they diagnose correctly, converge on the canonical fix. #431 (3/3) and
  #1495 (2/2) both had wide theoretical fix spaces yet converged. See memory
  [[divergence-needs-diagnosis-ambiguity]].
- **Round-1 harvest is biased toward CONVERGENT bugs.** It selected crisply-
  reported bugs = unambiguous root cause. Diagnosis-ambiguous bugs have
  contested/confused threads and were filtered out. Mining the list for
  divergence won't work — future hunts must search the SHAPE: contested cause,
  "not sure why", reopened issues, heisenbugs, competing theories.
- **Verify issue target-version vs the pin.** Several round-1 pins are a later
  beta where the (older-version) bug is already fixed (litestar #3772).

## Round-1 candidates NOT yet probed (if hunt resumes, start here)
On available substrates (Redis+PG; NO Mongo, NO Kafka broker):
- procrastinate #1591, #1543, #1599 (PG) — un-probed; likely convergent per bias.
- aiokafka #844/#1095/#1098/#1145 — BLOCKED (need a Kafka broker; #844 also a
  "question", repro shaky).
- strawberry #3290/#3414/#4326 (GraphQL, no external substrate) — un-probed.
- alembic #899 (PG) — un-probed.
- encode/databases #538/#570 — repo ARCHIVED (dead project); deprioritized.
NOTE: none of these are known diagnosis-ambiguous; expect convergence.

## Fallback in use — PACKAGED & READY (2026-08-03)
**dramatiq #431** (ranked #1). Instrumentation packaged in
investigations/dramatiq-431-delayed-dup/:
- grade.py — validated objective 2-gate grader (structure-agnostic).
- GRADER.md — full validation + divergence table.
- RUN.md — reproducible coordinates (repo/commit/env/commands), bug summary,
  and the split of "what AI packaged" vs "what USER must hand-write".
- Fresh pristine pinned checkout at /tmp/dramatiq431-pinned (git intact,
  SHA 288dc2651e). Grader re-confirmed on it: verdict FAIL, dup=2 (bug live).
- venv at /tmp/dramatiq431-venv (redis-py 8.1.0).

NEXT (USER action, reviewer policy): hand-write initial-prompt.md + grading
criteria, then run the cross-model A/B and hand-write the results/comparison.
Cross-model TIME-TO-FIX is the untested divergence axis (same-model = convergent).
