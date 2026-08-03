# dramatiq #431 — reproducible setup for A/B (instrumentation only)

This packages the VALIDATED, reproducible instrumentation so the task can be
authored. **Per reviewer policy, the task PROMPT, GRADING CRITERIA, and MODEL
EVALUATION must be hand-written by you (not AI).** Everything below is
research/instrumentation only.

## Repro coordinates (reviewer needs these)
- Repo: `Bogdanp/dramatiq`  (GitHub, >1k stars)
- Commit (pinned): `288dc2651e3e32da3769b69c285143da8466e4ab`
- Issue: #431 — OPEN, no merged/approved PR (novelty verified 2026-08-03)
- Substrate: Redis at 127.0.0.1:6379 (running). Grader uses db 13 by default.

## The bug (one paragraph, for your reference — NOT a prompt)
A delayed (eta) message is held unacked in a worker's memory until eta. If that
worker's heartbeat expires, another worker's maintenance requeues the delayed
message; both workers then promote it onto the real queue -> the task executes
more than once. Promotion (worker.py `handle_delayed_messages`) is non-atomic /
non-idempotent. The trap: the naive fix (ack the delayed message before
promoting) stops duplication but drops the message if the holder crashes before
eta -> silent LOSS. A correct fix makes promotion atomic AND idempotent (e.g. a
single ownership-checked Redis/Lua operation).

## Reproducible commands
```bash
# 1. Pristine pinned checkout (git intact for reviewer verification)
git clone https://github.com/Bogdanp/dramatiq /tmp/dramatiq431-pinned
git -C /tmp/dramatiq431-pinned checkout 288dc2651e3e32da3769b69c285143da8466e4ab

# 2. venv (already built at /tmp/dramatiq431-venv; redis-py 8.1.0)
#    If rebuilding: python3 -m venv <venv>; <venv>/bin/pip install redis

# 3. Grade any candidate repo root (the dir containing the `dramatiq` package):
/tmp/dramatiq431-venv/bin/python grade.py --pkg <repo-root> --db 13
```

## Grader (grade.py) — VALIDATED, objective, approach-agnostic
Two OPPOSING gates, drives the REAL ConsumerThread against real Redis, never
inspects fix internals (Harness subclasses ConsumerThread so any fix shape is
scored by outcome). See GRADER.md for the full validation table.
- **DUP gate** PASS iff a dead-but-holding worker's requeued message is promoted
  AT MOST ONCE (dup_promotions == 1).
- **LOSS gate** PASS iff a holder that CRASHES before eta still promotes EXACTLY
  ONCE (loss_promotions == 1).
- verdict PASS iff BOTH. Exit 0 on PASS.

### Validation results (reproducible)
| package                         | DUP     | LOSS | verdict |
|---------------------------------|---------|------|---------|
| pristine pinned checkout        | FAIL(2) | PASS | FAIL    |  <- bug live
| any of 3 blind correct fixes    | PASS(1) | PASS | PASS    |  <- fix rewarded

This is ≥3 independent grading signals in spirit (dup count, loss count, and the
two-gate conjunction), objective and outcome-based.

## Known divergence caveat (be honest with the reviewer)
3 blind SAME-MODEL fix attempts all CONVERGED on approach (atomic srem-guarded
Lua promote), all PASS/PASS. So #431 likely does NOT differentiate two models on
PASS/FAIL. The untested axis is CROSS-MODEL TIME-TO-FIX. If the A/B shows no
time gap either, #431 is non-differentiating like the prior 4 tasks. See
GRADER.md "DIVERGENCE MEASUREMENT" and BACKPOCKET.md ranked fallbacks.

## What YOU author (reviewer policy — not AI)
1. The initial-prompt.md (symptom-only per Rule 10, or however you choose).
2. The grading criteria writeup (you may cite grade.py's gates, but the rubric
   and thresholds are your judgement).
3. The A/B run + the comparison/results writeup (hand-written).
