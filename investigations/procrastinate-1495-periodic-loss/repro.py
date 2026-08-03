"""
Repro for procrastinate #1495 — orphaned periodic_defers row (null job_id)
permanently stops a periodic task from being deferred.

Chain:
  1. defer_periodic_job(ts=T) inserts a periodic_defers row and creates a job.
  2. Simulate a crash BETWEEN insert and job creation: leave a row for ts=T
     with job_id = NULL (exactly what an interrupted defer, or delete_jobs=
     'successful', leaves behind).
  3. On the next scheduling pass for the SAME timestamp T, defer_periodic_job
     hits ON CONFLICT DO NOTHING -> returns NULL -> the deferrer logs
     "already deferred" and NEVER creates the job.

PASS-CONDITION FOR THE REPRO (bug present): after step 3, there is NO job in
procrastinate_jobs for timestamp T, even though the periodic task should run.
"""
import asyncio
import sys

from procrastinate import App, PsycopgConnector, builtin_tasks  # noqa
from procrastinate.jobs import Job

DSN = "postgresql://jeff@/proc1495?host=/var/run/postgresql"


async def main() -> int:
    app = App(connector=PsycopgConnector(conninfo=DSN))
    async with app.open_async():
        # fresh schema
        await app.connector.execute_query_async(
            "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
        )
        await app.schema_manager.apply_schema_async()

        mgr = app.job_manager
        T = 1_700_000_000  # fixed periodic timestamp

        job = Job(task_name="mytask", queue="default", lock=None,
                  queueing_lock=None, task_kwargs={"timestamp": T})

        # --- First defer: works, creates a job.
        jid1 = await mgr.defer_periodic_job(job=job, periodic_id="p1", defer_timestamp=T)
        print(f"first defer_periodic_job(T={T}) -> job_id={jid1}")

        # Count jobs for T
        rows = await app.connector.execute_query_all_async(
            "SELECT id, task_name FROM procrastinate_jobs"
        )
        print(f"jobs after first defer: {rows}")

        # --- Simulate the orphan: wipe jobs (delete_jobs='successful' path) and
        #     NULL the job_id, leaving a dangling defer row for T.
        await app.connector.execute_query_async("DELETE FROM procrastinate_jobs")
        await app.connector.execute_query_async(
            "UPDATE procrastinate_periodic_defers SET job_id = NULL"
        )
        defers = await app.connector.execute_query_all_async(
            "SELECT id, task_name, periodic_id, defer_timestamp, job_id "
            "FROM procrastinate_periodic_defers"
        )
        print(f"orphaned defer rows (job_id NULL): {defers}")

        # --- Second defer for the SAME timestamp T (what a restarted worker does).
        jid2 = await mgr.defer_periodic_job(job=job, periodic_id="p1", defer_timestamp=T)
        print(f"second defer_periodic_job(T={T}) -> job_id={jid2}")

        jobs_after = await app.connector.execute_query_all_async(
            "SELECT id, task_name FROM procrastinate_jobs"
        )
        print(f"jobs after second defer: {jobs_after}")

        bug_present = (jid2 is None) and (len(jobs_after) == 0)
        print(f"\nBUG PRESENT (periodic task silently lost): {bug_present}")
        return 0 if bug_present else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
