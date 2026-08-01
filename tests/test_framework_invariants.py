"""Tests for :mod:`core.framework_invariants`.

Rule 9: planted fixtures — a stub with a mutable ``route_signature`` field.
Never depends on any real framework.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.framework_invariants import RouteRegistryStable
from core.invariant import Ok, Violation


@dataclass
class _StubApp:
    route_signature: tuple[str, ...]


class TestRouteRegistryStable:
    def test_unchanged_routes_return_ok(self) -> None:
        inv = RouteRegistryStable()
        app = _StubApp(route_signature=("GET /", "POST /login"))
        baseline = inv.setup(app)
        result = inv.check(app, baseline, 0)
        assert isinstance(result, Ok)

    def test_added_route_produces_violation(self) -> None:
        inv = RouteRegistryStable()
        app = _StubApp(route_signature=("GET /",))
        baseline = inv.setup(app)
        app.route_signature = ("GET /", "GET /leaked")
        result = inv.check(app, baseline, 5)
        assert isinstance(result, Violation)
        assert result.iteration == 5
        assert result.evidence["added"] == ["GET /leaked"]
        assert result.evidence["removed"] == []
        assert result.evidence["baseline_count"] == 1
        assert result.evidence["current_count"] == 2

    def test_removed_route_produces_violation(self) -> None:
        inv = RouteRegistryStable()
        app = _StubApp(route_signature=("GET /", "POST /login"))
        baseline = inv.setup(app)
        app.route_signature = ("GET /",)
        result = inv.check(app, baseline, 1)
        assert isinstance(result, Violation)
        assert result.evidence["removed"] == ["POST /login"]
        assert result.evidence["added"] == []

    def test_symmetric_diff_reports_both_add_and_remove(self) -> None:
        inv = RouteRegistryStable()
        app = _StubApp(route_signature=("GET /a", "GET /b"))
        baseline = inv.setup(app)
        app.route_signature = ("GET /b", "GET /c")
        result = inv.check(app, baseline, 0)
        assert isinstance(result, Violation)
        assert result.evidence["added"] == ["GET /c"]
        assert result.evidence["removed"] == ["GET /a"]
