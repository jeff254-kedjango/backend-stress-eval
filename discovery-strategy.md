# Backend Debugging Evaluation Task Discovery Strategy

> **Working rules — read first.** All work on this task must follow the nine rules in [`rules.md`](./rules.md) at all times, whether or not they are restated in a given prompt. `rules.md` is canonical and must be referenced consistently through every plan, implementation, review, and handoff. Any deviation must be called out and justified in the change itself.

## Summary of Discussion, Reasoning, and Final Decisions

**Purpose of this document**

This document captures the discussion around designing a high-quality debugging task for evaluating frontier coding models. The goal is to create a task that satisfies the evaluation requirements:

* The task must involve a real backend debugging problem.
* It must be difficult enough that strong coding models require substantial investigation time.
* It must contain objective grading criteria.
* It must differentiate between models rather than being solved instantly by both.

The discussion evolved from trying to manually discover bugs toward designing a systematic methodology for discovering deep, model-challenging debugging tasks.

---

# 1. What We Discussed

## 1.1 The original challenge

The starting problem was that finding a suitable evaluation task had become difficult.

The requirements were:

* Pick a popular open-source backend project.
* Find a real bug.
* Ensure the bug exists at a specific commit.
* Ensure it is not already fixed or publicly discussed.
* Ensure it requires significant debugging effort.
* Create grading criteria that objectively measure the solution.

The main difficulty identified was that many real bugs are not suitable evaluation tasks because they are either:

* too easy,
* already known,
* quickly solved by current models,
* or difficult to grade.

A key observation was:

> "Finding a task that meets this threshold has defeated me."

The discussion then focused on why this was happening and how to change the approach.

---

# 2. Why Simple Bugs Are Not Suitable

## 2.1 Experience with previously discovered bugs

A major insight came from previous attempts:

> "I have found bugs and reproduced them even but the models solve the problem in 4 minutes...that is too small to measure feedback."

This highlighted an important distinction:

A bug can be real and technically interesting but still be a poor evaluation task.

For example:

```text
Bug:
Incorrect condition in function X.

Investigation:
Read error.
Find function.
Change line.
Run tests.
```

This type of task does not differentiate frontier models because the debugging path is too direct.

---

# 3. The Important Distinction: Bug Difficulty vs Investigation Difficulty

A central conclusion was that evaluation tasks should not necessarily have complicated fixes.

The important factor is the complexity of discovering the correct solution.

A five-line fix can be extremely difficult if:

* the failure appears far from the root cause,
* several systems interact,
* multiple hypotheses must be eliminated,
* the bug is intermittent,
* existing tests pass.

The discussion emphasized:

> "The task is not: Find a hard bug. It's: Find a bug whose root cause requires about an hour of investigation, even if the final code change is only a few lines."

This became one of the foundational principles.

---

# 4. Learning From the Playwright/Vite Example

A particularly important example was provided:

> "Claude discovered a bug that only appears after 7 runs. Playwright passed it and even Vite tests turned green, but after every 7 runs the bug appeared."

This changed the direction of the search.

The important characteristics were:

* The bug was not immediately visible.
* Normal testing gave false confidence.
* State accumulated over time.
* The model had to investigate lifecycle behavior.
* The solution required extended reasoning.

This led to the conclusion that the ideal task is not simply a broken function.

Instead, it is likely a:

> "state exploration problem."

---

# 5. The Decision to Focus on Hidden State and Lifecycle Bugs

The discussion moved away from simple feature bugs and toward bugs involving:

* state accumulation,
* cleanup failures,
* resource leaks,
* caching issues,
* lifecycle problems,
* repeated execution failures.

Examples discussed:

## Resource leaks

```text
Request succeeds.

Repeated requests accumulate resources.

Eventually the system fails.
```

## Cleanup failures

```text
Dependency starts.

Cleanup does not happen correctly.

Later operations fail.
```

## Cache/state corruption

```text
First request works.

Later requests receive stale or incorrect state.
```

The reasoning was:

These bugs naturally force models to investigate rather than immediately patch.

---

# 6. Why FastAPI Was Selected as a Candidate

FastAPI was discussed as a strong target because:

* It is popular.
* It is backend-focused.
* It has a relatively understandable codebase.
* It interacts with several complex systems.

The areas identified as valuable:

* Dependency injection
* Response serialization
* Middleware
* Lifespan handling
* Background tasks
* Async execution

The reasoning was that these components create interaction graphs.

A request may flow through:

```text
Request

↓

Router

↓

Dependency Injection

↓

Validation

↓

Middleware

↓

Endpoint

↓

Background Tasks

↓

Response Serialization

↓

ASGI Layer
```

The more layers involved, the more difficult the debugging process becomes.

---

# 7. Why We Should Not Intentionally Create Random Failures

A key clarification was made:

We should not create unreliable flaky tests.

Instead, we should search for deterministic bugs whose symptoms appear after repeated operations.

The desired pattern:

Bad:

```text
Random failure after unknown conditions.
```

Good:

```text
Repeat the same sequence 50 times.

Failure occurs consistently during iteration 37.
```

