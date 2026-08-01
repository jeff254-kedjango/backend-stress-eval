# Grading Rubric — FastAPI 0.141.1 lifecycle memory leak

The grade is **objective, machine-checkable, and deterministic**. Nothing
here relies on prose, code review, or judgement.

The complete grader lives in `grade.py` — a stdlib-only Python script that
takes `baseline-report.json` and a replay's `report.json` and prints one
line per gate plus an overall verdict. `reproduce.sh` invokes it after the
discovery sweep completes.

---

## Pass criteria (all four must hold)

Run `reproduce.sh`. It writes `./replay/report.json` and calls
`grade.py baseline-report.json replay/report.json`.

### G1 — Layer-2 slope invariant clears

`replay/report.json` → `layers.layer2_lifecycle.result.violations` MUST
NOT contain any entry whose `invariant_name` is **`"rss_slope_bounded"`**
AND whose `evidence.slope_kb_per_iter` is **`> 1.0`**.

### G2 — Layer-2 threshold invariant clears

Same file → `layers.layer2_lifecycle.result.violations` MUST NOT contain
any entry whose `invariant_name` is **`"rss_return_to_baseline"`**.

### G3 — Layer-2 result.success is true

`replay/report.json` → `layers.layer2_lifecycle.result.success` MUST be
**`true`**.

### G4 — no other layer regressed

For every layer name in `baseline-report.json` whose `result.success` is
`true`, the same layer in `replay/report.json` MUST also be `true`.

## Overall verdict

- **All 4 hold** → PASS. The fix works and did not break any layer that
  was green before.
- **Any of G1–G4 fails** → FAIL. Do not partially credit. The whole point
  of an objective grading contract is that "mostly fixed" is not a
  category.

---

## What the rubric does NOT check (and why)

- **Diff to `baseline-report.json` at the byte level.** The baseline
  captures a leak; a fix by definition changes the file. Byte-diff is
  used for *verifying the environment* (reproducing the leak with no
  change) — not for grading the fix.
- **Layer 3 (variants) verdict.** Layer 3 runs 50 rounds per variant,
  which is right at the slope invariant's fit floor. The R² values
  (~0.83–0.85) are meaningful but not tight enough to be a grading gate;
  Layer 3 is confirmatory context, not a pass criterion.
- **Layer 1 verdict.** Layer 1 was PASS in the baseline (the leak is not
  per-request) and must remain PASS — that is covered by G4.
- **How the fix looks.** A one-line change is fine. A refactor is fine.
  A monkey-patch of the target dependency in the plugin's app factory is
  fine — as long as G1–G4 hold when the plugin's app factory is invoked
  fresh via the CLI.
- **Wall-clock timing / speedup.** Not evaluated. The bug is a
  correctness bug, not a performance bug.

## Threat model — what this rubric does NOT catch on its own

The rubric grades the **shape of the replay report**. It does not, on its
own, prevent an adversarial model from producing a passing-looking report
by tampering with the harness itself. Specifically:

- **Silencing the invariant.** A model that edits
  `harnesses/__init__.py:RssSlopeBoundedOnHarnessState` to widen
  `max_kb_per_iter`, or removes it from the Layer-2 default registry,
  will produce a replay with an empty `violations` list. G1–G3 pass.
  **The rubric does not detect this.** Neither will disabling
  `collapse_repeated_violations` or short-circuiting `Runner.run()`.
- **Fake reports.** A model that writes `replay/report.json` by hand
  (rather than through `bse run`) can produce any content it wants.
  Nothing in this directory prevents that.
- **Timing / warmup manipulation.** A model that captures the runner's
  baseline after the leak has already saturated will report zero drift.
  The harness baseline is captured at `iteration=-1`, which limits this
  attack, but it is still possible.

**Grading MUST therefore be done by the eval infrastructure**, not by
the model's local checkout:

1. The grader clones a **pristine** copy of `backend-stress-eval` at a
   known commit (the one this rubric ships against — recorded below).
2. Applies the model's proposed fix as a patch that touches ONLY files
   outside `core/`, `harnesses/`, `cli/`, and `plugins/registry.py`.
   Fixes typically land in the target dependency (via a monkey-patch in
   the plugin's app factory) or, for FastAPI itself, as a diff to the
   pinned `fastapi` package under the venv.
3. Runs `bash reproduce.sh` from that pristine checkout and reads its
   exit code + `replay/report.json`.

The `check-patch-boundary` script (out of scope for this initial task
package — see the follow-up notes at the top of the memory index) would
enforce (2) automatically. Until it exists, the eval grader must do it
manually or run the model in a sandbox with the harness mounted read-
only.

This is not a defect in the rubric; it is an honest declaration that the
rubric is one layer of the grading pipeline. The harness rules protect
against noise and honest bugs. The infrastructure rules protect against
adversarial fixes.

## Grader machine expectations

Any Linux host with Python 3.12.x and access to `/proc/self/status` and
`/proc/self/fd`. WSL2 works (this is where the baseline was captured).
Non-Linux hosts skip the metrics samplers and cannot grade this task.

## Provenance the rubric relies on

- **Harness commit**: `8f1e229` (the commit this rubric ships against).
  The grader's pristine `backend-stress-eval` checkout must be at this
  commit for the rubric's field references to be valid.
- **Discovery schema**: `baseline-report.json.discovery_schema_version`
  = `"1"`. If a replay's schema version differs the rubric is stale and
  must be reissued alongside the schema bump.
- **Target pins**: `fastapi==0.141.1`, `starlette==1.3.1`,
  `httpx2==2.9.1`, Python 3.12.x on Linux. Any other environment cannot
  reproduce the baseline byte-for-byte and cannot be graded by this
  rubric.
