"""
Structure-agnostic outcome grader for procrastinate #1495.

Measures the BUG'S IMPACT, not the fix's shape: after an orphaned
procrastinate_periodic_defers row exists for timestamp T (job_id NULL, or the
referenced job deleted), can the periodic task for T still be deferred?

Three independent gates (>=3 criteria, per reviewer policy):

  RECOVER gate: after a null-job_id orphan for T, a fresh defer for T must
                create a real job (job_id not null AND a row in procrastinate_jobs).
                This is the core bug — the naive "already deferred" dedupe loses it.

  DEDUPE gate:  the fix must NOT regress the legitimate dedupe — deferring the
                SAME T twice when a VALID (job_id present, job alive) row already
                exists must create AT MOST ONE job. (A fix that just always
                re-creates passes RECOVER but breaks idempotency.)

  DANGLING gate: after a defer row whose job_id points to a DELETED job (the
                delete_jobs='successful' path), a fresh defer for T must again
                create a real job (same silent-loss class, different trigger).

Grader drives the REAL app.job_manager.defer_periodic_job against real Postgres
and inspects only the public tables. It never calls fix internals, so any fix
shape (SQL fn rewrite, startup cleanup, null-aware conflict, migration) is scored
purely by outcome.

Usage: python hunt2-proc-grade.py --pkg /path/to/procrastinate/repo --db proc_div1
Prints one JSON line; exit 0 iff all three gates pass.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _load(pkg_root: str):
    sys.path.insert(0, pkg_root)
    for m in list(sys.modules):
        if m == "procrastinate" or m.startswith("procrastinate."):
            del sys.modules[m]
    import procrastinate  # noqa
    return procrastinate


async def _fresh_app(procrastinate, dsn):
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn))
    await app.open_async()
    await app.connector.execute_query_async(
        "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    )
    await app.schema_manager.apply_schema_async()
    return app


def _mkjob(procrastinate, T):
    from procrastinate.jobs import Job
    return Job(task_name="mytask", queue="default", lock=None,
              queueing_lock=None, task_kwargs={"timestamp": T})


async def _njobs(app):
    rows = await app.connector.execute_query_all_async(
        "SELECT id FROM procrastinate_jobs")
    return len(rows)


async def grade(pkg_root: str, dsn: str) -> dict:
    procrastinate = _load(pkg_root)
    Tbase = 1_700_000_000

    # ---------- RECOVER gate ----------
    app = await _fresh_app(procrastinate, dsn)
    T = Tbase
    await app.job_manager.defer_periodic_job(job=_mkjob(procrastinate, T),
                                             periodic_id="p1", defer_timestamp=T)
    # orphan: delete jobs, null the job_id
    await app.connector.execute_query_async("DELETE FROM procrastinate_jobs")
    await app.connector.execute_query_async(
        "UPDATE procrastinate_periodic_defers SET job_id = NULL")
    jid = await app.job_manager.defer_periodic_job(job=_mkjob(procrastinate, T),
                                                   periodic_id="p1", defer_timestamp=T)
    recover_jobs = await _njobs(app)
    recover_pass = (jid is not None) and (recover_jobs >= 1)
    await app.close_async()

    # ---------- DEDUPE gate ----------
    app = await _fresh_app(procrastinate, dsn)
    T = Tbase + 100
    await app.job_manager.defer_periodic_job(job=_mkjob(procrastinate, T),
                                             periodic_id="p1", defer_timestamp=T)
    # second defer with a VALID existing row (job alive) — must NOT duplicate
    await app.job_manager.defer_periodic_job(job=_mkjob(procrastinate, T),
                                             periodic_id="p1", defer_timestamp=T)
    dedupe_jobs = await _njobs(app)
    dedupe_pass = dedupe_jobs == 1
    await app.close_async()

    # ---------- DANGLING gate ----------
    app = await _fresh_app(procrastinate, dsn)
    T = Tbase + 200
    await app.job_manager.defer_periodic_job(job=_mkjob(procrastinate, T),
                                             periodic_id="p1", defer_timestamp=T)
    # dangling: job_id still set but the referenced job is gone
    await app.connector.execute_query_async("DELETE FROM procrastinate_jobs")
    jid2 = await app.job_manager.defer_periodic_job(job=_mkjob(procrastinate, T),
                                                    periodic_id="p1", defer_timestamp=T)
    dangling_jobs = await _njobs(app)
    dangling_pass = (jid2 is not None) and (dangling_jobs >= 1)
    await app.close_async()

    verdict = recover_pass and dedupe_pass and dangling_pass
    return {
        "pkg": pkg_root,
        "RECOVER_gate": "PASS" if recover_pass else "FAIL",
        "DEDUPE_gate": "PASS" if dedupe_pass else "FAIL",
        "DANGLING_gate": "PASS" if dangling_pass else "FAIL",
        "recover_jobs": recover_jobs,
        "dedupe_jobs": dedupe_jobs,
        "dangling_jobs": dangling_jobs,
        "verdict": "PASS" if verdict else "FAIL",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", required=True)
    ap.add_argument("--db", required=True, help="database name")
    args = ap.parse_args()
    dsn = f"user=jeff host=/var/run/postgresql dbname={args.db}"
    result = asyncio.run(grade(args.pkg, dsn))
    print(json.dumps(result, sort_keys=True))
    sys.exit(0 if result["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
