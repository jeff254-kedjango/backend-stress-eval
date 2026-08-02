# SQLAlchemy-on-Postgres staleness hunt — findings (2026-08-02)

**Target:** SQLAlchemy 2.0.51 + psycopg 3.3.4 + Postgres 14.23, Python 3.12
**Substrate:** `postgresql+psycopg:///bse_hunt`, real MVCC, VERIFIED two
isolated backend PIDs (session vs external writer) — the genuine concurrent
writer that SQLite in-memory could not provide (see
`investigations/sqlalchemy-staleness/findings.md`).
**Goal (step 3-Postgres):** find a *novel* staleness/isolation interaction —
one a frontier model would NOT name on sight — hard enough for a 1–2h eval.

## Setup done this chunk

- Created scratch DB `bse_hunt` (role `jeff`, peer auth, superuser).
- Installed `psycopg[binary]` 3.3.4 into `.venv`.
- Confirmed substrate: `session pid != external pid`, `QueuePool`/`NullPool`
  both give real separate backends.

## Probes (all teeth-verified — Rule 9)

Two scenarios × two isolation levels. Teeth: a no-writer run must read `orig`
(no false stale); a READ COMMITTED post-commit run must read `changed` (no
false fresh). **Both teeth PASS**, so the reals below are trustworthy.

| Scenario | READ COMMITTED | REPEATABLE READ |
|---|---|---|
| A: re-read in SAME open txn (after `s.expire`) | fresh (`changed`) | **stale (`orig`)** |
| B: re-read after `commit()` boundary (new txn) | fresh (`changed`) | fresh (`changed`) |

## Result — NEGATIVE (correct-by-design, teeth-verified)

The one "stale" cell (A / REPEATABLE READ) is **not a bug**:

- It is stale even though `s.expire()` forced a real DB round-trip — so it is
  NOT the identity-map cache footgun. It is the **Postgres MVCC transaction
  snapshot**: a REPEATABLE READ transaction is *defined* to see a single
  consistent snapshot for its whole lifetime, so an external commit is
  correctly invisible until the transaction ends.
- Scenario B confirms the boundary: once `commit()` ends the transaction, the
  next `get()` opens a new snapshot and reads fresh at BOTH isolation levels.
- SQLAlchemy is faithfully surfacing exactly what the DBAPI returns. There is
  no layer where it caches incorrectly, expires wrongly, or diverges from the
  isolation contract.

A frontier model would name "REPEATABLE READ holds a snapshot" immediately —
zero investigation depth. Same verdict category as the SQLite hunt: a real,
reproducible behaviour that is **correct and documented**, not a defect.

## Conclusion

No novel staleness bug on SQLAlchemy 2.0 + Postgres 14 across isolation
levels, commit boundaries, and expire semantics. SQLAlchemy's transaction/
identity-map model is mature and correct; Postgres MVCC is doing exactly what
the SQL standard specifies. This closes the last untried staleness substrate.

## Hunt scoreboard (all teeth-verified)

| Surface | Result |
|---|---|
| FastAPI 0.141.1 interactions (L3 + widened lens) | dry / correct |
| SQLAlchemy staleness — SQLite in-memory | wrong substrate (no real writer) |
| SQLAlchemy staleness — file SQLite | correct-by-design |
| **SQLAlchemy staleness — Postgres MVCC** | **correct-by-design (this doc)** |
| Starlette 1.3.1 middleware+bg+contextvar | dry / correct |
| anyio 4.14.2 lifecycle leak | REAL bug, but too easy (both models fix it) |

## Honest assessment for the reviewer bar

Six surfaces hunted, all teeth-verified. The only real bug found (anyio) does
not differentiate two frontier models on any grading criterion — both fully
fix it; they differ only in fix elegance, which the rubric doesn't score. No
surface hunted so far yields a task meeting the strict reviewer bar
("models perform differently on at least some grading criterion").

This is a substantive, honest finding in itself: across popular Python backend
libraries at current stable versions, the deterministic-lifecycle/staleness
bug class is largely *absent or on-sight* — these libraries are mature. The
platform (generic core + 3 plugins: fastapi, starlette, stub, + the SQLAlchemy
probe substrate) works and the negatives are trustworthy, but a differentiating
task has not been found on these surfaces.

### Remaining options (diminishing returns)
- **Cross-version bisect** on anyio: find the anyio version where the leak is
  subtler (partial fix) so localization actually separates models — but risks
  the "pinned-old" criterion again.
- **A less-mature target**: the maturity of fastapi/starlette/sqlalchemy/anyio
  is itself the obstacle. A newer/thinner library (e.g. a recent async ORM,
  a task-queue) is likelier to still carry a hard, undocumented lifecycle bug.
- **Reframe the deliverable**: present the *harness + methodology + trustworthy
  negatives* as the result, with the anyio task as a fix-quality (not
  pass/fail) differentiator, and state plainly it doesn't meet the strict bar.
