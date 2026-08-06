# Upgrade Plan — Meeting the Reviewer's Bar

> **Status:** DESIGN DOC. Supersedes the 2026-08-05 "C+ → A" plan (kept in
> git history for reference; do not read as guidance). This version is
> authoritative for all work on this repo from 2026-08-06 forward.
>
> Read alongside [`rules.md`](./rules.md) (the canonical working rules, now
> including the three new sourcing gates) and
> [`discovery-strategy.md`](./discovery-strategy.md) (the historical "why").
>
> Anchor date: 2026-08-06.

---

## 0. Why this rewrite exists

The prior plan (2026-08-05) was written before external reviewer feedback
landed. It optimized for **divergence** — a real problem — while leaving a
deeper problem untouched: **the packaged bug narratives were not being
personally reproduced at the pinned commit.** The reviewer's own words on
dramatiq #431:

> "The bug report also describes a repository history that does not exist.
> There is no earlier patch in dramatiq that makes workers release delayed
> jobs earlier in the cycle, and at the commit you pinned a worker crash
> does not silently drop a held delayed job — the broker's dead-worker
> maintenance requeues it. Half of the symptom you describe cannot occur
> at the pinned commit, so the task cannot be run as reported. A debugging
> proposal must describe the actual behaviour of the pinned code, from a
> reproduction you performed yourself."

And the positive definition of what would pass:

> "The path back is a different proposal: a bug you have personally
> reproduced at the commit you pin, whose published record does not
> already contain the fix, complex enough to require roughly 45 minutes
> [minimum one hour per project requirement] of model work, with every
> part of the proposal written in your own words."

This rewrite exists to align the tool and the process with that bar.

---

## 1. The five failure modes, named

1. **Difficulty floor unmet.** Every shipped or banked task is solvable by
   frontier models in under one hour. The reviewer's minimum is one hour.
2. **Divergence absent.** dramatiq #431 converged 3/3; procrastinate #1495
   converged 2/2. The four-qualities gate lists divergence as required;
   no shipped task clears it.
3. **Narratives not personally reproduced.** dramatiq #431's report
   describes behaviour that does not occur at its pinned commit. The
   author read the issue thread instead of reproducing on-bench, and
   inherited the thread's mistakes.
4. **Prior "fixes" targeted the wrong problem.** The 2026-08-05 upgrade
   plan proposed a divergence probe, version-diff, concurrency-matrix,
   teardown-fuzzer, fault-injection, and contested-issue harvester. Only
   the first three of those attack a reviewer objection. The
   contested-issue harvester *worsens* the personal-reproduction problem
   by supplying more thread material to inherit.
5. **Grader value is conditional, not absolute.** Grader craft
   (structure-agnostic, 3-gate, baseline FAIL / fixed PASS) is sound.
   But a grader keyed on a fabricated symptom is worse than no grader
   at all: it produces confident PASS/FAIL on a phantom. Graders are
   only worth what the bug narrative under them is worth.

---

## 2. The reframe

The prior plan said: *"Stop asking the harness to discover on saturated
axes. Retarget discovery to unsaturated axes."* That is still correct as
far as it goes, and remains part of this plan.

The new frame adds two sentences the prior plan was missing:

> **The harness does not source bugs. The author sources bugs, on-bench,
> by personal reproduction. The harness refuses to package anything that
> has not been sourced that way.**

> **Difficulty is a measured pre-packaging gate, not a hope. A candidate
> that any three frontier-model attempts solve in median < 60 minutes is
> rejected before packaging cost is paid.**

Both are process changes enforced by the harness. The harness's
contribution is *refusal* — it will not emit a report artifact for a
candidate that has not cleared the new gates.

---

## 3. Saturated vs unsaturated axes (unchanged from prior plan)

Kept verbatim because this table remains correct.

| Axis | Status | Why |
|------|--------|-----|
| RSS-return-to-baseline on mature ASGI | **Saturated** | Prod already swept. Keep as regression net; do not extend. |
| FD-return-to-baseline on mature ASGI | **Saturated** | Same. |
| Route-registry stability | **Saturated** | Trivially exercised by every prod deploy. |
| Response determinism on canonical apps | **Saturated** | Any nondeterminism here would already be a bug report. |
| **Cross-version byte-diff** | **Unsaturated** | Nobody diffs adjacent versions byte-for-byte. |
| **Concurrency-mode matrix** (asyncio ↔ anyio-trio ↔ threadpool) | **Unsaturated** | State-desync across modes is where anyio-lifecycle-leak already lives. |
| **Teardown-order permutation** | **Unsaturated** | Prod apps run the canonical order. |
| **Fault-injected probes** (disconnect / cancel / SIGTERM mid-request) | **Unsaturated** | Litestar #3772 was exactly this shape. |
| **Diagnosis-ambiguity signal** | **Unmeasured** | The gate that has killed multiple candidates post-packaging. |

