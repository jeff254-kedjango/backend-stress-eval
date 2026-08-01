# SHELVED — do not submit

This eval task is **shelved as a documented negative result**. Do NOT run
`reproduce.sh` expecting a submission-quality grade, and do NOT paste
its README to a frontier-model eval.

**Why shelved.** Chunk 6b tracemalloc attribution (`attribution.md` in
this directory, commit `356abb7`) proved the +9.60 KB/iter residual
lifecycle leak attributes to the exact code region PR
[#16049](https://github.com/fastapi/fastapi/pull/16049) — merged
2026-07-24, released in FastAPI 0.140.0 — already refactored for a
~16× per-entry memory reduction. Our pin (0.141.1) contains that fix;
the residual is the shrunken tail of the same class of bug, not a
distinct one.

**Novelty verdict:** the eval-task submission spec requires "the fix
shouldn't already exist upstream or online." This one does. A reviewer
finds PR #16049 in a single search, and the task grades nothing.

**What's preserved and why.** Everything that was in this directory
before shelving is kept, in place, so the audit trail is complete:

- `README.md`, `RUBRIC.md` — the original task framing. Read to
  understand *how* we mis-scoped it.
- `baseline-report.json`, `baseline-summary.txt` — the L2 discovery
  run that surfaced the leak.
- `attribute.py`, `attribution.md` — the 6b post-mortem that killed
  novelty. `attribute.py` still runs; it just doesn't imply a
  submittable task on its own anymore.
- `grade.py`, `reproduce.sh` — the grading contract. `grade.py` now
  fail-loudly refuses to grade when invoked from `_shelved/` (Rule 5).

**How this is used going forward.**

1. As documentation of what "novelty check first" is supposed to catch.
   The mistake was: baseline & rubric & grader shipped *before* novelty
   was checked.
2. As a template — the harness, plugin manifest, `attribute.py` shape,
   and `grade.py` structure all remain valid. The next eval task
   (Chunk 7b) can copy those bones.
3. Anyone auditing the repo's eval-task history sees the negative
   result recorded honestly, not swept away.

**How to find the next one.** See Chunk 7b — rediscovery sweep across
older FastAPI pins or a different framework, with novelty check DONE
BEFORE baseline commit.
