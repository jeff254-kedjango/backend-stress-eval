# procrastinate #1495 — periodic task silently lost after orphaned defer row

**Repo:** procrastinate-org/procrastinate @ `d9cf91de96611d5da2fee9c48762cdd297d5ba6c`
**Issue:** #1495, OPEN, no branch/PR (novelty verified 2026-08-03 via GitHub).
**Substrate:** PostgreSQL (have it locally). Stars ~1.1k+ (>1k bar).

## Symptom
Periodic (cron) task silently stops being deferred after a worker restart — no
error, no log. Triggered when the worker was killed mid-defer, OR when
`delete_jobs="successful"` cleans up finished jobs.

## Root cause (confirmed by reading source + repro)
`procrastinate_defer_periodic_job_v2` (SQL fn, migration
`03.02.00_01_pre_batch_defer_jobs.sql`) does:
1. `INSERT INTO procrastinate_periodic_defers (task_name, periodic_id,
   defer_timestamp) ... ON CONFLICT DO NOTHING RETURNING id INTO _defer_id`
2. `IF _defer_id IS NULL THEN RETURN NULL` (interpreted upstream as "already
   deferred, skip")
3. only THEN does it create the job and set `job_id`.

So the `(task_name, periodic_id, defer_timestamp)` row is the dedupe key, written
BEFORE the job exists. If the process dies between (1) and (3), or the job is
later deleted (`delete_jobs='successful'`), the row survives with `job_id = NULL`
(or dangling). On the next attempt for the SAME timestamp T, the INSERT hits
ON CONFLICT DO NOTHING → `_defer_id` NULL → `RETURN NULL` → deferrer logs
`periodic_task_already_deferred` and the job is **never created**. Silent loss.

Compounded by the deferrer's in-memory `last_defers` (periodic.py): a freshly
restarted worker has empty `last_defers`, takes the `get_prev`-only branch, and
if T is older than `max_delay` (default 600s) it's ignored — so the DB row is the
only dedupe guard, and that guard is poisoned.

## Reproduction — CONFIRMED (repro.py, real Postgres)
first defer T -> job_id=1, 1 job created.
orphan (delete jobs + null job_id) -> defer row job_id=NULL.
second defer SAME T -> returns None, **0 jobs created**. BUG PRESENT = True.

## Grader (grade.py) — VALIDATED with teeth on unfixed baseline
Structure-agnostic, outcome-based, 3 independent gates:
- **RECOVER**: after null-job_id orphan for T, fresh defer must create a job.
  Baseline: FAIL (recover_jobs=0). ← core bug.
- **DEDUPE**: two defers of T with a VALID alive row must create AT MOST 1 job.
  Baseline: PASS (dedupe_jobs=1). ← guards against a lazy "always recreate" fix.
- **DANGLING**: after a defer row whose job_id points to a DELETED job, fresh
  defer must create a job. Baseline: FAIL (dangling_jobs=0). ← 2nd trigger.
Baseline verdict: FAIL. A do-nothing fix fails RECOVER+DANGLING; an always-recreate
fix fails DEDUPE. Only a correct fix passes all three.

## Four-quality assessment
1. Reproducible ✓ (real PG, deterministic).
2. Novel ✓ (open issue, no PR).
3. Difficult — probing (blind fix attempts in progress).
4. Divergent — probing. Fix space is WIDE with real trade-offs:
   - rewrite SQL fn to be atomic (create job first, then insert defer w/ job_id)
   - make ON CONFLICT null-aware (re-run when existing row has null/dangling job_id)
   - add startup/periodic cleanup of orphaned rows (Python worker layer)
   - FK ON DELETE handling for the delete_jobs='successful' path
   Different models plausibly land in different spots → divergence candidate.

## Divergence probe — RESULT: CONVERGENT (2026-08-03)
2 independent blind (symptom-only, no issue#, no git history) fix attempts on
/tmp/hunt2-proc-div{1,2} (dbs proc_div{1,2}). Graded through grade.py.

| attempt | approach                                                          | verdict |
|---------|-------------------------------------------------------------------|---------|
| 1 | SQL fn: on-conflict, reclaim `job_id IS NULL` row FOR UPDATE, re-create | PASS (3/3) |
| 2 | SQL fn: same, +explicit dangling-job EXISTS check +migration file       | PASS (3/3) |

BOTH found the SAME function (`procrastinate_defer_periodic_job_v2`), SAME
mechanism (unlink-trigger nulls job_id; ON CONFLICT DO NOTHING conflates
stale/live), SAME fix shape (on-conflict FOR UPDATE reclaim + re-create else
skip). Difference is cosmetic (attempt 2 added a migration + explicit dangling
branch — but grader's DANGLING gate passes for attempt 1 too, since the unlink
trigger already nulls job_id).

VERDICT: difficulty OK (both needed real diagnosis, ~3.5-4min agent time / would
be longer for a full model), but **divergence WEAK on approach** — same failure
mode as #431 and the 4 shipped tasks. Wide theoretical fix space did NOT produce
approach divergence; both models beelined the cleanest SQL fix.

DISPOSITION: BANK as ranked fallback #2 (behind #431). May diverge cross-model on
time-to-fix. Grader + repro validated & preserved here. Do NOT ship yet.