New invariants target unsaturated axes only.

---

## 4. The three sourcing gates (the new load-bearing addition)

Every candidate must pass all three of these gates, in order, before any
packaging work begins. The gates are enforced mechanically by the CLI
and audited by machine-checkable artifacts. See `rules.md` Rules 11-13
for the standing rules that back these gates.

### Gate 1 — Repro affidavit

**What:** A machine-readable JSON artifact
(`candidate/repro-affidavit.json`) signed off by the author before any
`grade.py` or `initial-prompt.md` is written. Fields:

- `pinned_commit`: full SHA (not a tag alias, not a version string).
- `repo_url`: canonical git URL.
- `bench_transcript_path`: path to a captured bench session showing the
  commit checked out, dependencies installed to the pin, the repro
  script executed, and the failing behaviour observed.
- `observed_behaviour`: two-to-four sentences, written by the author,
  describing what actually happened on-bench. Not what the issue thread
  says happened.
- `divergence_from_thread`: any respect in which the on-bench behaviour
  differed from the linked issue's report. Empty string only if the
  author has re-read the linked issue and confirmed no divergence — an
  explicit affirmation, not a default.
- `upstream_status`: `open` / `merged-pr-<n>` / `closed-fixed`. `merged`
  or `closed-fixed` is an automatic REJECT.
- `signed_by`: author name and ISO-8601 timestamp.

**Enforced by:** `bse affidavit <candidate-dir>` validates schema,
verifies the transcript exists and references the pinned SHA, and
refuses the affidavit if `upstream_status ≠ open`.

**Failure mode this closes:** dramatiq #431. If the author had run
the pin, they would have seen dead-worker maintenance re-queue the
job, and would not have written a symptom description that omits it.

### Gate 2 — Difficulty gate

**What:** Before any grader is written and before any packaged report
is emitted, the candidate is attempted by N=3 independent frontier-model
sessions using **only** the draft `initial-prompt.md` (symptom-only,
Rule 10). Each session's wall-clock time-to-fix and turn count is
recorded in `difficulty-attempts.jsonl`. The candidate passes iff the
**median time-to-fix ≥ 60 minutes**.

- A "fix" is defined as: the model produces a diff that causes the
  author's independent probe (the same probe the grader will use) to
  transition from FAIL to PASS.
- A session that exceeds a 3-hour ceiling without producing a passing
  diff is terminated (`subprocess.run(timeout=)` — SIGKILL at the
  ceiling) and counts as `>= 180 min` for the median.
- Sessions run in isolated ephemeral environments (no shared cache,
  no access to prior attempts, no access to the issue thread).

**Enforced by:** `bse difficulty-check <candidate-dir>` runs the three
sessions, writes the JSONL, computes the median, and refuses to
proceed if median < 60 min. The rejection is recorded in
`BACKPOCKET.jsonl` with the median so we do not re-attempt.

**Failure mode this closes:** every shipped task to date. All were
demonstrated after-the-fact to be < 1 h. The gate makes that
demonstration *before* packaging cost is paid.

### Gate 3 — Own-words writeup

**What:** The `initial-prompt.md`, `grading-criteria.md`, and any
`README.md` under `eval-tasks/<candidate>/` must be written by the
author, from the affidavit's `observed_behaviour`, without quoting or
paraphrasing the linked upstream issue. The linked issue may be listed
as a reference at the bottom of the README under an "Upstream
discussion" heading but may not be a source for the symptom
description, the reproduction steps, or the grading criteria.

**Enforced by:** `bse writeup-audit <candidate-dir>` does a mechanical
diff of the writeup against the linked issue's body and top-N comments
(fetched fresh at audit time), flags substring matches ≥ 8 words, and
requires the author to either rewrite the flagged section or annotate
it as a legitimately-shared technical term (e.g. a function name).
The audit output is committed as `writeup-audit.txt`.

**Failure mode this closes:** the inheritance channel through which
the dramatiq #431 fictional history entered the report.

