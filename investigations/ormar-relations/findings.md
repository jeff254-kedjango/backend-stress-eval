# ormar relation-cache hunt — findings (2026-08-02)

**Target:** ormar 0.26.0 (async ORM over SQLAlchemy core) + asyncpg 0.31 +
Postgres 14.23, Python 3.12. **1,804 GitHub stars** (live count).
**Sweep context:** ascending-maturity, ≥1000★ reviewer popularity floor.
Skipped odmantic (1,174★, needs MongoDB) and aiocache (1,435★, needs Redis)
for lack of a running service; ormar reuses the `bse_hunt` Postgres substrate.

## Setup done this chunk

- Installed `ormar==0.26.0`, `databases==0.9.0`, `asyncpg==0.31.0`.
- ormar 0.26 uses its OWN `ormar.databases.connection.DatabaseConnection`
  (SQLAlchemy-async based), NOT the third-party `databases.Database` — the
  latter raises `AttributeError: get_query_executor`. DSN must be async:
  `postgresql+asyncpg://jeff@/bse_hunt`.
- Smoke test passed. Notable: `Book.objects.get(...) is created_obj` → **False**
  — ormar returns a fresh instance per query (no SQLAlchemy-style identity map).

## Probes (teeth-verified — Rule 9)

Recon surfaced that `book.load()` after a `select_related("author")` nulls the
cached relation. Characterised it:

| Q | Probe | Result |
|---|---|---|
| Q1 | does `book.load()` drop a loaded FK relation? | **yes** — `before='orig-author'`, `after=None`, reproducible |
| Q2 | is the dropped value a silently-wrong None while the row exists? | probe returned False — the post-load relation is a pk-only stub, not a plain readable None |

Teeth PASS: relation is genuinely present before load (`before='orig-author'`),
author row genuinely exists in the DB. So the reals are trustworthy.

## Result — NEGATIVE (documented behaviour, teeth-verified)

`book.load()` dropping the preloaded relation is **ormar's documented,
intended `.load()` contract**, verbatim from `Model.load.__doc__`:

> "Be careful as the related models can be overwritten by pk_only models in
> load. Does NOT refresh the related models fields if they were loaded
> before."

`load_all(follow=...)` exists specifically to refresh relations. So this is a
named method-choice footgun (`load()` vs `load_all()`), documented in the API
itself — a frontier model reading the docstring names it on sight. Zero
investigation depth. Same verdict category as the SQLAlchemy and Postgres
hunts: real and reproducible, but **correct-by-design / on-sight**.

## Conclusion

No novel, hard, deterministic bug on ormar 0.26's relation-cache surface. The
`load()`/`load_all()` split is documented behaviour. First rung of the
ascending-maturity sweep is DRY (teeth-verified).

## Sweep scoreboard so far (ascending maturity, ≥1000★)

| # | Target | Stars | Status | Result |
|---|---|---:|---|---|
| 1 | odmantic | 1,174 | SKIPPED | needs MongoDB service |
| 2 | aiocache | 1,435 | SKIPPED | needs Redis service |
| 3 | **ormar** | **1,804** | **HUNTED** | **dry (documented load() contract)** |
| 4 | piccolo | 1,934 | pending | async ORM, Postgres/SQLite |
| 5 | taskiq | 2,270 | pending | needs a broker |
| 6 | beanie | 2,691 | pending | needs MongoDB |
| 7 | arq | 2,998 | pending | needs Redis |

## Next move

Per stop-at-first-bug + ascending order, the next no-new-service candidate is
**piccolo (1,934★)** — its own async ORM (not a SQLAlchemy wrapper), works on
Postgres, so it reuses `bse_hunt`. If a service becomes available, odmantic
(1,174★) is the strictly-lower rung to revisit first.

Open question for the reviewer bar: three ORMs (SQLAlchemy, ormar) and their
substrates now show the staleness/relation bug class is documented-or-absent
at current stable. The maturity axis is real but ORMs specifically may be a
picked-over category regardless of star count. Task queues (taskiq/arq) and
ASGI frameworks (litestar) exercise a *different* bug class (worker/job
lifecycle, DI scope) closer to the anyio leak we did find — may be higher-yield
than more ORMs, but need a broker/Redis.
