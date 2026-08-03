Work within this directory only.

We schedule a lot of jobs to run a little later — "send this in thirty seconds,"
that kind of thing — and we run more than one worker so the load is shared. Most
of the time it is fine. But we have two intermittent failures we cannot pin
down, and they seem to be two faces of the same problem.

The first: every so often one of these delayed jobs runs **twice**. Two
different workers each carry it out, in full, a moment apart. Nothing errors,
nothing logs a warning — the job simply happens twice, and for anything that
isn't safe to repeat (charging a card, sending a message) that is a real
problem. It only shows up when a worker becomes unhealthy or drops out around
the time one of its delayed jobs is due; a colleague tried to fix it by having a
worker let go of the delayed job sooner.

The second appeared right after that change: now, once in a while, a delayed job
**never runs at all**. Same conditions — a worker goes down while it is sitting
on a job that hasn't come due yet — but instead of running twice the job is
silently lost. No error, no trace of it, it is just gone. So we have gone from
"sometimes twice" to "sometimes twice, sometimes never," and we cannot get both
to behave at once: every attempt trades one failure for the other.

The setup is a throwaway local instance and a single third-party dependency that
handles the scheduling and hand-off between workers. Find why a delayed job that
is in flight when a worker drops out can end up run more than once **or** lost
entirely, and fix it so that each such job runs **exactly once** — never
duplicated and never dropped — regardless of a worker becoming unhealthy or
crashing while holding it. Jobs that are never caught in one of these worker
hand-offs must keep working exactly as before, and the fix must address where
the problem actually originates rather than masking it in our own code.
