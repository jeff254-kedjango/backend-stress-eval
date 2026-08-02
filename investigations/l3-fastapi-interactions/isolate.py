"""Isolation test: is the yield_dep+streaming leak super-additive?

Rule 9 discipline: a higher combined slope is meaningless unless it exceeds
the sum of the parts. We measure four apps under the SAME harness, SAME
rounds, and compare per-iter RSS slope:

    baseline   : plain single-route
    stream_only: StreamingResponse, no dependency
    yield_only : yield-dependency, plain dict response
    combined   : yield-dependency feeding a StreamingResponse

Interaction leak <=> slope(combined) > slope(stream_only)+slope(yield_only)
minus slope(baseline) (the shared per-loop floor counted once).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse

from harnesses.layer2_lifecycle import run_layer2_lifecycle
from plugins.fastapi import FastAPIPlugin

_ROUNDS = 80


def _ls():
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    return lifespan


def baseline() -> FastAPI:
    app = FastAPI(lifespan=_ls())

    @app.get("/")
    def _r() -> dict[str, str]:
        return {"status": "ok"}

    return app


def stream_only() -> FastAPI:
    app = FastAPI(lifespan=_ls())

    @app.get("/")
    def _r() -> StreamingResponse:
        def gen():
            yield b'{"status":"ok"}'

        return StreamingResponse(gen(), media_type="application/json")

    return app


def yield_only() -> FastAPI:
    app = FastAPI(lifespan=_ls())

    def dep() -> AsyncIterator[dict[str, int]]:
        s = {"n": 1}
        try:
            yield s
        finally:
            s["n"] = 0

    @app.get("/")
    def _r(_d: dict[str, int] = Depends(dep)) -> dict[str, str]:
        return {"status": "ok"}

    return app


def combined() -> FastAPI:
    app = FastAPI(lifespan=_ls())

    def dep() -> AsyncIterator[dict[str, int]]:
        s = {"n": 1}
        try:
            yield s
        finally:
            s["n"] = 0

    @app.get("/")
    def _r(_d: dict[str, int] = Depends(dep)) -> StreamingResponse:
        def gen():
            yield b'{"status":"ok"}'

        return StreamingResponse(gen(), media_type="application/json")

    return app


def slope_of(factory) -> float:
    rep = run_layer2_lifecycle(
        plugin=FastAPIPlugin(app_factory=factory),
        request_callable=lambda c: c.get("/"),
        route_signature_of=FastAPIPlugin(app_factory=factory).route_signature,
        rounds=_ROUNDS,
        target_commit="isolate",
    )
    for v in rep.result.violations:
        if v.invariant_name == "rss_slope_bounded":
            # detail: "RSS grows +X KB/iter ..."
            return float(v.detail.split("+", 1)[1].split(" ", 1)[0])
    return 0.0


if __name__ == "__main__":
    b = slope_of(baseline)
    s = slope_of(stream_only)
    y = slope_of(yield_only)
    c = slope_of(combined)
    additive = (s - b) + (y - b)  # excess of each part over shared floor
    excess = (c - b) - additive
    print(f"baseline      slope = {b:8.3f} KB/iter")
    print(f"stream_only   slope = {s:8.3f} KB/iter  (excess {s-b:+.3f})")
    print(f"yield_only    slope = {y:8.3f} KB/iter  (excess {y-b:+.3f})")
    print(f"combined      slope = {c:8.3f} KB/iter  (excess {c-b:+.3f})")
    print(f"additive prediction (parts) = {b+additive:8.3f} KB/iter")
    print(f"super-additive excess       = {excess:+8.3f} KB/iter")
    verdict = "SUPER-ADDITIVE (interaction leak)" if excess > 2.0 else "additive/noise (reject)"
    print("VERDICT:", verdict)
