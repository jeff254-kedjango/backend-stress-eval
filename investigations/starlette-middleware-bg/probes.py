#!/usr/bin/env python3
"""Starlette hunt: BaseHTTPMiddleware + BackgroundTask interaction.

The FastAPI findings (investigations/l3-fastapi-interactions/findings.md)
named three surfaces the route/response/RSS invariants cannot see; one was
"exception-group swallowing in combined middleware+bg paths." Raw Starlette
is where that interaction is thinnest, so that's the hypothesis under test.

Rule 9: reproduce/measure BEFORE theorizing. Each probe is paired with a
TEETH check — we inject a KNOWN fault and assert the probe detects it. A
green probe whose teeth are unproven is worthless.

Probes:
  P1  bg_runs           — does a BackgroundTask scheduled behind
                          BaseHTTPMiddleware actually execute? (Known
                          historical hazard: BaseHTTPMiddleware's
                          streaming wrapper can drop response.background.)
  P2  bg_exc_visibility — if the background task raises, is the error
                          surfaced or silently swallowed?

Run:
  python probes.py            # real apps
  python probes.py --teeth    # inject faults, assert probes catch them
"""
from __future__ import annotations

import sys
import threading
from typing import Any

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

_HTTP_OK = 200


def _with_middleware(has_mw: bool, route_handler: Any) -> Starlette:
    async def _passthrough(request: Request, call_next: Any) -> Response:
        return await call_next(request)

    mw = [Middleware(BaseHTTPMiddleware, dispatch=_passthrough)] if has_mw else []
    return Starlette(routes=[Route("/", route_handler)], middleware=mw)


# ---------------------------------------------------------------------------
# P1 — does the background task run when behind BaseHTTPMiddleware?
# ---------------------------------------------------------------------------
def probe_bg_runs(*, has_middleware: bool, drop_background: bool = False) -> bool:
    """Return True iff the scheduled BackgroundTask actually executed.

    ``drop_background`` is the TEETH lever: when True the route schedules NO
    background task, so a correct probe MUST report False.
    """
    ran = threading.Event()

    async def _mark() -> None:
        ran.set()

    async def _handler(_request: Request) -> Response:
        bg = None if drop_background else BackgroundTask(_mark)
        return JSONResponse({"ok": True}, background=bg)

    app = _with_middleware(has_middleware, _handler)
    with TestClient(app) as c:
        r = c.get("/")
        if r.status_code != _HTTP_OK:
            raise RuntimeError(f"probe route returned {r.status_code}")
    return ran.is_set()


# ---------------------------------------------------------------------------
# P2 — is a raising background task's error surfaced or swallowed?
# ---------------------------------------------------------------------------
def probe_bg_exc_visibility(*, has_middleware: bool, raise_in_bg: bool = True) -> bool:
    """Return True iff a raising BackgroundTask surfaces (client sees error).

    TEETH lever ``raise_in_bg=False``: the task does not raise, so a correct
    probe MUST report False (nothing to surface).
    """

    async def _boom() -> None:
        if raise_in_bg:
            raise ValueError("bg-task-boom")

    async def _handler(_request: Request) -> Response:
        return JSONResponse({"ok": True}, background=BackgroundTask(_boom))

    app = _with_middleware(has_middleware, _handler)
    surfaced = False
    try:
        with TestClient(app, raise_server_exceptions=True) as c:
            c.get("/")
    except Exception:  # we're classifying whether it surfaced, not handling
        surfaced = True
    return surfaced


def _run_real() -> int:
    print("=== P1: does BackgroundTask run behind BaseHTTPMiddleware? ===")
    no_mw = probe_bg_runs(has_middleware=False)
    with_mw = probe_bg_runs(has_middleware=True)
    print(f"  bg ran (no middleware):   {no_mw}")
    print(f"  bg ran (with middleware): {with_mw}")
    p1_bug = no_mw and not with_mw
    print(f"  -> INTERACTION BUG: {p1_bug}  (bg dropped only when middleware present)")

    print("=== P2: is a raising BackgroundTask surfaced? ===")
    no_mw_s = probe_bg_exc_visibility(has_middleware=False)
    with_mw_s = probe_bg_exc_visibility(has_middleware=True)
    print(f"  surfaced (no middleware):   {no_mw_s}")
    print(f"  surfaced (with middleware): {with_mw_s}")
    p2_drift = no_mw_s != with_mw_s
    print(f"  -> DIVERGENCE (mw changes error visibility): {p2_drift}")

    return 1 if (p1_bug or p2_drift) else 0


def _run_teeth() -> int:
    print("=== TEETH: inject known faults, assert probes catch them ===")
    ok = True

    # P1 teeth: drop the background task -> probe MUST report it didn't run.
    dropped = probe_bg_runs(has_middleware=True, drop_background=True)
    t1 = dropped is False
    print(f"  P1 detects dropped bg (expect ran=False): ran={dropped}  PASS={t1}")
    ok &= t1

    # P2 teeth: task that does not raise -> probe MUST report not-surfaced.
    surfaced = probe_bg_exc_visibility(has_middleware=True, raise_in_bg=False)
    t2 = surfaced is False
    print(f"  P2 detects non-raising bg (expect surfaced=False): surfaced={surfaced}  PASS={t2}")
    ok &= t2

    print(f"TEETH: {'ALL PASS — probes are trustworthy' if ok else 'FAILED — probe is blind'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_run_teeth() if "--teeth" in sys.argv else _run_real())