**Order of gates:** Gate 1 (affidavit) → Gate 2 (difficulty) → Gate 3
(writeup). No gate may be skipped. A failure at any gate ends
packaging for that candidate. The candidate is recorded in
`BACKPOCKET.jsonl` with the failure reason.

---

## 5. The four-qualities gate, restated

The prior four qualities remain. This plan expresses which sourcing
gate certifies each:

| Quality       | Certified by                        |
|---------------|-------------------------------------|
| Reproducible  | Gate 1 (repro affidavit)            |
| Novel         | Gate 1 (`upstream_status = open`)   |
| Difficult     | Gate 2 (median ≥ 60 min)            |
| Divergent     | Divergence probe, §6 below          |

Divergence remains a required quality but is now measured, not asserted.

---

## 6. The divergence probe (retained from prior plan, refined)

After Gates 1–3 pass, before final packaging, the candidate goes
through a divergence probe.

- Feed the byte-stable violation evidence (and *only* that — no thread
  material) to N ≥ 2 frontier models, one session each, sealed from
  each other.
- Each returns a *diagnosis* — root-cause hypothesis in two sentences.
  Not a fix.
- Cluster the hypotheses by root-cause claim (not by prose similarity).
- **≥ 2 clusters = diagnosis-ambiguous = divergent = PROCEED.**
- **1 cluster = convergent = REJECT-CONVERGE.** Record in
  `BACKPOCKET.jsonl` with the shared hypothesis.

`bse triage <candidate-dir>` runs this probe. It is a hard gate: no
packaging without it.

Note the sequencing change vs. the prior plan: divergence runs *after*
Gates 1–3, not before. Reason: divergence is expensive; running it on a
candidate that fails the difficulty gate is waste.

---

## 7. Discovery retarget (unchanged in intent, reduced in scope)

Discovery still needs to feed unsaturated axes. Kept from prior plan:

- **T1.1 — Differential-across-versions runner (`bse diff`).** Same
  probe sequence, two adjacent pinned commits, diff the byte-stable
  reports. The diff *is* the finding — nothing to fabricate, because
  the artifact is machine-generated bytes.
- **T1.2 — Concurrency-mode matrix.** Same probe under asyncio /
  anyio-trio / anyio-asyncio / sync-in-threadpool. State-desync bugs
  of this shape are typically diagnosis-ambiguous by construction.
- **T1.3 — Teardown-ordering fuzzer.** Permute lifespan-shutdown-hook
  order (bounded space) and run existing baseline invariants.
- **T1.4 — Fault-injected probe adapter.** Client disconnect / cancel
  / SIGTERM / background-task exception mid-request. Multiplier on
  every existing invariant.

**Dropped:** T2.2 contested-issue harvester. Under Gate 3 (own-words
writeup) and Gate 1 (personal repro), contested threads are the *worst*
source material — their confusion is exactly what leaks into a writeup.
Anything harvesting-shaped that survives must be pointer-only (issue
URL, pinned SHA candidate) and must never surface thread narrative.
We do not build that in this plan; the harness's own discovery output
(T1.1–T1.4) is the sourcing channel.

---

## 8. Packaging hardening (kept from prior plan, promoted to first-class)

Retained tiers, now framed as protections around a scarce good rather
than as amplifiers on a broken pipeline.

- **T3.1 — Grader validator.** `bse validate-grader <candidate>`.
  Grader must PASS on the canonical fix, FAIL on the buggy tree, and
  FAIL on N mutated buggy trees. Catches "grader keys on an
  implementation detail of the canonical fix."
- **T3.2 — Repro provenance lock.** `bse verify-repro <candidate>`.
  Ephemeral venv, install the pinned commit exactly, run the repro,
  assert grader FAIL on baseline. Nightly cron. Catches upstream
  fixes / yanked deps / drifted transitive versions.
- **T3.3 — Baseline attribution schemad.** Formalize the artifact
  `anyio-lifecycle-leak` already ships; validate on package.

---

## 9. Build order

Explicit dependencies below. Each chunk: implement → test → verify →
commit → memory note (Rules 6 + 8).

