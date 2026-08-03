# dramatiq #845 — baseline measurement (grader validation)

Bug: async-exception deadlock. `dramatiq.threading.raise_thread_exception`
(PyThreadState_SetAsyncExc, threading.py:85) delivers Interrupt at an arbitrary
bytecode boundary; landing inside a logging.Handler acquire->release window
orphans the handler lock -> process-wide deadlock.

Repo Bogdanp/dramatiq @ 288dc2651e (dramatiq 2.2.0). Issue OPEN, no merged fix.

## Grader = measure.py (outcome-based, approach-agnostic)
Runs N trials; each fires interrupts at a thread logging through a real
logging.Handler, then checks whether the handler lock was orphaned (freeze).
Metric: freeze_rate = freezes/trials. Feature-preservation guard:
interrupts_delivered_ok must stay true (a fix that just stops delivering
interrupts would cheat freeze_rate to 0).

## Baseline (UNPATCHED) result
    freeze_rate = 1.0  (20/20 froze), 764 interrupts delivered.
Deterministic-enough at iters-per-trial=300. A correct fix must drive
freeze_rate toward 0 WHILE keeping interrupts_delivered_ok=true.

## Note
First harness version fired interrupts in a tight loop + single end-check ->
false 0.0 (under-measured). Correct recipe (per-attempt fire->wait-for-death->
probe-lock, respawn victim) matches the working probe repro. Rule 9.

## CORRECTION (grader validity)
First fixed-vs-unfixed run gave freeze_rate 1.0 for BOTH -> grader looked invalid.
Root cause (Rule 9): the probe's fix activates only via
`install_logging_interrupt_guard()`, called at WORKER BOOT (time_limit.py:89 /
shutdown.py:87). The bare-thread harness never boots a worker, so the guard was
dormant. With the guard installed manually: freeze_rate 1.0 -> 0.0, interrupts
still delivered (4500). Grader LOGIC is sound (distinguishes fixed/unfixed +
catches the disabled-interruption cheat).

## OPEN ISSUE — approach-agnostic activation
Hand-calling a named install hook biases the grader toward ONE fix's shape. A
different model may wire its fix differently. The AUTHORITATIVE grader must boot
a REAL dramatiq Worker under TimeLimit/Shutdown middleware and measure freezes
there, so it exercises whatever activation the model chose. bare-thread mode is
kept as a fast primitive-level check only. TODO: build worker-mode measure.

## FINAL VERDICT (2026-08-03): #845 NOT cleanly gradable — DEMOTED
Empirically established while building the worker-mode grader:
- Bare-thread freeze_rate 1.0 was an ARTIFACT: a raw threading.Lock held across a
  plain loop with NO try/finally. No real library writes this.
- stdlib logging.Handler.handle() releases the lock in a finally on unwind ->
  worker-mode freeze_rate 0.0 even with interrupt firing every trial (12/12).
- current loguru _handler.py:111-125 uses _protected_lock(): try/finally + a
  re-entrancy guard literally commented "deadlock avoided". Worker-mode: NOT frozen.
=> The PyThreadState_SetAsyncExc hazard is real but every realistic, current
   lock-holder defends against it. Reliable repro would require SHIPPING a
   deliberately-broken handler = partly synthetic bug. Not objectively gradable
   against stock deps. Same demotion category as arq #402 (grader caught it).

DECISION: proceed to dramatiq #431 (objective dup-vs-loss grader). #845 stays
banked; revisit only if a real stock dependency without lock try/finally surfaces.
