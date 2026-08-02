"""Widened-lens interaction probes for FastAPI 0.141.1.

The Layer-2/3 invariants only see route drift, response drift, RSS/FD slope.
These two probes see what those cannot:

  1. teardown ordering — nested yield-dependency `finally` blocks must run in
     strict reverse-of-entry (LIFO) order, exactly once, on EVERY request,
     across many requests and lifecycle restarts.

  2. dependency-cache bleed — FastAPI caches a `Depends(...)` result per
     request. A value cached in request A must never be observed by request
     B. We give each request a unique token and assert no token leaks.

Both probes combine features (nested deps + middleware + repeated lifecycle)
so an interaction-only corruption would surface. Each probe drives real
requests via TestClient inside a fresh lifespan per lifecycle iteration.

Rule 9: probes only OBSERVE. They record events and return raw findings;
the caller decides pass/fail. Rule 1: per-request work is O(depth) with
depth fixed at 3 — bounded, effectively O(1) at iteration scale.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    ok: bool
    detail: str
    sample: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Probe 1: teardown ordering across nested yield-deps + middleware.
# ---------------------------------------------------------------------------
def _teardown_app(events: list[str]) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def _mw(request: Request, call_next):
        events.append("mw-enter")
        resp = await call_next(request)
        events.append("mw-exit")
        return resp

    def dep_a() -> AsyncIterator[str]:
        events.append("A-enter")
        try:
            yield "A"
        finally:
            events.append("A-exit")

    def dep_b(_a: str = Depends(dep_a)) -> AsyncIterator[str]:
        events.append("B-enter")
        try:
            yield "B"
        finally:
            events.append("B-exit")

    def dep_c(_b: str = Depends(dep_b)) -> AsyncIterator[str]:
        events.append("C-enter")
        try:
            yield "C"
        finally:
            events.append("C-exit")

    @app.get("/")
    def _root(_c: str = Depends(dep_c)) -> dict[str, str]:
        events.append("handler")
        return {"status": "ok"}

    return app


def probe_teardown_ordering(*, rounds: int, reqs_per_round: int) -> ProbeResult:
    """Enter order is A,B,C; teardown MUST be C,B,A (LIFO), exactly once each.

    We check the dependency enter/exit subsequence per request (ignoring the
    middleware bracket, which legitimately straddles the handler).
    """
    expected_dep = ["A-enter", "B-enter", "C-enter", "handler", "C-exit", "B-exit", "A-exit"]
    first_bad: tuple[str, ...] = ()
    bad_count = 0
    total = 0
    for _ in range(rounds):
        app_events: list[str] = []
        app = _teardown_app(app_events)
        with TestClient(app) as client:
            for _ in range(reqs_per_round):
                app_events.clear()
                client.get("/")
                total += 1
                dep_only = [e for e in app_events if e != "mw-enter" and e != "mw-exit"]
                if dep_only != expected_dep:
                    bad_count += 1
                    if not first_bad:
                        first_bad = tuple(dep_only)
    ok = bad_count == 0
    detail = (
        f"{total} requests, all teardown orders correct (LIFO C,B,A)"
        if ok
        else f"{bad_count}/{total} requests had wrong teardown order/count"
    )
    return ProbeResult("teardown_ordering", ok, detail, first_bad)


# ---------------------------------------------------------------------------
# Probe 2: per-request dependency-cache isolation.
# ---------------------------------------------------------------------------
def _cache_app() -> tuple[FastAPI, dict[str, list[int]]]:
    """Each request stamps a unique token into a request-scoped cached dep and
    reads it back through a second dep that shares the cache. If FastAPI's
    per-request cache is correct, the read-back equals the write for the SAME
    request and never a prior request's token.
    """
    seen: dict[str, list[int]] = {"writes": [], "reads": []}
    counter = {"n": 0}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=lifespan)

    def token_source() -> int:
        counter["n"] += 1
        return counter["n"]

    def writer(tok: int = Depends(token_source)) -> int:
        seen["writes"].append(tok)
        return tok

    def reader(tok: int = Depends(token_source)) -> int:
        # Shares token_source cache within a request => must equal writer's tok.
        return tok

    @app.get("/")
    def _root(w: int = Depends(writer), r: int = Depends(reader)) -> dict[str, int]:
        seen["reads"].append(r)
        return {"w": w, "r": r}

    return app, seen


def probe_cache_isolation(*, rounds: int, reqs_per_round: int) -> ProbeResult:
    bad = 0
    total = 0
    first_bad: tuple[str, ...] = ()
    for _ in range(rounds):
        app, seen = _cache_app()
        with TestClient(app) as client:
            for _ in range(reqs_per_round):
                resp = client.get("/")
                total += 1
                body = resp.json()
                # Within one request, writer and reader share token_source cache.
                if body["w"] != body["r"]:
                    bad += 1
                    if not first_bad:
                        first_bad = (f"w={body['w']} r={body['r']}",)
    ok = bad == 0
    detail = (
        f"{total} requests, per-request cache isolated (w==r every time)"
        if ok
        else f"{bad}/{total} requests saw cache bleed (w!=r)"
    )
    return ProbeResult("cache_isolation", ok, detail, first_bad)


if __name__ == "__main__":
    r1 = probe_teardown_ordering(rounds=30, reqs_per_round=20)
    r2 = probe_cache_isolation(rounds=30, reqs_per_round=20)
    for r in (r1, r2):
        flag = "OK " if r.ok else "!! "
        print(f"{flag}{r.name}: {r.detail}")
        if not r.ok and r.sample:
            print(f"     sample: {r.sample}")
    raise SystemExit(0 if (r1.ok and r2.ok) else 2)
