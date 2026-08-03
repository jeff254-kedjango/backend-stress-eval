# dramatiq #431 A/B — final benchmark report (2026-08-03)

Full session report for the delayed-dup-loss (dramatiq issue #431) A/B run. The
metrics table lives in `results.md`; this file is the durable narrative — how the
numbers were obtained, what separated, and the caveats — so nothing lives only in
a chat session.

## What was run

The banked fallback #1 (dramatiq #431, "a delayed job runs twice **or** never")
was run A/B, prompt-only: each model got only `initial-prompt.md` + the pinned
`src/` (git history stripped) + a `.venv`. No repro, no grader, no issue number.
User recorded wall-clock times live; everything else was reconstructed this
session.

## Final metrics

| metric | Model A | Model B |
|---|---|---|
| Wall-clock time-to-fix | **40m51s** | **20m00s** |
| Turn counts | not recorded (`n/r`) | not recorded (`n/r`) |
| DUP_gate | PASS (dup_promotions=1) | PASS (dup_promotions=1) |
| LOSS_gate | PASS (loss_promotions=1) | PASS (loss_promotions=1) |
| Verdict | **PASS** | **PASS** |
| Code diff vs pinned `288dc26` | 3 files, +104/−2 | 4 files, +174/−4 |

Per-file diff:
- **A:** `brokers/redis.py` +51/−0 · `brokers/redis/dispatch.lua` +31/−0 · `worker.py` +22/−2
- **B:** `brokers/redis.py` +85/−1 · `broker.py` +40/−0 · `brokers/redis/dispatch.lua` +26/−0 · `worker.py` +23/−3

Grading was **stable across 3 grader runs each** (A on Redis db 14, B on db 15).

## The finding

- **Pass/fail did NOT differentiate** — both PASS both gates.
- **Approach did NOT differentiate** — both converged on the *same* core fix: an
  atomic **SREM-guarded Lua promote** (delay-queue ack-set entry = the single
  ownership token; the canonical enqueue fires only when `SREM == 1`, so promotion
  is exactly-once under maintenance-driven re-fetch). A names the Lua branch
  `schedule`; B names it `enqueue_from_delayed` — mechanically identical. Exactly
  what HUNT-STATE predicted for this unambiguous-diagnosis bug.
- **Time-to-fix DID differentiate — ~2× (A 40m51s vs B 20m00s).** This is the one
  axis the #431 fallback was banked to test (same-model A/B had already been shown
  to converge on approach 3/3; the open question was time-to-fix). It separated
  cleanly at ~2×.
- **Diff size ran opposite to time:** the faster run (B, 20m) shipped the *larger*
  patch (+174/−4, adding a `broker.py` hook) while the slower run (A, 40m) shipped
  a tighter one (+104/−2). Bigger diff ≠ slower; the gap reflects diagnosis/search
  effort, not lines emitted.

## Two caveats

1. **Turn counts are permanently lost** — they were never recorded during the runs
   and the sessions are gone, so that metric stays `n/r`. Record turns live in the
   next A/B.
2. The live A/B working dirs (`~/dramatiq-eval-task-A/-B`) were deleted after the
   run. Each model's solution was preserved under
   `~/eval-outputs*/dramatiq-model-{A,B}-2026-08-03/` (own `src/` + `.venv`). The
   `.venv` editable link is broken (points at the deleted path), but the grader
   loads the package directly via `--pkg`, so grading needs only `redis`
   (present, 8.1.0) — not the editable install.

## Reproduce these numbers

```bash
GRADER=~/backend-stress-eval/investigations/dramatiq-431-delayed-dup/grade.py
# Model A
~/eval-outputs.previous/dramatiq-model-A-2026-08-03/.venv/bin/python "$GRADER" \
  --pkg ~/eval-outputs.previous/dramatiq-model-A-2026-08-03/src --db 14
# Model B
~/eval-outputs/dramatiq-model-B-2026-08-03/.venv/bin/python "$GRADER" \
  --pkg ~/eval-outputs/dramatiq-model-B-2026-08-03/src --db 15
```
Both print `"verdict": "PASS"` (exit 0). Diff counts are `diff -ru` of each
model's `src/dramatiq` vs `/tmp/dramatiq431-pinned/dramatiq` (pinned SHA
`288dc2651e`), excluding `*.pyc` / `__pycache__` / `*.egg-info`.

## Where the model solutions live (durable copies)

- **Model A:** `~/eval-outputs.previous/dramatiq-model-A-2026-08-03/`
  (outer `src/` and nested `dramatiq-eval-task-A/src/` are byte-identical)
- **Model B:** `~/eval-outputs/dramatiq-model-B-2026-08-03/`

> These are OUTSIDE the git repo (in `~/eval-outputs*`). They are the only surviving
> copies of the patched sources — do not delete them without archiving first.

## Cross-references

- Metrics table: `results.md` (same dir)
- Grader + validation: `investigations/dramatiq-431-delayed-dup/`
- Divergence principle: memory `divergence-needs-diagnosis-ambiguity`
- This result in memory: `dramatiq-431-time-to-fix-diverged`
