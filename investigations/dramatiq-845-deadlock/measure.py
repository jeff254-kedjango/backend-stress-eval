"""
Outcome-based measurement harness for the dramatiq async-exception deadlock
(issue #845, HEAD 288dc265). Grader-agnostic to the *fix approach*: it measures
the BUG'S IMPACT (freeze frequency under stress) against whatever dramatiq is
importable, so it can score the unpatched baseline and any candidate fix on the
same scale.

The defect: dramatiq.threading.raise_thread_exception uses
PyThreadState_SetAsyncExc, which delivers the exception at an arbitrary bytecode
boundary. If it lands inside the window where a thread holds a non-reentrant lock
(here: a stdlib logging.Handler lock, the exact trigger in the report), the lock
is orphaned and any later acquirer blocks forever -> process deadlock.

Because delivery timing is probabilistic, a single trial is not a reliable gate.
We run N independent TRIALS; each trial:
  1. starts a victim thread that loops doing `logger.info(...)` (acquires/releases
     the handler lock every iteration),
  2. from the main thread, repeatedly fires raise_thread_exception at the victim
     to interrupt it at a random point,
  3. after the victim is asked to stop, the main thread tries to acquire the SAME
     handler lock within a timeout.
A trial is a FREEZE if that acquire times out (the victim died holding the lock).

Output: freeze_rate = freezes / trials. Baseline (unpatched) has a high rate;
a correct fix drives it toward 0 WITHOUT disabling interruption (we separately
assert interrupts are still delivered -> feature preserved).

Run:  python measure.py [--trials N] [--iters-per-trial M]
Exit 0 always; the JSON on stdout is the graded artifact.
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
class TrialResult:
    froze: bool
    interrupts_delivered: int
    victim_exited: bool


def _make_handler() -> logging.Handler:
    # A real logging.Handler with a real non-reentrant lock, writing nowhere
    # expensive. The lock (Handler.lock) is what gets orphaned.
    h = logging.Handler()
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit = lambda record: None  # type: ignore[method-assign]  # no I/O; keep the lock window only
    return h


def _run_trial(raise_thread_exception, interrupt_exc, iters: int,
               acquire_timeout: float) -> TrialResult:
    """One trial = up to `iters` interrupt ATTEMPTS against a logging victim.

    Mirrors the proven per-attempt recipe: fire ONE interrupt, wait for the
    victim to actually observe it and die, then test whether the handler lock
    leaked. Restart the victim and retry until a freeze is seen or attempts run
    out. A trial FREEZES the first time the main thread cannot re-acquire the
    handler lock (victim died mid-critical-section, orphaning it).
    """
    handler = _make_handler()
    logger = logging.getLogger(f"trial-{threading.get_ident()}-{time.monotonic_ns()}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    delivered = 0

    def make_victim():
        stop = threading.Event()
        died = threading.Event()

        def victim() -> None:
            try:
                while not stop.is_set():
                    # logger.info takes handler.lock in acquire()/release():
                    # the vulnerable window for an async exception.
                    logger.info("x")
            except BaseException:
                # Interrupt landed. If inside the acquire->release window,
                # handler.lock is orphaned right now.
                pass
            finally:
                died.set()

        return stop, died, victim

    stop, died, victim = make_victim()
    t = threading.Thread(target=victim, daemon=True)
    t.start()
    time.sleep(0.02)

    for _ in range(iters):
        if not t.is_alive():
            # Respawn a victim so we keep having a live target to interrupt.
            stop, died, victim = make_victim()
            t = threading.Thread(target=victim, daemon=True)
            t.start()
            time.sleep(0.005)
            # If handler.lock was orphaned by the PREVIOUS victim, this new
            # victim's first logger.info() will itself block — detect that
            # rather than firing into a wedged target.
        tid = t.ident
        if tid is None:
            continue
        try:
            raise_thread_exception(tid, interrupt_exc)
            delivered += 1
        except Exception:
            pass

        if not died.wait(timeout=1.0):
            # Victim still running (interrupt not yet delivered) OR it wedged
            # holding the lock. Probe the lock to decide.
            got = handler.lock.acquire(timeout=acquire_timeout)
            if not got:
                return TrialResult(froze=True, interrupts_delivered=delivered,
                                   victim_exited=False)
            handler.lock.release()
            continue

        # Victim died. Did it leave the lock held?
        got = handler.lock.acquire(timeout=acquire_timeout)
        if not got:
            return TrialResult(froze=True, interrupts_delivered=delivered,
                               victim_exited=True)
        handler.lock.release()

    stop.set()
    return TrialResult(froze=False, interrupts_delivered=delivered,
                       victim_exited=True)


def measure(trials: int, iters: int, acquire_timeout: float) -> dict:
    # Import from whatever dramatiq is on sys.path (baseline or candidate).
    from dramatiq.threading import raise_thread_exception, Interrupt

    # A candidate fix may wire its protection into worker boot (the real
    # dramatiq lifecycle installs it there). Since this harness stresses the
    # threading primitive directly rather than booting a full worker, activate
    # any known install hook so we measure the fix's ACTUAL behavior, not the
    # dormant package. Baselines have no such hook -> no-op. This must stay
    # approach-agnostic: only call install entrypoints the fix chose to export.
    try:
        import dramatiq.threading as _dt
        for _hook in ("install_logging_interrupt_guard", "install_interrupt_guard"):
            _fn = getattr(_dt, _hook, None)
            if callable(_fn):
                _fn()
    except Exception:
        pass

    results: list[TrialResult] = []
    for _ in range(trials):
        results.append(_run_trial(raise_thread_exception, Interrupt, iters, acquire_timeout))

    freezes = sum(1 for r in results if r.froze)
    total_delivered = sum(r.interrupts_delivered for r in results)
    return {
        "trials": trials,
        "iters_per_trial": iters,
        "freezes": freezes,
        "freeze_rate": round(freezes / trials, 4) if trials else 0.0,
        "total_interrupts_delivered": total_delivered,
        # Feature-preservation signal: a fix that just stops delivering interrupts
        # would drive freeze_rate to 0 too -> this guards against that cheat.
        "interrupts_delivered_ok": total_delivered > 0,
        "per_trial": [asdict(r) for r in results],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--iters-per-trial", type=int, default=2000)
    ap.add_argument("--acquire-timeout", type=float, default=2.0)
    args = ap.parse_args()
    summary = measure(args.trials, args.iters_per_trial, args.acquire_timeout)
    # Drop per-trial detail from stdout summary line; keep it in --verbose later.
    summary_out = {k: v for k, v in summary.items() if k != "per_trial"}
    print(json.dumps(summary_out, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
