"""
Objective grader for dramatiq issue #431 (delayed-message duplication).

ONE symptom (a delayed job runs more than once), but a correct fix must satisfy
TWO OPPOSING gates — this is what makes the task discriminating:

  DUP  gate: a delayed message whose in-memory-holding worker is declared dead
             and requeued by maintenance must be promoted onto the real queue
             AT MOST ONCE (no duplicate execution).
  LOSS gate: a delayed message whose holding worker actually CRASHES before eta
             must still be promoted EXACTLY ONCE (never dropped).

The trap: the obvious fix (ack the delayed message before promoting it) passes
DUP but FAILS LOSS. A fix that stops requeueing delayed messages passes DUP but
FAILS LOSS. Only a fix that makes promotion atomic+idempotent passes BOTH.

Grader is approach-agnostic: it drives the REAL ConsumerThread methods
(handle_message / handle_delayed_messages / post_process_message) against real
Redis and counts promotions on the real queue. It never inspects fix internals.

Usage:
  python grade.py --pkg /path/to/dramatiq/repo/root [--db 13]
  (--pkg is the repo root that contains the `dramatiq` package to grade.)

Prints one JSON line. Exit 0 iff BOTH gates pass.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time


def _load_dramatiq(pkg_root: str):
    # Put the candidate repo first so its `dramatiq` shadows any installed one.
    sys.path.insert(0, pkg_root)
    for mod in list(sys.modules):
        if mod == "dramatiq" or mod.startswith("dramatiq."):
            del sys.modules[mod]
    import dramatiq  # noqa: F401
    return dramatiq


def _run_scenario(pkg_root: str, db: int, ns: str, crash_before_eta: bool) -> int:
    """Return promotions onto the real queue. crash_before_eta=False models the
    DUP scenario (A silent-but-alive, still holds copy); True models the LOSS
    scenario (A actually gone)."""
    import redis as _redis
    _load_dramatiq(pkg_root)
    from dramatiq.brokers.redis import RedisBroker
    from dramatiq.common import current_millis, dq_name
    from dramatiq.message import Message
    from dramatiq import worker as worker_mod

    HB = 2000
    client = _redis.Redis(host="127.0.0.1", port=6379, db=db)
    client.flushdb()

    def mk(bid):
        b = RedisBroker(client=_redis.Redis(host="127.0.0.1", port=6379, db=db),
                        namespace=ns, heartbeat_timeout=HB)
        b.broker_id = bid
        return b

    class Harness(worker_mod.ConsumerThread):
        # Subclass the REAL ConsumerThread so we inherit EVERY method the
        # candidate fix defines (e.g. a new promote_delayed_message / promote),
        # not a hardcoded subset. This keeps the grader structure-agnostic:
        # whatever code path the fix routes promotion through, it exists here.
        def __init__(self, broker, dq):
            self.broker = broker
            self.queue_name = dq
            self.delay_queue = worker_mod.PriorityQueue()
            self.consumer = broker.consume(queue_name=dq, prefetch=1000)
            self.logger = worker_mod.get_logger(__name__, "Harness")
            # NOTE: intentionally do NOT call super().__init__ — we drive the
            # message-handling methods directly rather than run the thread loop.
        def fetch(self):
            for _ in range(100):
                m = next(self.consumer)
                if m is None:
                    break
                self.handle_message(m)

    A = mk("worker-A"); B = mk("worker-B")
    q = "default"; dq = dq_name(q)
    A.declare_queue(q); B.declare_queue(q)

    delay_ms = 1400
    A.enqueue(Message(queue_name=q, actor_name="do_work", args=(), kwargs={}, options={}),
              delay=delay_ms)

    a = Harness(A, dq)
    a.fetch()

    if crash_before_eta:
        # A dies: drop its in-memory copy entirely; it never promotes.
        del a
        a = None

    # A declared dead.
    client.zadd(f"{ns}:__heartbeats__", {b"worker-A": current_millis() - 10 * HB})

    # B runs maintenance during fetch -> requeues A's waiting message; B holds it.
    B.maintenance_chance = 1_000_000
    b = Harness(B, dq)
    b.fetch()

    # eta passes -> promote from whoever still holds a copy.
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if a is not None:
            a.handle_delayed_messages()
        b.handle_delayed_messages()
        drained = (a is None or a.delay_queue.qsize() == 0) and b.delay_queue.qsize() == 0
        if drained:
            # give a beat for the last promote to land
            time.sleep(0.1)
            break
        time.sleep(0.1)

    return client.llen(f"{ns}:{q}".encode())


def grade(pkg_root: str, db: int) -> dict:
    dup_promotions = _run_scenario(pkg_root, db, "grade431_dup", crash_before_eta=False)
    loss_promotions = _run_scenario(pkg_root, db, "grade431_loss", crash_before_eta=True)

    dup_pass = dup_promotions == 1          # exactly once (not 2 = duplicate)
    loss_pass = loss_promotions == 1        # exactly once (not 0 = lost)
    return {
        "pkg": pkg_root,
        "dup_promotions": dup_promotions,
        "loss_promotions": loss_promotions,
        "DUP_gate": "PASS" if dup_pass else "FAIL",
        "LOSS_gate": "PASS" if loss_pass else "FAIL",
        "verdict": "PASS" if (dup_pass and loss_pass) else "FAIL",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", required=True, help="repo root containing the dramatiq package")
    ap.add_argument("--db", type=int, default=13)
    args = ap.parse_args()
    result = grade(args.pkg, args.db)
    print(json.dumps(result, sort_keys=True))
    sys.exit(0 if result["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
