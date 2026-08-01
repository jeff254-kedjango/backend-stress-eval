# Grading Rubric — anyio lifecycle leak

The grade is **objective, machine-checkable, and deterministic**.
Nothing here relies on prose, code review, or judgement.

The complete grader lives in `grade.py` — a stdlib-only Python script
that takes `baseline-attribution.json` and a replay's `report.json`
and prints one line per gate plus an overall verdict. `reproduce.sh`
invokes it after `measure.py` finishes.

Four independent gates. All four must PASS for OVERALL: PASS.

---

## G1 — Slope invariant clears

`report.json` → `slope_kb_per_iter` MUST be `<= 1.0`.

The baseline runs at ~+5.21 KB/iter; a real fix drops that below the
noise floor of Python-heap allocation (measured empirically at
~0.98 KB/iter on the anyio-cut path — see
`../../investigations/7b-anyio-vs-asyncio/findings.md`).

## G2 — Total heap delta bounded

`report.json` → `total_delta_kb` MUST be `<= 500.0`.

The baseline is ~2549 KB; a real fix cuts total growth to well under
half a megabyte over 489 iterations. G2 catches a fix that reduces the
*rate* but leaves a large one-shot allocation (e.g. moving the
per-iteration allocations to a per-anyio-import allocation of similar
size).

## G3 — No blacklisted anyio backend line in top-5

`report.json` → `top_lines[:5]` MUST NOT contain any entry whose
`(file, lineno)` matches any of:

```
anyio/_backends/_asyncio.py:2481
anyio/_backends/_asyncio.py:2598
anyio/_backends/_asyncio.py:2599
anyio/_backends/_asyncio.py:2052
anyio/_backends/_asyncio.py:2053
```

These are the five lines whose allocations dominate the baseline's top
attribution (see `baseline-summary.txt`). If a fix reduces the slope
without displacing these lines from the top-5, the underlying
mechanism is unchanged and the "fix" is masking, not fixing.

**Caveat:** these line numbers are pinned to anyio 4.14.2's
`_backends/_asyncio.py`. If a fix reformats the source (moves the
allocations to different lines) but doesn't change the mechanism, G3
would pass spuriously. G4 (version match) is what guards against that
attack in the grading pipeline — a fix that changes the anyio version
would fail G4 first.

## G4 — Environment matches baseline

`report.json` must have:

- `schema_version == "1"`
- `anyio_version == baseline.anyio_version`
- `python_version == baseline.python_version`
- `span_iters >= baseline.span_iters`

Prevents "grading" a replay collected on a different anyio version
(where the leak might not exist at all) or a truncated run (where the
slope hadn't stabilised).

---

## Overall verdict

- **All 4 hold** → PASS. The fix works, is at the right layer, and was
  measured under the right conditions.
- **Any of G1–G4 fails** → FAIL. Do not partially credit. Objective
  grading contracts don't have a "mostly fixed" category.

---

## What the rubric does NOT check (and why)

- **Diff to `baseline-attribution.json` at the byte level.** The
  baseline captures the leak; a fix changes the file. Byte-diff is used
  for *verifying the environment* (running the reproducer unmodified
  produces the baseline within noise) — not for grading the fix.
- **Wall-clock timing / speedup.** Not evaluated. The bug is a
  correctness (leak) bug, not a performance bug.
- **How the fix looks.** A one-line change, a refactor of
  `AsyncIOBackend.run_sync_in_worker_thread`, or a monkey-patch applied
  in a plugin's `sitecustomize.py` are all acceptable as long as G1–G4
  hold when `reproduce.sh` runs fresh.

## Threat model — what this rubric does NOT catch on its own

The rubric grades the **shape of the replay report**. It does not, on
its own, prevent an adversarial model from producing a passing-looking
report by tampering with the measurement itself. Specifically:

- **Silencing the invariant.** A model that edits `measure.py` to skip
  the outer iteration loop, or to always report `slope_kb_per_iter=0`,
  will produce a passing replay. **The rubric does not detect this on
  its own.**
- **Fake reports.** A model that writes `replay/report.json` by hand
  (rather than through `measure.py`) can produce any content it wants.
  Nothing in this directory prevents that.
- **Warmup manipulation.** A model that captures the snapshot late
  enough that the leak has saturated will report zero further drift.
  `warmup_iter=10` and `rounds=500` are the pinned defaults, but a
  handwritten report can lie about them.

**Grading MUST therefore be done by the eval infrastructure**, not by
the model's local checkout:

1. The grader clones a **pristine** copy of `backend-stress-eval` at
   the harness commit recorded below.
2. Applies the model's proposed fix as a patch that touches ONLY files
   under `anyio/**` in the installed anyio wheel (equivalent to a
   monkey-patch or a source-level diff to anyio itself). A
   `check-patch-boundary` step (out of scope for this eval-task
   package — see the shelved task's TODO note) would enforce this.
3. Runs `bash reproduce.sh` from that pristine checkout and reads its
   exit code + `replay/report.json`.

This is not a defect in the rubric; it is an honest declaration that
the rubric is one layer of the grading pipeline. Harness rules protect
against noise and honest bugs. Infrastructure rules protect against
adversarial fixes.

## Grader machine expectations

Any Linux host with Python 3.12.13, anyio 4.14.2 installed, and
tracemalloc functional (i.e. any standard CPython 3.12). WSL2 works
(this is where the baseline was captured). Non-Linux hosts may see
different `weakref` behaviour; the rubric is not certified there.

## Provenance the rubric relies on

- **Harness commit:** whatever the current `~/backend-stress-eval`
  main tip is at grading time (recorded in the grader's environment,
  not in this file — the file is versioned with the harness).
- **anyio pin:** 4.14.2 (recorded in `baseline-attribution.json`).
- **Python pin:** 3.12.13 (recorded in `baseline-attribution.json`).
- **Baseline schema:** `baseline-attribution.json.schema_version == "1"`.
  Any bump breaks this rubric and must ship alongside a new one.
