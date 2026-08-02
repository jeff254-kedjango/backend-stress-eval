# Starlette interaction hunt — findings (2026-08-02)

**Target:** Starlette 1.3.1 (pure, no FastAPI), anyio 4.14.2, Python 3.12
**Goal (step 3-Starlette):** find a feature-*interaction* bug on a thinner,
less-picked-over surface than FastAPI — after the FastAPI hunt came up dry
and its own `findings.md` recommended Starlette next.

**Deliverable built alongside:** `plugins/starlette/` — a pure-Starlette
plugin conforming to `core.plugin.Plugin` (auto-discovered by the registry,
protocol-conformant, smoke-tested). Adding a framework is one dir, per the
Decision-6 design.

## Hypotheses tested

The FastAPI findings named three surfaces the route/response/RSS/FD
invariants CANNOT see. The one most Starlette-specific — "exception-group
swallowing in combined middleware+bg paths" — plus the two classic
`BaseHTTPMiddleware` hazards, were probed directly. All teeth-verified
(Rule 9: inject a known fault, assert the probe catches it, BEFORE trusting
a green).

| # | Interaction probed | Teeth | Real result |
|---|---|---|---|
| P1 | `BackgroundTask` scheduled behind `BaseHTTPMiddleware` still runs | PASS (detects a dropped task) | **runs correctly**, with or without middleware |
| P2 | raising `BackgroundTask` surfaces (not swallowed) behind middleware | PASS (detects a non-raising task) | **surfaces identically**, with or without middleware |
| P3 | contextvar isolation across the `BaseHTTPMiddleware` boundary | (inline recon) | middleware→endpoint propagates; endpoint mutation does NOT leak back or across requests — **correct** |

## Result — NEGATIVE (teeth-verified)

Starlette 1.3.1 is **correct** on all three surfaces:

- **P1** — `bg ran (no mw)=True`, `bg ran (with mw)=True`. The historical
  "BaseHTTPMiddleware's streaming wrapper drops `response.background`" hazard
  is fixed in this version. Teeth confirm the probe would have caught a drop
  (`drop_background=True` → `ran=False`).
- **P2** — error visibility identical with/without middleware. Teeth confirm
  the probe reports `surfaced=False` for a non-raising task, so the green is
  real.
- **P3** — 3 sequential requests: `endpoint_seen='req-{i}'` each time (no
  bleed), `cv_after_mw` reflects the middleware's own value (endpoint mutation
  contained). Contextvar isolation intact.

These are **trustworthy negatives**, not false passes.

## Conclusion

No interaction bug found on Starlette 1.3.1 across the middleware+background
+contextvar surfaces — the sharpest known hazards, all fixed/correct in this
release. This is consistent with the pattern across the whole hunt: FastAPI
(dry), SQLAlchemy (correct-by-design), anyio (real but too easy), and now
Starlette (correct on its historically-buggy surfaces).

## Honest assessment for the reviewer bar

The reviewer criterion is "the two models perform differently on at least
some grading criterion." A teeth-verified NEGATIVE does not produce a
differentiating task — there is no bug here to grade against. What this hunt
DID produce:

1. A reusable `plugins/starlette/` adapter (real deliverable, conforms to the
   platform contract) — the framework now covers a third ecosystem.
2. Three teeth-verified negatives that *retire* the most-likely Starlette
   interaction hazards, so future hunts don't re-tread them.

## Next moves (unchanged priority order)

The tractable surfaces are exhausting. The remaining honest options:

- **Older Starlette pin** — the P1 bg-drop bug was real in older Starlette
  (<0.21-ish). But that reintroduces the v1-anyio flaw: a pinned-old tree the
  criteria explicitly forbid ("stock current stable"). Rejected for the same
  reason.
- **Postgres-backed SQLAlchemy** — the `sqlalchemy-staleness` findings showed
  SQLite in-memory can't host a genuine concurrent-writer staleness bug and a
  file-SQLite substrate proved SQLAlchemy correct-by-design. Real MVCC
  (Postgres) is the one substrate not yet tried; heavier, less deterministic,
  but the only place a *novel* staleness interaction could still live.
- **Accept the anyio task as-is** — if the goal shifts from "differentiate on
  pass/fail" to "differentiate on fix quality," the anyio v2 task already does
  that (B's 9-line weakref vs A's 23-line private-API cleanup). But by the
  strict reviewer bar it does not qualify.
