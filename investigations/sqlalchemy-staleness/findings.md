# SQLAlchemy staleness hunt — substrate finding (2026-08-02)

**Goal (step 2):** find an identity-map / expire-on-commit staleness bug
hard enough for a 1–2h eval. Locked substrate: SQLite in-memory + ORM
Session.

## Blocking finding: SQLite in-memory can't simulate a concurrent writer

The probes need a genuinely *external* writer to create staleness (session
holds a cached instance; another connection mutates the row; re-read must
reload or is stale). But:

- `create_engine("sqlite://")` uses **`SingletonThreadPool`** — the Session
  and any "external" `engine.begin()` block resolve to the **same DBAPI
  connection** (verified: identical `id()`).
- So an "external" `UPDATE` is not external; it runs on the session's own
  connection, and the re-read sees fresh data. No genuine staleness path.

Teeth test failed (`0/20` stale detected) for exactly this reason — the
fault we tried to inject can't exist on this substrate. A separate single
-shot repro DID show a stale `got is w` identity-map hit, but that is the
*no-DB-access* path, not the concurrent-writer path the eval bug needs.

## Consequence

SQLite in-memory + default pool is the **wrong substrate** for the
identity-map/expire-on-commit bug shape. Two isolated connections are
required. Options:

1. **SQLite file** (`sqlite:///tmp.db`) — real separate connections; the
   external writer genuinely isolated. Cheap, still no server. Needs temp
   -file teardown. Most likely fixes the substrate with least effort.
2. **SQLite in-memory shared-cache** (`file::memory:?cache=shared` +
   `StaticPool`) — trickier; shared-cache semantics differ from a server.
3. **Postgres** — real MVCC/isolation; the sharpest staleness surface but
   needs a running server (heavier, less deterministic).

Recommend option 1 (SQLite file) as the minimal substrate fix, re-run the
teeth test FIRST (must detect an injected stale read), then hunt.

If the substrate fix still yields no *bug* (SQLAlchemy behaving correctly is
the likely outcome — its expiry model is mature), step 2 is dry and we move
to step 3 (anyio symptom-only re-scope).

## Substrate fixed (SQLite file) — and step 2 is DRY

Switched `_fresh_engine()` to a temp-file SQLite engine (context-managed,
always unlinked). Verified the substrate is now correct: `QueuePool`,
session and external writer get **different** DBAPI connections, external
write is visible on a third connection.

**But the staleness bug does not exist here — SQLAlchemy behaves correctly:**

- With `expire_on_commit=False`, `commit()` still ends the transaction; the
  next `s.get()` starts a fresh transaction and RELOADS from DB → returns the
  updated value. Teeth test stays `0/20` because the injected "stale read"
  simply doesn't occur — SQLAlchemy 2.0 reloads correctly.
- The ONLY way to manufacture true staleness: read an object **inside an open
  transaction**, have an external writer commit, then re-read the same PK in
  the **same** transaction → identity-map hit returns the old value until an
  explicit `s.expire()`/`s.refresh()`. Verified: `orig` → `s.expire()` →
  `changed`.
- That is **correct, documented behavior** — the identity map is a
  within-transaction cache by contract. It's a well-known footgun, not a bug,
  and a frontier model would name it on sight. Zero investigation depth.

**Conclusion:** no novel, hard, deterministic staleness bug on this surface.
SQLAlchemy's expiry/identity-map model is mature and correct. Step 2 is DRY
(teeth-verified: we could reproduce the footgun but it isn't a defect).

## Decision: step 2 dry → proceed to step 3 (anyio symptom-only re-scope)

Per the agreed order (1 → 2 → 3), both interaction hunts are exhausted:
- Step 1 (widen FastAPI lens): green + teeth-verified negative.
- Step 2 (SQLAlchemy staleness): correct-by-design, no bug.

Move to step 3: re-scope the known anyio 4.14.2 leak into a gradeable 1–2h
task via a SYMPTOM-ONLY prompt (strip the event-loop/worker-pool giveaways)
+ a partial-credit rubric shipped in the working dir + stock current-stable
release (fixing crit-4 of the original grading).
