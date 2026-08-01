"""Tests for :mod:`harnesses.layer3_variants` and :mod:`harnesses.layer4_sequence`.

Rule 9: planted-bug fixtures. Layer-4 uses an endpoint whose response body
embeds an iteration counter — every request produces a different SHA-256,
which the ``response_determinism`` invariant must catch. Layer-3 combines a
clean variant with a route-drift variant and confirms the aggregate report
tags violations by variant name.
"""

from __future__ import annotations

import hashlib
import itertools
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harnesses.layer3_variants import run_layer3_variants
from harnesses.layer4_sequence import LayerStep, run_layer4_sequence
from plugins.fastapi import FastAPIPlugin

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="harness metrics require Linux /proc",
)


# ---------------------------------------------------------------------------
# App factories.
# ---------------------------------------------------------------------------


def _clean_app_factory() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    def _root() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _drifting_response_app_factory() -> FastAPI:
    """App whose ``/`` response embeds a monotonic counter — every call
    yields a different SHA-256 digest. Response-determinism must fire."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    counter = itertools.count()
    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    def _root() -> dict[str, int]:
        return {"n": next(counter)}

    return app


_route_drift_app_factory_counter = 0


def _route_drift_app_factory() -> FastAPI:
    """Adds one extra route on each build — route_registry_stable will fire."""
    global _route_drift_app_factory_counter  # noqa: PLW0603
    _route_drift_app_factory_counter += 1
    n = _route_drift_app_factory_counter

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    def _root() -> dict[str, bool]:
        return {"ok": True}

    for i in range(n):

        def _extra(i: int = i) -> dict[str, int]:
            return {"i": i}

        app.add_api_route(f"/extra_{i}", _extra, methods=["GET"])
    return app


def _reset_route_drift_counter() -> None:
    global _route_drift_app_factory_counter  # noqa: PLW0603
    _route_drift_app_factory_counter = 0


def _digest_probe(app: FastAPI) -> str | None:
    """Fire GET / and hash the response body — stable digest per identical response."""
    with TestClient(app) as tc:
        r = tc.get("/")
    return hashlib.sha256(r.content).hexdigest()


def _one_probe_request(client: object) -> None:
    r = client.get("/")  # type: ignore[attr-defined]
    assert r.status_code == 200


def _fastapi_route_signature(app: FastAPI) -> tuple[str, ...]:
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
# Layer 4 — sequence.
# ---------------------------------------------------------------------------


class TestLayer4Sequence:
    def test_clean_sequence_passes(self) -> None:
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)

        def _issue(app: FastAPI) -> None:
            with TestClient(app) as tc:
                r = tc.get("/")
            assert r.status_code == 200

        steps = (
            LayerStep(
                name="probe_1",
                action=_issue,
                invariants=("rss_return_to_baseline", "response_determinism"),
            ),
            LayerStep(
                name="probe_2",
                action=_issue,
                invariants=("rss_return_to_baseline", "response_determinism"),
            ),
        )
        report = run_layer4_sequence(
            plugin=plugin,
            steps=steps,
            response_digest_of=_digest_probe,
            target_commit="test",
        )
        assert report.result.success is True

    def test_drifting_response_caught_deterministically(self) -> None:
        # Each call to _digest_probe hits the counter endpoint → different digest.
        plugin = FastAPIPlugin(app_factory=_drifting_response_app_factory)

        def _issue(app: FastAPI) -> None:
            with TestClient(app) as tc:
                tc.get("/")

        steps = (
            LayerStep(name="step_1", action=_issue, invariants=("response_determinism",)),
            LayerStep(name="step_2", action=_issue, invariants=("response_determinism",)),
        )
        # 10 replays should all fail identically.
        outcomes: list[bool] = []
        for _ in range(10):
            report = run_layer4_sequence(
                plugin=plugin,
                steps=steps,
                response_digest_of=_digest_probe,
                target_commit="test",
            )
            outcomes.append(report.result.success)
        assert outcomes == [False] * 10

    def test_empty_steps_rejected(self) -> None:
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)
        with pytest.raises(ValueError, match="steps must not be empty"):
            run_layer4_sequence(
                plugin=plugin,
                steps=(),
                response_digest_of=_digest_probe,
                target_commit="test",
            )

    def test_layer_step_blank_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            LayerStep(name="", action=lambda _: None)

    def test_layer_step_whitespace_padded_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="trimmed"):
            LayerStep(name=" step ", action=lambda _: None)


# ---------------------------------------------------------------------------
# Layer 3 — feature-combination via variants.
# ---------------------------------------------------------------------------


class TestLayer3Variants:
    def test_two_clean_variants_all_pass(self) -> None:
        _reset_route_drift_counter()
        report = run_layer3_variants(
            plugin_factory=lambda af: FastAPIPlugin(app_factory=af),
            variants=(
                ("clean_a", _clean_app_factory),
                ("clean_b", _clean_app_factory),
            ),
            request_callable=_one_probe_request,
            route_signature_of=_fastapi_route_signature,
            rounds=5,
            target_commit="test",
        )
        assert report.result.success is True
        assert report.result.violations == ()

    def test_mixed_clean_and_route_drift_variant(self) -> None:
        _reset_route_drift_counter()
        report = run_layer3_variants(
            plugin_factory=lambda af: FastAPIPlugin(app_factory=af),
            variants=(
                ("clean", _clean_app_factory),
                ("route_drift", _route_drift_app_factory),
            ),
            request_callable=_one_probe_request,
            route_signature_of=_fastapi_route_signature,
            rounds=3,
            target_commit="test",
        )
        assert report.result.success is False
        # Violations from the "route_drift" variant must be tagged as such.
        names = [v.invariant_name for v in report.result.violations]
        assert any(n.startswith("route_drift::") for n in names)
        # Clean variant must NOT produce violations.
        assert not any(n.startswith("clean::") for n in names)

    def test_empty_variants_rejected(self) -> None:
        with pytest.raises(ValueError, match="variants must not be empty"):
            run_layer3_variants(
                plugin_factory=lambda af: FastAPIPlugin(app_factory=af),
                variants=(),
                request_callable=_one_probe_request,
                route_signature_of=_fastapi_route_signature,
                rounds=1,
                target_commit="test",
            )

    def test_duplicate_variant_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate variant name"):
            run_layer3_variants(
                plugin_factory=lambda af: FastAPIPlugin(app_factory=af),
                variants=(
                    ("dup", _clean_app_factory),
                    ("dup", _clean_app_factory),
                ),
                request_callable=_one_probe_request,
                route_signature_of=_fastapi_route_signature,
                rounds=1,
                target_commit="test",
            )
