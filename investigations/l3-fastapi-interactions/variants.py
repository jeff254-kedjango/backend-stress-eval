"""Layer-3 interaction variant apps for FastAPI 0.141.1.

Each factory composes a *combination* of individually-correct features and
returns a single-route ("/") app so the harness's canonical probe + digest
+ route-signature invariants apply uniformly. The hunt: does any COMBINATION
drift a route set or a response digest across lifecycle iterations while each
feature alone stays stable?

Rule 9: this file only *builds* apps. Measurement is done by run_hunt.py.
No feature is exercised here that the plugin's probe (GET /) does not hit,
so every variant is comparable under the same invariant.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask


def _lifespan_noop():
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    return lifespan


def v_yield_dep_plus_streaming() -> FastAPI:
    """Yield-dependency (with teardown) feeding a StreamingResponse.

    Interaction under test: does the generator dependency's teardown run
    relative to the streaming body, and does anything leak into the next
    lifecycle iteration's route/response?
    """
    app = FastAPI(lifespan=_lifespan_noop())

    def dep() -> AsyncIterator[dict[str, int]]:
        state = {"n": 1}
        try:
            yield state
        finally:
            state["n"] = 0

    @app.get("/")
    def _root(d: dict[str, int] = Depends(dep)) -> StreamingResponse:
        def gen():
            yield b'{"status":"ok"}'

        return StreamingResponse(gen(), media_type="application/json")

    return app


def v_middleware_plus_background() -> FastAPI:
    """HTTP middleware wrapping an endpoint that schedules a BackgroundTask."""
    app = FastAPI(lifespan=_lifespan_noop())

    @app.middleware("http")
    async def _mw(request: Request, call_next: Any):
        resp = await call_next(request)
        resp.headers["x-harness"] = "on"
        return resp

    def _cleanup() -> None:
        return None

    from fastapi.responses import JSONResponse

    @app.get("/")
    def _root() -> JSONResponse:
        return JSONResponse({"status": "ok"}, background=BackgroundTask(_cleanup))

    return app


def v_nested_yield_deps() -> FastAPI:
    """Two nested yield-dependencies; teardown ordering is the interaction."""
    app = FastAPI(lifespan=_lifespan_noop())
    order: list[str] = []

    def outer() -> AsyncIterator[str]:
        order.append("outer-enter")
        try:
            yield "outer"
        finally:
            order.append("outer-exit")

    def inner(_o: str = Depends(outer)) -> AsyncIterator[str]:
        order.append("inner-enter")
        try:
            yield "inner"
        finally:
            order.append("inner-exit")

    @app.get("/")
    def _root(_i: str = Depends(inner)) -> dict[str, str]:
        return {"status": "ok"}

    return app


VARIANTS: tuple[tuple[str, Any], ...] = (
    ("yield_dep+streaming", v_yield_dep_plus_streaming),
    ("middleware+background", v_middleware_plus_background),
    ("nested_yield_deps", v_nested_yield_deps),
)
