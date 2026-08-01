"""End-to-end tests for :mod:`harnesses.layer1_repetition` and
:mod:`harnesses.layer2_lifecycle` against real FastAPI.

Rule 9: planted-lifecycle-leak fixture from Chunk 7 is reused here — the
harness composes plugin + runner + reporter and the invariant must fire
deterministically across 10 replays.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI

from core.reporter import to_json
from harnesses.layer1_repetition import run_layer1_repetition
from harnesses.layer2_lifecycle import run_layer2_lifecycle
from plugins.fastapi import FastAPIPlugin

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="harness metrics require Linux /proc",
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

_LEAK_SINK: list[int] = []


def _reset_leak_sink() -> None:
    _LEAK_SINK.clear()


def _clean_app_factory() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        _LEAK_SINK.append(1)
        try:
            yield
        finally:
            _LEAK_SINK.pop()

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    def _root() -> dict[str, bool]:
        return {"ok": True}

    return app


_FD_LEAK: list[object] = []


def _reset_fd_leak() -> None:
    while _FD_LEAK:
        obj = _FD_LEAK.pop()
        close = getattr(obj, "close", None)
        if callable(close):
            close()


def _leaky_lifespan_factory() -> FastAPI:
    """FastAPI app whose lifespan leaks a real file descriptor per build.

    ``_FD_LEAK`` retains a reference to the open file across shutdown, so
    the FD stays open and ``FdReturnToBaseline`` (part of the Layer-2
    default registry) detects the drift. This exercises the *default*
    invariants, which is the true user-facing promise of the harness.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Open one FD; retain it in a module-global list so shutdown cannot
        # reclaim it. This is the exact bug pattern the FD invariant exists
        # to surface.
        f = open("/dev/null", "rb")  # noqa: SIM115, PTH123 — intentional leak
        _FD_LEAK.append(f)
        yield
        # Bug: no cleanup.

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    def _root() -> dict[str, bool]:
        return {"ok": True}

    return app


def _route_added_each_build_factory() -> FastAPI:
    """Every ``build_app`` call adds one more decorated route than the last.

    Uses a module-level counter to plant a route-signature drift that
    Layer 2's ``RouteRegistryStable`` must catch.
    """

    _route_added_each_build_factory._counter = (  # type: ignore[attr-defined]
        getattr(_route_added_each_build_factory, "_counter", 0) + 1
    )
    counter: int = _route_added_each_build_factory._counter  # type: ignore[attr-defined]

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    def _root() -> dict[str, bool]:
        return {"ok": True}

    # Add exactly ``counter`` extra routes so each build has a different sig.
    for i in range(counter):
        # Fresh function per iteration keeps FastAPI happy.
        def _extra(i: int = i) -> dict[str, int]:
            return {"i": i}

        app.add_api_route(f"/extra_{i}", _extra, methods=["GET"])
    return app


def _reset_route_counter() -> None:
    if hasattr(_route_added_each_build_factory, "_counter"):
        delattr(_route_added_each_build_factory, "_counter")


def _one_probe_request(client: object) -> None:
    # ``client`` is a TestClient; get / and discard the response body.
    r = client.get("/")  # type: ignore[attr-defined]
    assert r.status_code == 200


def _fastapi_route_signature(app: FastAPI) -> tuple[str, ...]:
    # Sorted route-signature strings for deterministic comparison.
    sigs: list[str] = []
    for route in app.router.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if methods is None or path is None:
            continue
        for m in methods:
            sigs.append(f"{m} {path}")
    return tuple(sorted(sigs))


# ---------------------------------------------------------------------------
# Layer 1 — repetition.
# ---------------------------------------------------------------------------


class TestLayer1Repetition:
    def test_clean_app_20_requests_report_success(self) -> None:
        _reset_leak_sink()
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)
        report = run_layer1_repetition(
            plugin=plugin,
            request_callable=_one_probe_request,
            iterations=20,
            target_commit="test",
        )
        assert report.result.success is True
        assert report.result.iterations_completed == 20
        assert report.result.violations == ()

    def test_report_serialises_to_bytes(self) -> None:
        _reset_leak_sink()
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)
        report = run_layer1_repetition(
            plugin=plugin,
            request_callable=_one_probe_request,
            iterations=5,
            target_commit="test",
        )
        payload = json.loads(to_json(report))
        assert payload["metadata"]["target"] == "fastapi"
        assert payload["result"]["success"] is True

    def test_iterations_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            run_layer1_repetition(
                plugin=FastAPIPlugin(app_factory=_clean_app_factory),
                request_callable=_one_probe_request,
                iterations=0,
                target_commit="test",
            )


# ---------------------------------------------------------------------------
# Layer 2 — lifecycle.
# ---------------------------------------------------------------------------


class TestLayer2Lifecycle:
    def test_clean_app_20_rounds_report_success(self) -> None:
        _reset_leak_sink()
        _reset_route_counter()
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)
        report = run_layer2_lifecycle(
            plugin=plugin,
            request_callable=_one_probe_request,
            route_signature_of=_fastapi_route_signature,
            rounds=20,
            target_commit="test",
        )
        assert report.result.success is True
        assert report.result.violations == ()

    def test_leaky_lifespan_caught_across_10_replays(self) -> None:
        outcomes: list[tuple[bool, int]] = []
        try:
            for _ in range(10):
                _reset_leak_sink()
                _reset_fd_leak()
                plugin = FastAPIPlugin(app_factory=_leaky_lifespan_factory)
                report = run_layer2_lifecycle(
                    plugin=plugin,
                    request_callable=_one_probe_request,
                    route_signature_of=_fastapi_route_signature,
                    rounds=20,
                    target_commit="test",
                )
                outcomes.append(
                    (
                        report.result.success,
                        any(
                            v.invariant_name == "fd_return_to_baseline"
                            for v in report.result.violations
                        ),
                    )
                )
        finally:
            _reset_fd_leak()  # release the leaked descriptors
        # Every replay must fail AND the FD invariant must fire.
        for success, fd_violation_seen in outcomes:
            assert success is False
            assert fd_violation_seen is True

    def test_route_drift_across_builds_is_caught(self) -> None:
        _reset_leak_sink()
        _reset_route_counter()
        plugin = FastAPIPlugin(app_factory=_route_added_each_build_factory)
        report = run_layer2_lifecycle(
            plugin=plugin,
            request_callable=_one_probe_request,
            route_signature_of=_fastapi_route_signature,
            rounds=5,
            target_commit="test",
        )
        # Every subsequent build adds a route, so the invariant fires.
        assert report.result.success is False
        # Find the route_registry_stable violation.
        names = [v.invariant_name for v in report.result.violations]
        assert "route_registry_stable" in names

    def test_rounds_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            run_layer2_lifecycle(
                plugin=FastAPIPlugin(app_factory=_clean_app_factory),
                request_callable=_one_probe_request,
                route_signature_of=_fastapi_route_signature,
                rounds=0,
                target_commit="test",
            )
