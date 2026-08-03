"""
Authoritative, approach-AGNOSTIC grader for dramatiq issue #845.

Unlike measure.py (which stresses the threading primitive directly), this boots a
REAL dramatiq Worker with the REAL TimeLimit middleware and sends an actor that
logs heavily and overruns its time limit. The TimeLimit middleware fires
`raise_thread_exception(tid, TimeLimitExceeded)` at the worker thread exactly as
in production. Whatever a candidate fix does to make that safe — install a guard
at boot, change the primitive, wrap logging, anything — is activated through the
normal worker lifecycle. The grader never calls into fix internals, so it scores
ANY fix shape purely by outcome.

Metric per trial: after the actor is interrupted mid-logging, can the MAIN
process still acquire the root logging handler's lock within a timeout? If not,
the interrupt orphaned it -> FREEZE. freeze_rate = freezes / trials.

Feature-preservation guard: the TimeLimit interrupt must actually fire (we assert
the actor was interrupted, not allowed to run to completion) — a fix that just
disables time limits would trivially avoid freezes but fails this.

Run:  python measure_worker.py [--trials N]
Prints one JSON summary line (the graded artifact). Exit 0.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass, asdict


@dataclass
class WorkerTrial:
    froze: bool
    interrupted: bool          # the actor was actually cut off by the time limit
    completed_normally: bool   # actor finished before the limit (should be rare)


def _install_lockable_handler() -> logging.Handler:
    """Attach a real handler (with a real lock) to the logger the actor uses,
    so an interrupt landing in acquire()->release() can orphan it."""
    h = logging.Handler()
    h.emit = lambda record: None  # type: ignore[method-assign]
    logging.getLogger("m845_actor").handlers = [h]
    logging.getLogger("m845_actor").propagate = False
    logging.getLogger("m845_actor").setLevel(logging.INFO)
    return h


def _one_trial(dramatiq, time_limit_ms: int, acquire_timeout: float) -> WorkerTrial:
    from dramatiq.brokers.stub import StubBroker
    from dramatiq.middleware import TimeLimit

    handler = _install_lockable_handler()
    actor_log = logging.getLogger("m845_actor")

    # DEFAULT middleware stack (Retries etc.) + TimeLimit. Replacing the stack
    # leaves max_retries undefined and the actor never registers.
    broker = StubBroker()
    broker.add_middleware(TimeLimit(time_limit=time_limit_ms, interval=50))
    broker.emit_after("process_boot")
    dramatiq.set_broker(broker)

    state = {"interrupted": False, "completed": False}

    @dramatiq.actor(max_retries=0, time_limit=time_limit_ms)
    def spinner():
        try:
            deadline = time.monotonic() + (time_limit_ms / 1000.0) * 6
            while time.monotonic() < deadline:
                # Heavy logging: takes handler.lock every iteration -> the
                # acquire->release window the async TimeLimitExceeded can hit.
                actor_log.info("tick")
        except BaseException:
            # TimeLimitExceeded (or anything) landed. If it hit the lock window,
            # handler.lock is orphaned right now.
            state["interrupted"] = True
            raise
        else:
            state["completed"] = True

    worker = dramatiq.Worker(broker, worker_threads=1)
    worker.start()
    try:
        spinner.send()
        # Give the worker time to pick up, run past the limit, and be interrupted.
        # StubBroker.join re-raises the actor's exception (TimeLimitExceeded is the
        # EXPECTED outcome here) — that is success, not a harness error.
        try:
            broker.join(spinner.queue_name, timeout=int(time_limit_ms * 8))
        except BaseException:
            # TimeLimitExceeded subclasses Interrupt -> BaseException (not
            # Exception). Its propagation IS the expected outcome, not an error.
            pass
        # Probe the (possibly orphaned) handler lock from the main thread.
        got = handler.lock.acquire(timeout=acquire_timeout)
        froze = not got
        if got:
            handler.lock.release()
    finally:
        try:
            worker.stop(timeout=2000)
        except Exception:
            pass

    return WorkerTrial(
        froze=froze,
        interrupted=state["interrupted"],
        completed_normally=state["completed"],
    )


def measure(trials: int, time_limit_ms: int, acquire_timeout: float) -> dict:
    import dramatiq
    results: list[WorkerTrial] = []
    for _ in range(trials):
        try:
            results.append(_one_trial(dramatiq, time_limit_ms, acquire_timeout))
        except BaseException as e:  # a trial that errored out is not a freeze; record it
            results.append(WorkerTrial(froze=False, interrupted=False, completed_normally=False))
            print(f"trial error: {e!r}", file=sys.stderr)

    freezes = sum(1 for r in results if r.froze)
    interrupted = sum(1 for r in results if r.interrupted)
    return {
        "mode": "worker",
        "trials": trials,
        "time_limit_ms": time_limit_ms,
        "freezes": freezes,
        "freeze_rate": round(freezes / trials, 4) if trials else 0.0,
        "trials_interrupted": interrupted,
        # feature-preservation: the time limit must actually fire in most trials
        "interrupts_fired_ok": interrupted >= max(1, trials // 2),
        "per_trial": [asdict(r) for r in results],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=15)
    ap.add_argument("--time-limit-ms", type=int, default=300)
    ap.add_argument("--acquire-timeout", type=float, default=1.5)
    args = ap.parse_args()
    summary = measure(args.trials, args.time_limit_ms, args.acquire_timeout)
    print(json.dumps({k: v for k, v in summary.items() if k != "per_trial"}, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
