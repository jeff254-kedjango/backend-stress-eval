# anyio lifecycle-leak v2 — run results

Per-task run log. **Time-to-fix and turns are first-class metrics** — two
models can reach the same grade with very different effort, and that gap is a
differentiation signal the pass/fail gate misses.

## Protocol note

- **prompt-only** (current standard): the model gets ONLY `initial-prompt.md`.
  No reproducer. The model must localize the leak itself.
- **assisted** (deprecated): earlier runs shipped `minimal_repro.py`, which
  pre-localizes the leak. Assisted times are **not comparable** to prompt-only.

Fill `time` and `turns` in by hand from each run.

## Runs

| date | model | protocol | time (min) | turns | verdict (LEAK/RUN/LOOPS) | notes |
|------|-------|----------|-----------|-------|--------------------------|-------|
| — | A | prompt-only | — | — | — | pending re-run under prompt-only |
| — | B | prompt-only | — | — | — | pending re-run under prompt-only |

> Prior runs: frontier models fixed this leak fully — differing only in fix
> elegance, which the gate doesn't score. Re-run prompt-only and record
> time-to-fix; that's where the differentiation should now show.

## Known harness notes

- **Grader rewritten to be repro-independent.** `scripts/grade_evals.sh`
  previously ran the model's `minimal_repro.py` as its gate — which breaks under
  prompt-only. It now runs its OWN inline workload (200 `anyio.run()` calls) and
  reads retained event-loop count: LEAK=PASS iff <=5 loops survive.
- `make-eval-dirs.sh` confirms the leak is live via an inline loop-count probe
  (~60 loops retained on stock 4.14.2).
