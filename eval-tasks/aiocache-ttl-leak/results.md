# aiocache TTL-leak — run results

Per-task run log. **Time-to-fix and turns are first-class metrics** — two
models can reach the same grade with very different effort, and that gap is a
differentiation signal the pass/fail gates miss.

## Protocol note

- **prompt-only** (current standard): the model gets ONLY `initial-prompt.md`.
  No reproducer. The model must localize the leak itself.
- **assisted** (deprecated): earlier runs shipped `minimal_repro.py`, which
  pre-localizes the leak. Assisted times are **not comparable** to prompt-only.

Fill `time` and `turns` in by hand from each run.

## Runs

| date | model | protocol | time (min) | turns | verdict (REPRO/EXPIRE/HANDLERS/REFRESH) | notes |
|------|-------|----------|-----------|-------|------------------------------------------|-------|
| — | A | prompt-only | — | — | — | pending re-run under prompt-only |
| — | B | prompt-only | — | — | — | pending re-run under prompt-only |

> Prior assisted A/B (with repro) both PASSed — the one-line fix wasn't
> differentiating. Re-run prompt-only and record time-to-fix.

## Known harness notes

- The grader (`scripts/grade_aiocache_evals.sh`) uses its own independent PROBE
  (handler-count + expire + refresh-throughput); it never reads a model-side
  repro, so dropping the reproducer costs the grade step nothing.
- `make-eval-dirs.sh` confirms the leak is live via an inline handler-count
  probe (~2000 handlers on stock 0.12.3).
