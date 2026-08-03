# dramatiq #431 — objective grader (VALIDATED)

Bug: delayed (eta) messages duplicated across workers. A worker holds a delayed
message unacked in memory awaiting eta; if its heartbeat expires, another
worker's maintenance requeues it; both then promote -> executed >1x. Non-atomic,
non-idempotent promotion (worker.py handle_delayed_messages).

Repo Bogdanp/dramatiq @ 288dc2651e. Issue OPEN, no merged/approved PR (verified).

## Grader = grade.py  (objective, approach-agnostic, two OPPOSING gates)
Drives the REAL ConsumerThread methods against real Redis; counts promotions on
the real queue. Never inspects fix internals. `--pkg <repo-root>` selects which
dramatiq to grade.
  DUP  gate: dead-but-holding worker requeued -> promoted AT MOST ONCE (==1).
  LOSS gate: holding worker CRASHES before eta -> still promoted EXACTLY ONCE.
The trap: ack-at-fetch / stop-requeue fixes pass DUP but FAIL LOSS; only an
atomic+idempotent promotion passes BOTH.

## Validation (against a CLEAN baseline worktree at 288dc265)
| package                | DUP  | LOSS | verdict |
|------------------------|------|------|---------|
| clean baseline         | FAIL(2) | PASS | FAIL  |  <- bug live, grader detects
| probe fix (atomic Lua) | PASS(1) | PASS | PASS  |  <- correct fix rewarded
Grader cleanly separates unpatched from fixed. Gates are independent.

## Note (honesty)
- The FIRST "baseline" (verify clone /tmp/verify-Bogdanp-dramatiq-431) was
  CONTAMINATED — the verify agent had applied its fix in place (git status:
  broker.py/redis.py/dispatch.lua/worker.py modified). Rebuilt a clean worktree
  at the pinned SHA before grading. Always grade against a pristine checkout.
- Did NOT hand-craft a naive-fix variant that isolates LOSS=FAIL (that is
  fix-authoring, not grader work; the two gates run independently so any
  regression of either shows). Grader validity does not depend on it.

## Still OPEN (the real question): model DIVERGENCE
Blind probe fully fixed #431 in ~15min -> divergence between two models is
UNPROVEN. That is the reviewer's key bar and what killed the 4 prior tasks.
Next: measure whether two independent fix attempts diverge in grader outcome.

## DIVERGENCE MEASUREMENT (2026-08-03) — 3 blind independent attempts
Symptom-only prompts, pristine checkouts, no git history, independent Redis dbs.
Graded through grade.py (after making Harness subclass ConsumerThread so it's
structure-agnostic — attempt 3 added a new promote method that the old hardcoded
Harness couldn't see).

| attempt | approach                                  | DUP  | LOSS | verdict |
|---------|-------------------------------------------|------|------|---------|
| 1       | atomic Lua `promote`, srem-guarded        | PASS | PASS | PASS |
| 2       | atomic Lua `stage_delayed_message`        | PASS | PASS | PASS |
| 3       | atomic Lua `promote` + new CT method      | PASS | PASS | PASS |

RESULT: **CONVERGENT.** All 3 independent attempts found the SAME correct
solution shape — atomic, ownership-checked (srem-guarded) promotion in a single
Lua script — and all avoided the dup-vs-loss trap. Naming/structure differed
cosmetically; grader outcome identical (PASS/PASS).

IMPLICATION: like the prior 4 shipped tasks, #431 does NOT differentiate on
pass/fail — a competent solver reliably lands the correct fix. The dup-vs-loss
"trap" is real but a strong model sees it. Same failure mode as arq #402 / the
6/10 demotions. Caveat: 3 same-model runs != 2 different frontier models, but
convergence this clean is a strong negative signal for divergence.