| Chunk | Content | Depends on | Why here |
|-------|---------|------------|----------|
| **A** ✅ | Gate 1 (repro affidavit) + `bse affidavit` | none | Closes the dramatiq-#431-class failure directly. Cheapest gate to build. Nothing else ships until this exists. Shipped 2026-08-06 (b63a9fc). |
| **B** ✅ | Gate 2 (difficulty check) + `bse difficulty-check` | A | Requires an affidavit-approved candidate to run against. Expensive per candidate but pre-packaging. Shipped 2026-08-06 (5ac3f69). |
| **C** ✅ | Gate 3 (own-words audit) + `bse writeup-audit` + `bse scaffold-candidate` | A | Independent of B; can build in parallel if capacity allows. Shipped 2026-08-06 (f93a605) — includes scaffolder as ergonomics multiplier. |
| **D** ✅ | Divergence probe + `bse triage` (refined from T2.1) | A, B, C | Runs last of the four gates; benefits from filtering out easy or fabricated candidates first. Shipped 2026-08-06. |
| **E** ✅ | Differential-across-versions runner (T1.1) + `bse diff` | none, but sequenced here | Once the gates exist, discovery is the throughput bottleneck. Version-diff is the highest-yield unsaturated axis. Shipped 2026-08-06. |
| **F** ✅ | Concurrency-mode matrix + teardown fuzzer (T1.2, T1.3) | E for scaffolding | Cheap once E lands. Shipped 2026-08-06. |
| **G** ✅ | Grader validator + repro verifier (T3.1, T3.2) | none | Locks packaging quality permanently. Can run in parallel with A–D. Shipped 2026-08-06. |
| **H** ✅ | Fault-injected probe adapter (T1.4) | E, F | Multiplier — best done once the shape above is in place. Shipped 2026-08-06. |

**Old-task treatment (per decision 2026-08-06):** existing tasks
(`anyio-lifecycle-leak`, `aiocache-ttl-leak`, `piccolo-txn-data-loss`,
`dramatiq-431-delayed-dup`, `procrastinate-1495-periodic-loss`) are
frozen. They are not resubmitted, not re-verified through the new
gates, and not deleted. New gates apply to *new* candidates only.
Rationale: audit cost on old tasks is high; the reviewer's next
submission needs a fresh candidate anyway, so audit budget is better
spent on new work.

---

## 10. What "meeting the reviewer" looks like (definition of done)

A submission that would pass the reviewer's bar has all of:

- **Repro affidavit signed** at a specific SHA, with a bench transcript
  showing the author personally reproducing the failure at that SHA.
- **Difficulty gate passed** with median ≥ 60 minutes across three
  independent frontier-model attempts, transcripts retained.
- **Own-words writeup** with a clean `writeup-audit.txt` — no
  ≥ 8-word substring matches against the upstream issue body or top
  comments except explicitly-annotated technical terms.
- **Divergence probe passed** with ≥ 2 diagnosis clusters, session
  transcripts retained.
- **Grader validator green** — PASS on canonical fix, FAIL on baseline,
  FAIL on ≥ 3 mutated buggy trees.
- **Repro provenance verified** in an ephemeral venv within the last
  seven days.
- **All narrative text** (`initial-prompt.md`, `README.md`,
  `grading-criteria.md`) authored by the human submitter, from the
  affidavit's `observed_behaviour`.

Nothing ships that lacks any of these seven.

---

## 11. On the grader question — is it worth anything?

Answered here because it will keep coming up.

- **Runtime harness invariants** (`core/`, `harnesses/`) — yes,
  as a **regression net**. They caught anyio-lifecycle-leak end-to-end.
  Keep. Do not extend on saturated axes.
- **Byte-stable JSON reporter** — yes, unconditionally. This is the
  reusable engineering core of the repo. It is what makes `bse diff`
  possible, and what makes a grader validator (T3.1) trustworthy.
