"""Tests for :mod:`harnesses.concurrency_matrix`.

Uses an in-memory stub plugin implementing ConcurrencyAware. The stub
lets tests dial in per-mode violation shape so cross-mode divergence
detection is unit-testable without any framework.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import pytest

from core.invariant import Violation
from core.plugin_extensions import CONCURRENCY_MODES_CANONICAL
from core.reporter import Report, ReportMetadata
from core.runner import RunResult
from harnesses.concurrency_matrix import (
    MODE_MATRIX_SCHEMA_VERSION,
    ModeDivergence,
    ModeMatrixError,
    ModeMatrixReport,
    diff_modes,
    run_concurrency_matrix,
)


# ---------------------------------------------------------------------------
# Stub plugin — implements ConcurrencyAware. Layer runs are simulated via
# ``build_app_for_mode`` returning a marker dict; the real plugin surface
# stays no-op so run_discovery threads through cleanly.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _StubApp:
    mode: str
    magic: int = 1


@dataclass(slots=True)
class _StubModePlugin:
    name: str = "stub"
    _modes: tuple[str, ...] = ("asyncio", "anyio-trio")
    build_app_calls: list[str] = field(default_factory=list)

    def available_modes(self) -> tuple[str, ...]:
        return self._modes

    def build_app_for_mode(self, mode: str, /) -> _StubApp:
        if mode not in self._modes:
            raise ValueError(f"unknown mode {mode!r}")
        self.build_app_calls.append(mode)
        return _StubApp(mode=mode)

    # Base Plugin surface — all no-ops.
    def build_app(self) -> _StubApp:
        return _StubApp(mode="default")

    def client(self, app: _StubApp, /) -> _StubApp:
        return app

    def lifecycle_start(self, app: _StubApp, /) -> None:
        return None

    def lifecycle_stop(self, app: _StubApp, /) -> None:
        return None

    def reset(self, app: _StubApp, /) -> None:
        return None

    def feature_matrix(self) -> Mapping[str, bool]:
        return MappingProxyType({})

    def probe(self, client: _StubApp, /) -> None:
        return None

    def route_signature(self, app: _StubApp, /) -> tuple[str, ...]:
        return ()

    def response_digest(self, app: _StubApp, /) -> str | None:
        return None


@dataclass(slots=True)
class _StubBasePlugin:
    """Plugin that does NOT implement ConcurrencyAware — for negative tests."""

    name: str = "stub-base"

    def build_app(self) -> _StubApp:
        return _StubApp(mode="default")

    def client(self, app: _StubApp, /) -> _StubApp:
        return app

    def lifecycle_start(self, app: _StubApp, /) -> None:
        return None

    def lifecycle_stop(self, app: _StubApp, /) -> None:
        return None

    def reset(self, app: _StubApp, /) -> None:
        return None

    def feature_matrix(self) -> Mapping[str, bool]:
        return MappingProxyType({})

    def probe(self, client: _StubApp, /) -> None:
        return None

    def route_signature(self, app: _StubApp, /) -> tuple[str, ...]:
        return ()

    def response_digest(self, app: _StubApp, /) -> str | None:
        return None


# ---------------------------------------------------------------------------
# diff_modes — pure data.
# ---------------------------------------------------------------------------
def _report_with_violations(target_commit: str, violations: tuple[Violation, ...] = ()) -> Report:
    return Report(
        metadata=ReportMetadata(
            target="stub",
            target_commit=target_commit,
            seed=0,
            iterations_requested=1,
            harness_version="0.0.1",
        ),
        result=RunResult(
            invariants_evaluated=("rss_return_to_baseline",),
            iterations_completed=1,
            success=not violations,
            violations=violations,
        ),
    )


def _v(name: str, iteration: int | None = 42) -> Violation:
    return Violation(
        invariant_name=name,
        detail="d",
        evidence=MappingProxyType({"k": 1}),
        iteration=iteration,
    )


class TestDiffModes:
    def test_all_modes_clean_produces_no_divergence(self) -> None:
        per_mode = {
            "asyncio": {"layer1_repetition": _report_with_violations("A")},
            "anyio-trio": {"layer1_repetition": _report_with_violations("A")},
        }
        assert diff_modes(per_mode) == ()

    def test_violation_only_in_one_mode_is_divergent(self) -> None:
        per_mode = {
            "asyncio": {"layer1_repetition": _report_with_violations("A", (_v("rss", 100),))},
            "anyio-trio": {"layer1_repetition": _report_with_violations("A")},
        }
        divs = diff_modes(per_mode)
        assert len(divs) == 1
        assert divs[0].violating_modes == ("asyncio",)
        assert divs[0].passing_modes == ("anyio-trio",)
        assert divs[0].invariant_name == "rss"
        assert divs[0].iteration == 100

    def test_all_modes_violate_is_NOT_divergent(self) -> None:
        """Same key violated everywhere = universal bug, not mode-divergent."""
        per_mode = {
            "asyncio": {"layer1_repetition": _report_with_violations("A", (_v("rss", 100),))},
            "anyio-trio": {"layer1_repetition": _report_with_violations("A", (_v("rss", 100),))},
        }
        assert diff_modes(per_mode) == ()

    def test_divergences_sorted_stable(self) -> None:
        """Emitted in (layer, invariant, iteration) order for byte-stability."""
        per_mode = {
            "asyncio": {
                "layer1_repetition": _report_with_violations(
                    "A", (_v("b_inv", 100), _v("a_inv", 200))
                ),
                "layer2_lifecycle": _report_with_violations("A", (_v("z_inv", 5),)),
            },
            "anyio-trio": {
                "layer1_repetition": _report_with_violations("A"),
                "layer2_lifecycle": _report_with_violations("A"),
            },
        }
        divs = diff_modes(per_mode)
        keys = [(d.layer, d.invariant_name, d.iteration) for d in divs]
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# run_concurrency_matrix — end-to-end using the stub.
# ---------------------------------------------------------------------------
class TestRunConcurrencyMatrix:
    def test_non_concurrency_aware_plugin_rejected(self) -> None:
        plugin = _StubBasePlugin()
        with pytest.raises(ModeMatrixError, match="ConcurrencyAware"):
            run_concurrency_matrix(plugin=plugin, target_commit="A")

    def test_unknown_mode_rejected(self) -> None:
        plugin = _StubModePlugin()
        with pytest.raises(ModeMatrixError, match="unknown mode"):
            run_concurrency_matrix(plugin=plugin, target_commit="A", modes=("does-not-exist",))

    def test_empty_modes_rejected(self) -> None:
        plugin = _StubModePlugin(_modes=())
        with pytest.raises(ModeMatrixError, match="Nothing to run"):
            run_concurrency_matrix(plugin=plugin, target_commit="A")

    def test_matrix_runs_and_produces_report(self) -> None:
        plugin = _StubModePlugin()
        # Use tiny counts so this test finishes fast — real modes are simulated.
        matrix = run_concurrency_matrix(
            plugin=plugin,
            target_commit="A",
            iterations_l1=3,
            rounds_l2=1,
            rounds_l3=1,
        )
        assert isinstance(matrix, ModeMatrixReport)
        assert set(matrix.modes) == {"asyncio", "anyio-trio"}
        assert "asyncio" in matrix.per_mode
        assert "anyio-trio" in matrix.per_mode
        # `build_app_for_mode` should have been invoked per mode (many times
        # actually — Layer 2 rebuilds each round). Assert it hit both modes.
        assert set(plugin.build_app_calls) == {"asyncio", "anyio-trio"}


# ---------------------------------------------------------------------------
# ModeMatrixReport.to_json — byte stability.
# ---------------------------------------------------------------------------
class TestJsonSerialization:
    def test_to_json_sort_keyed(self) -> None:
        matrix = ModeMatrixReport(
            schema_version=MODE_MATRIX_SCHEMA_VERSION,
            plugin_name="stub",
            target_commit="A",
            modes=("asyncio", "anyio-trio"),
            per_mode={},
            divergences=(
                ModeDivergence(
                    layer="layer1_repetition",
                    invariant_name="rss",
                    iteration=100,
                    violating_modes=("asyncio",),
                    passing_modes=("anyio-trio",),
                ),
            ),
        )
        import json as _json

        payload = matrix.to_json()
        parsed = _json.loads(payload)
        assert parsed["schema_version"] == MODE_MATRIX_SCHEMA_VERSION
        assert parsed["plugin_name"] == "stub"
        assert list(parsed.keys()) == sorted(parsed.keys())


# ---------------------------------------------------------------------------
# Canonical mode ordering — regression for the sort.
# ---------------------------------------------------------------------------
def test_canonical_mode_list_stable() -> None:
    """Fail-loud check: if someone reorders the canonical list, tests catch it."""
    assert CONCURRENCY_MODES_CANONICAL == (
        "asyncio",
        "anyio-asyncio",
        "anyio-trio",
        "sync-threadpool",
    )


def test_canonical_modes_sort_first() -> None:
    """A plugin that returns modes in random order sorts canonical first."""
    from harnesses.concurrency_matrix import _sorted_canonical_first

    modes = ("vendor-x", "anyio-trio", "asyncio")
    assert _sorted_canonical_first(modes) == ("asyncio", "anyio-trio", "vendor-x")


# ---------------------------------------------------------------------------
# CLI wiring — the negative paths (unknown plugin, non-opted-in plugin).
# ---------------------------------------------------------------------------
class TestCli:
    def test_unknown_plugin_exits_precondition(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cli.main import EXIT_MODE_MATRIX_PRECONDITION, main

        rc = main(["concurrency-matrix", "does_not_exist"])
        assert rc == EXIT_MODE_MATRIX_PRECONDITION
        assert "unknown plugin" in capsys.readouterr().err.lower()

    def test_non_opted_in_plugin_exits_precondition(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The bundled `stub` plugin doesn't implement ConcurrencyAware,
        so the CLI must reject with EXIT_MODE_MATRIX_PRECONDITION rather
        than crashing deep in the harness."""
        from cli.main import EXIT_MODE_MATRIX_PRECONDITION, main

        rc = main(["concurrency-matrix", "stub"])
        assert rc == EXIT_MODE_MATRIX_PRECONDITION
        assert "ConcurrencyAware" in capsys.readouterr().err
