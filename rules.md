# Working Rules — backend-stress-eval

These rules apply at all times during this project, whether or not they are restated in a given prompt. Every task, plan, review, and commit must comply. `discovery-strategy.md` references this file and must be read alongside it.

---

## Rule 1 — Complexity discipline

Prefer **O(1)** wherever it is achievable. Never accept worse complexity than the problem requires.

* Justify any algorithm above **O(n log n)** in a comment at the call site.
* Hot paths (per-request, per-iteration inside the stress runner, per-invariant check) must be **O(1) amortized** unless the input size is provably bounded and small.
* No hidden quadratic behavior (nested loops over the same growing collection, repeated `list.index`, `in` on lists, string concatenation in loops, etc.).

## Rule 2 — Security is paramount

Write code that is robust against security threats.

* Validate all inputs at the boundary. Fail closed.
* No secrets in code, logs, or error messages.
* No `eval` / `exec` / unsanitized `subprocess` / unsafe deserialization on untrusted data.
* Least privilege for filesystem, network, and process access.
* If a security bug is spotted **outside** the current task's scope, flag it or fix it — do not silently pass it.

## Rule 3 — Be sure; get context; ask when unclear

The codebase is large. Guessing costs more than asking.

* Read the relevant files before editing. Do not assume structure.
* Ask clarifying questions when requirements are ambiguous or a decision changes scope.
* Prefer verified facts over inferred ones.

## Rule 4 — No dead code

No unused imports, parameters, branches, functions, files, fixtures, or config keys. If something is removed, remove it fully. If it is kept for future use, it does not belong in a commit yet.

## Rule 5 — Robust, clear, efficient

* Handle errors explicitly. Never swallow exceptions.
* Fail with actionable messages — include what was being done and what value caused it.
* Keep functions small and named for what they do.
* Efficiency is not optional; see Rule 1.

## Rule 6 — Small, verifiable chunks

Break tasks into the smallest chunks that still deliver a testable outcome.

* Each chunk: implement → test → verify → commit (or hand off for review).
* Do not stack multiple concerns into one change.
* If a chunk grows, split it before continuing.

## Rule 7 — Perfection over speed

Correct, well-tested, well-reasoned work beats fast work. Do not ship "probably fine." When time pressure and correctness conflict, correctness wins.

## Rule 8 — Record progress in memory

At the end of each meaningful chunk, write a memory note capturing:

* What was done.
* Decisions locked in and why.
* What remains.
* Any traps or gotchas discovered.

This project must survive session boundaries. Memory is the handoff mechanism.

## Rule 9 — Measure before theorizing

The correct debugging order is:

1. **Reproduce** the failure deterministically.
2. **Capture** the real failure detail — actual output, actual stack, actual metric, actual state.
3. **Then** form **one** theory and test it.

No guess-and-rerun. No parallel speculative fixes. No theorizing before the failure is in hand.

This rule is doubly important here because the whole project's thesis (see `discovery-strategy.md` §7, §9) is that hard bugs mislead theorizers who skip measurement.

## Rule 10 — Prompt-only tasks; measure time-to-fix

Every eval task ships the model **exactly one file: `initial-prompt.md`** (symptom-only). **Never ship a reproducer** (`minimal_repro.py` or equivalent) into a model's working dir.

* A runnable reproducer pre-localizes the bug — it does the hardest part of an L3 task (finding *where* the fault originates) for the model, collapsing every task to "both models pass, no differentiation." That defeats the eval.
* The "bug is live" check must be an **inline probe** inside `make-eval-dirs.sh`, and the **grader must use its own independent probe** — never a model-side file. If a grader currently runs the model's reproducer, rewrite it to be repro-independent before shipping prompt-only.
* Keep `grading-criteria.md`, rubrics, findings, and graders **out** of the model dir; the build script's leak-check must list the reproducer among the forbidden files.
* **Time-to-fix and turn count are first-class metrics**, recorded by hand in each task's `results.md`. Same pass/fail grade with very different effort *is* differentiation (see `discovery-strategy.md` §7: "the models solve the problem in 4 minutes… that is too small to measure feedback"). Assisted (with-repro) times are not comparable to prompt-only and must be flagged as such.

---

## Enforcement

* Every plan, PR, and review must implicitly satisfy all ten rules.
* If a rule is intentionally deviated from, the deviation must be called out and justified in the change itself.
* `discovery-strategy.md` references this file; both are canonical.





