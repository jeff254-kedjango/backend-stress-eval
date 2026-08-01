"""Tests for :mod:`plugins.fastapi`.

Rule 9: the planted-leak fixture uses a **module-level list** that a
misbehaving ``lifespan`` fills but never empties on shutdown. That is the
canonical Layer-2 (lifecycle) bug the harness exists to detect. Every
assertion is on synthetic in-process state — no external HTTP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from core.invariant import CheckResult, InvariantRegistry, Ok, Violation
from core.plugin import Plugin
from core.runner import Runner, cadence_end_only
from plugins.fastapi import FastAPIPlugin

# ---------------------------------------------------------------------------
# App factories.
# ---------------------------------------------------------------------------

# Module-level sentinel list that a leaky lifespan mutates. The invariant
# reads its length after each build/start/stop cycle; a clean app leaves it
# empty, a leaky one grows it monotonically.
_LEAK_SINK: list[int] = []


def _clean_app_factory() -> FastAPI:
    """A minimal FastAPI app with a well-behaved lifespan."""

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


def _leaky_app_factory() -> FastAPI:
    """A FastAPI app whose shutdown fails to clean up. Deterministic."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        _LEAK_SINK.append(1)
        yield
        # NOTE: no pop() — that's the planted bug.

    app = FastAPI(lifespan=lifespan)

    @app.get("/")
    def _root() -> dict[str, bool]:
        return {"ok": True}

    return app


def _reset_leak_sink() -> None:
    _LEAK_SINK.clear()


# ---------------------------------------------------------------------------
# Protocol conformance.
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_fastapi_plugin_satisfies_plugin_protocol_at_runtime(self) -> None:
        assert isinstance(FastAPIPlugin(app_factory=_clean_app_factory), Plugin)


# ---------------------------------------------------------------------------
# Basic lifecycle.
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_client_stop(self) -> None:
        _reset_leak_sink()
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)
        app = plugin.build_app()
        plugin.lifecycle_start(app)
        assert _LEAK_SINK == [1]  # startup fired

        c = plugin.client(app)
        r = c.get("/")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

        plugin.lifecycle_stop(app)
        assert _LEAK_SINK == []  # shutdown cleaned up

    def test_lifecycle_stop_is_idempotent(self) -> None:
        _reset_leak_sink()
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)
        app = plugin.build_app()
        plugin.lifecycle_start(app)
        plugin.lifecycle_stop(app)
        plugin.lifecycle_stop(app)  # must not raise
        assert _LEAK_SINK == []

    def test_client_lazily_starts_lifecycle(self) -> None:
        _reset_leak_sink()
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)
        app = plugin.build_app()
        # No explicit lifecycle_start — client() should enter the lifespan.
        _ = plugin.client(app)
        assert _LEAK_SINK == [1]
        plugin.lifecycle_stop(app)
        assert _LEAK_SINK == []


# ---------------------------------------------------------------------------
# reset() semantics.
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_dependency_overrides(self) -> None:
        _reset_leak_sink()
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)
        app = plugin.build_app()

        def _stub() -> int:
            return 42

        # Inject one override to prove reset() clears it.
        app.dependency_overrides[_stub] = _stub
        assert len(app.dependency_overrides) == 1
        plugin.reset(app)
        assert app.dependency_overrides == {}

    def test_reset_replaces_state(self) -> None:
        _reset_leak_sink()
        plugin = FastAPIPlugin(app_factory=_clean_app_factory)
        app = plugin.build_app()
        app.state.marker = "dirty"
        plugin.reset(app)
        # Old marker must be gone on the fresh State instance.
        assert not hasattr(app.state, "marker")


# ---------------------------------------------------------------------------
# feature_matrix.
# ---------------------------------------------------------------------------


class TestFeatureMatrix:
    def test_declares_all_canonical_features(self) -> None:
        m = FastAPIPlugin(app_factory=_clean_app_factory).feature_matrix()
        for key in (
            "dependency_injection",
            "middleware",
            "background_tasks",
            "streaming",
            "lifespan",
        ):
            assert m[key] is True

    def test_feature_matrix_is_read_only(self) -> None:
        import pytest

        m = FastAPIPlugin(app_factory=_clean_app_factory).feature_matrix()
        # We're deliberately probing runtime read-only-ness. Cast to a mutable
        # dict shape so mypy accepts the write while runtime still raises.
        from typing import cast

        writable = cast(dict[str, bool], m)
        with pytest.raises(TypeError):
            writable["middleware"] = False


# ---------------------------------------------------------------------------
# End-to-end: harness catches the planted lifecycle leak deterministically.
# This is the whole point of Chunk 7.
# ---------------------------------------------------------------------------


@dataclass
class _LeakSinkClean:
    """Invariant: after every lifecycle round-trip, ``_LEAK_SINK`` returns to
    its baseline length (measured at ``setup``)."""

    name: str = "leak_sink_returns_to_baseline"

    def setup(self, _state: object, /) -> int:
        return len(_LEAK_SINK)

    def check(self, _state: object, baseline: int, iteration: int, /) -> CheckResult:
        current = len(_LEAK_SINK)
        if current > baseline:
            return Violation(
                invariant_name=self.name,
                detail=f"_LEAK_SINK grew from {baseline} to {current}",
                evidence={"baseline": baseline, "current": current, "drift": current - baseline},
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)


class TestPlantedLifecycleLeak:
    def _lifecycle_loop(self, factory: Callable[[], FastAPI], rounds: int) -> tuple[bool, int]:
        """Run ``rounds`` full build/start/stop cycles and check the invariant.

        Uses ``cadence_end_only`` — the invariant fires exactly once, at the
        end. That's the correct cadence for lifecycle checks (Layer 2).
        """
        _reset_leak_sink()
        plugin = FastAPIPlugin(app_factory=factory)

        def state_producer(iteration: int) -> object:
            if iteration >= 0:
                app = plugin.build_app()
                plugin.lifecycle_start(app)
                plugin.lifecycle_stop(app)
            return None

        reg = InvariantRegistry()

        @dataclass
        class _Wrap:
            name: str = "leak_sink_returns_to_baseline"
            cadence: object = None  # attached below

            def setup(self, _s: object, /) -> int:
                return len(_LEAK_SINK)

            def check(self, _s: object, baseline: int, iteration: int, /) -> CheckResult:
                current = len(_LEAK_SINK)
                if current > baseline:
                    return Violation(
                        invariant_name=self.name,
                        detail=f"_LEAK_SINK grew from {baseline} to {current}",
                        evidence={
                            "baseline": baseline,
                            "current": current,
                            "drift": current - baseline,
                        },
                        iteration=iteration,
                    )
                return Ok(invariant_name=self.name)

        inv = _Wrap()
        inv.cadence = cadence_end_only()  # fire once at the end
        reg.register(inv)
        result = Runner(reg, state_producer, iterations=rounds).run()
        return result.success, len(result.violations)

    def test_clean_app_20_cycles_no_violations(self) -> None:
        success, violations = self._lifecycle_loop(_clean_app_factory, rounds=20)
        assert success is True
        assert violations == 0

    def test_leaky_app_fires_invariant_deterministically(self) -> None:
        # 10 identical replays — every one must FAIL identically.
        outcomes: list[tuple[bool, int]] = []
        for _ in range(10):
            outcomes.append(self._lifecycle_loop(_leaky_app_factory, rounds=20))
        # Every replay: not success, exactly one violation (cadence_end_only).
        assert all(o == (False, 1) for o in outcomes)
