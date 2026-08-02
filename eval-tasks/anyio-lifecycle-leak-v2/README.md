# anyio lifecycle leak — eval task (v2)

**Framework:** [anyio](https://github.com/agronholm/anyio) 4.14.2 (stock, current stable)
**Language:** Python 3.12
**Task type:** find-and-fix (backend memory bug)
**Estimated model time:** 1–2 h

## The task, in golden-standard format

- `initial-prompt.md` — the prompt given to the model. Symptom + goal only:
  no file names, no scaffolding instructions, no hint at the root cause. It
  reads like a real "our CI is leaking memory" bug report.
- `grading-criteria.md` — prose outcome expectations a human reviewer checks
  by using the result (leak stays flat over a long run, fix at the right
  layer, no throughput regression, reproduces on stock current stable). No
  code, no numeric thresholds.
- `minimal_repro.py` — the standalone reproducer that sits in the model's
  working dir. Ten lines, one dependency; running it exhibits the steady
  per-iteration growth.

That is the whole task. The reviewer's whiteboard example is the template:
describe the problem and the desired outcome, and let a human grade the
result against prose criteria.

## Why v2 replaces v1

v1 (`../anyio-lifecycle-leak/`) was solved by frontier models in ~10 minutes.
Two reasons, both fixed here:

1. **The v1 prompt gave away the diagnosis** — it named "event loop, worker
   pool, internal loop state, across loop boundaries, the async runtime
   library." v2's prompt is symptom-only, so the model must localize the leak
   itself.
2. **v1 pinned a vendored anyio 3.7.1 tree** where the leak is a single
   greppable line, and told the model "work in this dir only" — which made the
   "reproduce on stock current stable release" criterion impossible to meet.
   v2 targets stock anyio 4.14.2, where the retained bytes spread across
   several files, a genuinely harder localization.

## Running it (clean-room isolation)

The model must not see anything that reveals the fix. `make-eval-dirs.sh`
builds an isolated working dir per model containing only the prompt + the
reproducer + an anyio 4.14.2 venv to patch, and refuses to build if the
prompt still contains a v1 giveaway phrase:

```bash
./make-eval-dirs.sh A B        # builds ~/anyio-eval-task-A and -B
```

Then hide this harness repo, run the model inside each dir, and archive the
result. Grade each output by hand against `grading-criteria.md`.