The difference:

* The first is difficult to reproduce.
* The second is difficult but objectively testable.

---

# 8. Decision to Build an Automated Stress and Exploration Harness

The discussion then moved from manual discovery to automation.

The proposed idea:

Create a framework that repeatedly exercises backend systems and checks invariants.

The reasoning:

Humans are bad at manually discovering long-running lifecycle problems.

Automation can explore:

* hundreds of requests,
* repeated application startup/shutdown cycles,
* dependency overrides,
* database sessions,
* serialization paths,
* async workflows.

The key idea:

> Instead of trying to invent complex bugs, write stress and repetition harnesses.

---

# 9. Proposed Testing Strategy

The automation framework would contain several layers.

## Layer 1: Repetition Testing

Example:

```text
Repeat the same request hundreds of times.
```

Purpose:

Find:

* state leaks,
* inconsistent responses,
* resource growth.

---

## Layer 2: Lifecycle Testing

Example:

```text
Create application.

Run request.

Destroy application.

Repeat.
```

Purpose:

Find:

* startup/shutdown bugs,
* cleanup failures,
* global state leaks.

---

## Layer 3: Feature Combination Testing

Instead of testing features independently:

```text
Dependency works.
Middleware works.
Background task works.
```

Combine them:

```text
Dependency + Middleware

Dependency + Background Task

Yield dependency + Streaming response
```

Reason:

Many difficult bugs only appear when individually correct components interact.

---

## Layer 4: Sequence Testing

Many bugs depend on history.

Example:

```text
Login

Create object

Delete object

Restore object

Logout
```

Repeated sequences can expose hidden state problems.

---

## Layer 5: Invariant Checking

The framework should not only look for crashes.

It should verify properties such as:

* cleanup occurs exactly once,
* memory returns to baseline,
* database connections are released,
* route configuration does not unexpectedly change,
* repeated identical requests produce identical results.

---

# 10. Final Decisions Reached

## Decision 1

Do not search for simple isolated bugs.

Reason:

They are often solved too quickly by frontier models.

---

## Decision 2

Target bugs involving:

* state,
* lifecycle,
* resource management,
* repeated execution,
* component interaction.

Reason:

These require investigation rather than pattern matching.

---

## Decision 3

Use FastAPI as the **initial** target — but not the only one.

Reason:

It provides a good balance:

* complex enough,
* modern architecture,
* manageable codebase,
* many interacting backend components.

FastAPI is the first ecosystem we exercise the framework against, not the framework's scope. See Decision 6 for how the harness stays portable.

---

## Decision 4

Build a reusable stress-testing and exploration framework.

Reason:

Manual bug discovery is slow and unreliable.

Automation allows systematic exploration.

---

## Decision 5

The framework should search for invariant violations, not just exceptions.

Reason:

Many of the hardest backend bugs do not fail immediately.

They violate assumptions over time.

---

## Decision 6

Do not tie the framework specifically to FastAPI.

The harness must be built as a **generic core engine + per-ecosystem plugin adapters**, so the same engine can evaluate Django, SQLAlchemy, Flask, or any other backend project by writing a small adapter — not by starting over.

Proposed layout:

```text
core/
    runner.py       # repetition, scheduling, iteration control
    invariant.py    # invariant registration + violation detection
    metrics.py      # memory, connections, handles, timings
    sequence.py     # stateful sequence definition + replay
    reporter.py     # deterministic failure reports for grading

plugins/
    fastapi/
    django/
    sqlalchemy/
    flask/
```

Responsibility split:

* **Core** owns everything ecosystem-agnostic: repetition loops, sequence execution, invariant registration and checking, metric sampling, and result reporting.
* **Plugins** own everything ecosystem-specific: how to construct an app, how to issue a request, how to drive startup/shutdown and lifespan events, and how to reach into framework-specific features (dependency overrides, ORM sessions, middleware stacks, background tasks, etc.).

Reason:

* Avoids rewriting the harness for every new target project.
* Lets a single set of invariants (cleanup-once, memory-baseline, connection-release, response-determinism, route-registry-stability) be reused across ecosystems.
* Turns the deliverable from "one FastAPI eval" into a **reusable platform for discovering deep lifecycle and state-management bugs across multiple backend ecosystems**.
* Keeps grading criteria comparable across targets, because the invariant layer is shared.

FastAPI is simply the first plugin. Django, SQLAlchemy, and Flask are the next likely adapters.

---

# Final Strategy Statement

The agreed approach is:

> Build an automated backend stress and lifecycle exploration framework that systematically exercises complex interactions inside popular open-source projects, detects violations of expected invariants, and identifies deterministic but difficult-to-debug failures suitable for frontier model evaluation.

The goal is not to find bugs with complicated patches.

The goal is to find bugs where reaching the correct patch requires deep investigation.

A successful evaluation task should make a strong model ask:

* "Where does this state come from?"
* "Which component owns this behavior?"
* "Why do normal tests pass?"
* "What changes after repeated execution?"
* "Which lifecycle assumption is incorrect?"

Those are the debugging challenges that can meaningfully separate strong coding models.
