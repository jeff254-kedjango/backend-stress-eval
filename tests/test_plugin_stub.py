"""Tests for :mod:`core.plugin` + :mod:`plugins.stub`.

Rule 9: planted-leak toggle is the deterministic bug fixture. Every
detection assertion is on synthetic in-memory state — no real HTTP, no
real framework, no wall-clock timing.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.invariant import CheckResult, InvariantRegistry, Ok, Violation
from core.plugin import Plugin
from core.runner import Runner
from plugins.stub import StubApp, StubPlugin

# ---------------------------------------------------------------------------
# Protocol conformance.
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_stub_plugin_satisfies_plugin_protocol_at_runtime(self) -> None:
        # runtime_checkable Protocol → isinstance works.
        assert isinstance(StubPlugin(), Plugin)


# ---------------------------------------------------------------------------
# Basic lifecycle behaviour.
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_build_client_request_stop(self) -> None:
        # mypy --strict + warn_unreachable narrows a bool attribute after
        # ``assert not app.started`` — later ``assert app.started`` then
        # reads as unreachable because mypy doesn't model attribute mutation
        # through method calls. We split the assertions into fresh apps.
        plugin = StubPlugin()
        pre_start = plugin.build_app()
        assert not pre_start.started

        app = plugin.build_app()
        plugin.lifecycle_start(app)
        client = plugin.client(app)
        for _ in range(5):
            client.issue_request()
        assert app.request_count == 5
        plugin.lifecycle_stop(app)
        # Post-stop invariants tested separately in test_lifecycle_stop_is_idempotent.

    def test_lifecycle_stop_is_idempotent(self) -> None:
        # Uses a fresh app; no prior narrow on ``stopped``, so mypy is happy.
        plugin = StubPlugin()
        app = plugin.build_app()
        plugin.lifecycle_start(app)
        plugin.lifecycle_stop(app)
        plugin.lifecycle_stop(app)  # must not raise
        assert app.stopped

    def test_reset_clears_request_count(self) -> None:
        plugin = StubPlugin()
        app = plugin.build_app()
        client = plugin.client(app)
        for _ in range(3):
            client.issue_request()
        assert app.request_count == 3
        plugin.reset(app)
        assert app.request_count == 0

    def test_reset_does_not_clear_leaked_kb(self) -> None:
        # This is the planted-bug contract: reset does NOT touch leaked_kb.
        plugin = StubPlugin(planted_leak=True)
        app = plugin.build_app()
        client = plugin.client(app)
        for _ in range(10):
            client.issue_request()
        assert app.leaked_kb == 10
        plugin.reset(app)
        assert app.request_count == 0
        assert app.leaked_kb == 10  # persisted through reset — the planted bug


# ---------------------------------------------------------------------------
# feature_matrix.
# ---------------------------------------------------------------------------


class TestFeatureMatrix:
    def test_features_are_declared(self) -> None:
        m = StubPlugin().feature_matrix()
        assert m["lifespan"] is True
        assert m["middleware"] is False

    def test_feature_matrix_is_read_only(self) -> None:
        # We hand out a MappingProxyType view — writes must raise at runtime.
        # Cast to a mutable dict shape so mypy accepts the deliberately-
        # invalid write. Runtime still raises TypeError (the assertion).
        from typing import cast

        import pytest

        m = StubPlugin().feature_matrix()
        writable = cast(dict[str, bool], m)
        with pytest.raises(TypeError):
            writable["middleware"] = True


# ---------------------------------------------------------------------------
# End-to-end: Runner drives the stub plugin, invariant catches the planted leak.
# This is the whole point of Chunk 6 — proving core drives a plugin.
# ---------------------------------------------------------------------------


@dataclass
class _LeakFreeApp:
    """Invariant: after every reset, ``leaked_kb`` should be 0. Deterministic."""

    name: str = "leaked_kb_zero_after_reset"

    def setup(self, _state: object, /) -> None:
        return None

    def check(self, state: object, _b: None, iteration: int, /) -> CheckResult:
        assert isinstance(state, StubApp)
        if state.leaked_kb > 0:
            return Violation(
                invariant_name=self.name,
                detail=f"leaked_kb={state.leaked_kb} after reset",
                evidence={"leaked_kb": state.leaked_kb},
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)


class TestPluginDrivesRunner:
    def _run_with(self, planted_leak: bool) -> tuple[bool, int, int]:
        """Full loop: build app, run 20 lifecycle iterations (each issues one
        request then resets), let the invariant check every iter. Returns
        (success, violation_count, first_violation_iter_or_-1).
        """
        plugin = StubPlugin(planted_leak=planted_leak)
        app = plugin.build_app()
        plugin.lifecycle_start(app)
        client = plugin.client(app)

        def state_producer(iteration: int) -> object:
            if iteration >= 0:
                client.issue_request()
                # Reset AFTER the request so the invariant sees whatever
                # persisted through reset — that's how we surface leaks.
                plugin.reset(app)
            return app

        reg = InvariantRegistry()
        reg.register(_LeakFreeApp())
        result = Runner(reg, state_producer, iterations=20).run()
        plugin.lifecycle_stop(app)

        # Careful: an integer 0 is a legitimate iteration index. Using
        # ``first_iter or -1`` would silently rewrite 0 → -1 (measured 2026-08-01).
        # Explicit sentinel: -1 iff no violations occurred.
        if not result.violations:
            first_iter = -1
        else:
            first_iter_value = result.violations[0].iteration
            first_iter = -1 if first_iter_value is None else first_iter_value
        return result.success, len(result.violations), first_iter

    def test_clean_app_passes_all_iterations(self) -> None:
        success, violations, _first = self._run_with(planted_leak=False)
        assert success is True
        assert violations == 0

    def test_planted_leak_fires_invariant_deterministically(self) -> None:
        # 10 identical replays — must produce identical results.
        outcomes: list[tuple[bool, int]] = []
        for _ in range(10):
            success, count, first = self._run_with(planted_leak=True)
            outcomes.append((success, count))
            # Bug shows up from iteration 0 (first request already leaked).
            assert first == 0
        assert all(o == (False, 20) for o in outcomes)  # every iter fires