- **Per-task `grade.py`** — worth exactly as much as the bug narrative
  under it. On a phantom bug (dramatiq #431 style), a grader is worse
  than nothing: it lends confidence to a false artifact. With Gates 1
  and 3 in place, the narrative can be trusted; then the grader has
  real value. **Order of operations matters: gates first, then the
  grader means something.**

---

## 12. Non-goals

- Not a load-testing initiative. Correctness harness.
- Not a benchmark. Wall-clock only enters at Gate 2, as a
  difficulty measurement, not as a performance metric.
- Not a fuzzer in the AFL sense. Fault injection is controlled and
  bounded.
- Not a rewrite. `core/` stays. `reporter.py` is the crown jewel and
  stays. Harnesses grow sideways.
- **Not** a "find more bugs and ship them" push. This plan is
  deliberately fewer, better, reviewer-passable candidates.

---

## 13. Change log

- 2026-08-06 — Chunk H shipped: fault-injected probe adapter
  (`bse fault-matrix`). Multiplier on every existing invariant.
  Opt-in `FaultInjectable` Protocol (`available_faults`,
  `probe_with_fault`) mirrors the F-chunk Protocol idiom.
  `_FaultBoundPlugin` adapter routes probe → probe_with_fault
  per fault while forwarding every other Plugin method. Runner
  returns `dict[fault_name, dict[layer_name, Report]]`;
  `diff_faults()` surfaces cross-fault divergences using the
  same set-arithmetic idiom as `diff_modes` (Chunk F). Canonical
  fault set: `client-disconnect`, `cancel-mid-request`,
  `background-exception`. `sigterm-mid-request` deferred (OS-
  signal machinery + flaky-test failure modes). Exit codes 23-24.
  Byte-stable `fault-matrix.json`. Test count 339 → 352.
- 2026-08-06 — Chunk G shipped: packaging-hardening tier as first-
  class CLI verbs. T3.1 grader validator (`bse validate-grader`) —
  drives `grade.py` against a `grader-validation.json` manifest
  declaring baseline (must FAIL), canonical fix (must PASS), and
  ≥ 3 mutated buggy variants (must FAIL). Refuses graders that
  key on canonical-fix implementation detail rather than on the
  fix itself. Path-escape guard on manifest paths. T3.2 repro
  verifier (`bse verify-repro`) — ephemeral tmpdir venv, uv-first
  with pip fallback, run `reproduce.sh` with `PYTHON` set to the
  venv interpreter, assert baseline still FAILs. Nightly-cron
  wrapper shipped as `scripts/nightly-verify-repro.sh` (operator-
  owned scheduling, harness-owned verb). Four new exit codes
  19-22, byte-stable artifacts (`grader-validation-report.json`,
  `repro-verification.json`). Test count 311 → 339.
- 2026-08-06 — Chunk F shipped: two new unsaturated-axis runners.
  T1.2 concurrency-mode matrix (`bse concurrency-matrix`) — runs
  discovery under every mode the plugin declares via an opt-in
  `ConcurrencyAware` Protocol; cross-mode divergences surface via a
  set-arithmetic `diff_modes` reusing Chunk-E's identity-key idiom.
  T1.3 teardown-order fuzzer (`bse teardown-fuzz`) — enumerates 4!
  = 24 permutations of the plugin's declared shutdown hooks via
  opt-in `TeardownAware`, flags any order whose observed behaviour
  diverges from the canonical. Both use fail-loud precondition
  errors (no silent fallback), distinct exit codes 15-18, byte-stable
  JSON artifacts (`mode-matrix.json`, `teardown-fuzz.json`). Neither
  auto-packages: the diff IS the finding, operator uses
  `bse scaffold-candidate` on interesting rows. Test count 289 → 311.
- 2026-08-06 — Chunk E shipped: `bse diff` cross-version differential
  runner. Two modes (in-process pip-install-and-run, file-mode over
  two `report.json` bundles). Emits byte-stable `diff-report.json`
  with three arrays per layer (only_in_a = fixes, only_in_b =
  regressions, evidence_changed = drift), summary line
  `+ N regressions, - N fixes, ~ N drift`, distinct exit codes 0/13/14.
  The diff is the finding — no auto-packaging, callers use
  `bse scaffold-candidate` on interesting rows. Test count 275 → 289.
- 2026-08-06 — Chunks A, B, C, D shipped in sequence (all in a single
  session). Repro-affidavit gate, difficulty gate (N=3 headless
  `claude -p` sessions, median ≥ 60 min), own-words writeup audit
  (live GitHub fetch with snapshot fallback + 8-word phrase
  matching + paragraph-scoped annotations), and divergence probe
  (N=3 diagnosis sessions, cluster by shared content-word overlap,
  ≥ 2 clusters = proceed). Also `bse scaffold-candidate` as the
  ergonomics multiplier and a `dev-fixtures/trivial-candidate/`
  smoke test for the difficulty driver. Full test suite grew from
  144 → 275 tests.
- 2026-08-06 — Full rewrite after reviewer feedback on dramatiq #431.
  Supersedes 2026-08-05 version. Key changes: three sourcing gates
  (affidavit, difficulty, own-words) added as load-bearing gates;
  contested-issue harvester dropped; divergence probe re-sequenced to
  run after the three gates; old tasks frozen; difficulty floor fixed
  at N=3 attempts, median ≥ 60 minutes.
- 2026-08-05 — Prior version (superseded). See git history.
