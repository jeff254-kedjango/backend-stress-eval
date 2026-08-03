# dramatiq delayed-dup-loss (issue #431) — run results

Per-task run log. **Time-to-fix and turns are first-class metrics** (Rule 10).

## Protocol note

- **prompt-only** (standard): the model gets ONLY `initial-prompt.md` + the pinned
  `src/` (git history stripped) + a `.venv`. No repro, no grader, no issue number.
- One symptom (a delayed job runs twice **or** never), two opposing gates:
  - **DUP_gate** — a delayed job whose silent-but-alive holder is declared dead and
    requeued by maintenance must promote **at most once** (`dup_promotions == 1`).
  - **LOSS_gate** — a delayed job whose holder actually crashes before eta must still
    promote **exactly once** (`loss_promotions == 1`).
  The obvious fix (ack-before-promote, or stop requeueing) passes DUP but fails LOSS.
  Only an atomic + idempotent promotion passes BOTH.

## Runs

| date | model | protocol | time (min) | turns | DUP_gate | LOSS_gate | verdict | code diff (vs pinned `288dc26`) |
|------|-------|----------|-----------|-------|----------|-----------|---------|----------------------------------|
| 2026-08-03 | A | prompt-only | **40.85 (40m51s)** | n/r | PASS (dup=1) | PASS (loss=1) | **PASS** | 3 files, **+104 / −2**: redis.py +51/−0, dispatch.lua +31/−0, worker.py +22/−2 |
| 2026-08-03 | B | prompt-only | **20.0 (20m00s)** | n/r | PASS (dup=1) | PASS (loss=1) | **PASS** | 4 files, **+174 / −4**: redis.py +85/−1, broker.py +40/−0, dispatch.lua +26/−0, worker.py +23/−3 |

Grader: `investigations/dramatiq-431-delayed-dup/grade.py` (structure-agnostic;
drives the real `ConsumerThread` methods against real Redis, counts promotions on
the real queue). Both results **stable across 3 grader runs each** (A on db 14, B
on db 15). Turn counts were not recorded during the runs (`n/r`).

> **Both PASS both gates — pass/fail did NOT differentiate.** As HUNT-STATE
> predicted for this banked fallback, the discriminating axis is **not** pass/fail
> and **not** approach. Both models converged on the *same core fix*: an atomic
> **SREM-guarded Lua promote**, where the delay-queue ack-set entry is the single
> ownership token and the enqueue only fires when `SREM == 1`, making promotion
> exactly-once under maintenance-driven re-fetch. A names the Lua branch `schedule`;
> B names it `enqueue_from_delayed` — cosmetically different, mechanically identical.
>
> **The one real signal this run: time-to-fix. A took 40m51s; B took 20m00s — a
> ~2× gap for the same passing, same-approach result.** This is the exact axis the
> #431 fallback was banked to test (same-model A/B had already been shown to
> converge on approach 3/3; the open question was cross-run/cross-model
> *time-to-fix*). Here it separated cleanly by 2×.
>
> **Diff size runs opposite to time.** The faster run (B, 20m) shipped the *larger*
> patch (+174/−4 across 4 files, adding a `broker.py` hook) while the slower run
> (A, 40m) shipped a tighter one (+104/−2 across 3 files). So bigger diff ≠ slower;
> the time gap reflects search/diagnosis effort, not lines emitted.

## Divergence summary (what this task did and did not separate)

| axis | A | B | separated? |
|------|---|---|-----------|
| pass/fail (both gates) | PASS | PASS | ✗ (converged) |
| approach (Lua SREM-guarded atomic promote) | `schedule` branch | `enqueue_from_delayed` branch | ✗ (same mechanism) |
| **time-to-fix** | **40m51s** | **20m00s** | ✓ **~2×** |
| diff footprint | 3 files, +104/−2 | 4 files, +174/−4 | ✓ (B larger, yet faster) |

Consistent with the standing finding ([[divergence-needs-diagnosis-ambiguity]]):
#431 has an unambiguous diagnosis, so strong models converge on the canonical
atomic-promote fix. Divergence surfaces only on **time-to-fix**, not approach.

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
`288dc2651e`), excluding `*.pyc`/`__pycache__`/`*.egg-info`.

> **Note on saved outputs:** the live A/B working dirs (`~/dramatiq-eval-task-A/-B`)
> were removed after the run, but each model's solution was preserved under
> `~/eval-outputs*/dramatiq-model-{A,B}-2026-08-03/` (own `src/` + `.venv`). The
> `.venv` editable link to the old path is broken, but the grader loads the package
> directly via `--pkg`, so grading needs only `redis` (present, 8.1.0) — not the
> editable install.
