"""Tests for :mod:`harnesses.fault_matrix`.

In-memory stub plugin implementing FaultInjectable. Same shape as
`tests/test_concurrency_matrix.py` — pure data for the diff, small
end-to-end for the runner, negative CLI paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import pytest

from core.invariant import Violation
from core.plugin_extensions import CANONICAL_FAULTS
from core.reporter import Report, ReportMetadata
from core.runner import RunResult
from harnesses.fault_matrix import (
    FAULT_MATRIX_SCHEMA_VERSION,
    FaultDivergence,
    FaultMatrixError,
    FaultMatrixReport,
    diff_faults,
    run_fault_matrix,
)


# ---------------------------------------------------------------------------
# Stub plugin — implements FaultInjectable. probe_with_fault does nothing
# by default; a Layer-2 lifecycle sweep threads through cleanly.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _StubApp:
    request_count: int = 0


@dataclass(slots=True)
class _StubFaultPlugin:
    name: str = "stub-fault"
    _faults: tuple[str, ...] = ("client-disconnect", "cancel-mid-request")
    probe_calls: list[str] = field(default_factory=list)

    def available_faults(self) -> tuple[str, ...]:
        return self._faults

    def probe_with_fault(self, client: _StubApp, fault_name: str, /) -> None:
        if fault_name not in self._faults:
            raise ValueError(f"unknown fault {fault_name!r}")
        self.probe_calls.append(fault_name)
        # No-op probe. Real plugins would inject the fault here.

    # Base Plugin surface.
    def build_app(self) -> _StubApp:
        return _StubApp()

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
    """Plugin that does NOT implement FaultInjectable."""

    name: str = "stub-base"

    def build_app(self) -> _StubApp:
        return _StubApp()

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
# diff_faults — pure data.
# ---------------------------------------------------------------------------
def _report(target_commit: str, violations: tuple[Violation, ...] = ()) -> Report:
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


class TestDiffFaults:
    def test_all_faults_clean_produces_no_divergence(self) -> None:
        per_fault = {
            "client-disconnect": {"layer1_repetition": _report("A")},
            "cancel-mid-request": {"layer1_repetition": _report("A")},
        }
        assert diff_faults(per_fault) == ()

    def test_violation_only_under_one_fault_is_divergent(self) -> None:
        per_fault = {
            "client-disconnect": {"layer1_repetition": _report("A", (_v("rss", 100),))},
            "cancel-mid-request": {"layer1_repetition": _report("A")},
        }
        divs = diff_faults(per_fault)
        assert len(divs) == 1
        assert divs[0].violating_faults == ("client-disconnect",)
        assert divs[0].passing_faults == ("cancel-mid-request",)
        assert divs[0].invariant_name == "rss"
        assert divs[0].iteration == 100

    def test_all_faults_violate_is_NOT_divergent(self) -> None:
        """Universal bug under any fault — not fault-specific."""
        per_fault = {
            "client-disconnect": {"layer1_repetition": _report("A", (_v("rss", 100),))},
            "cancel-mid-request": {"layer1_repetition": _report("A", (_v("rss", 100),))},
        }
        assert diff_faults(per_fault) == ()

    def test_divergences_sorted_stable(self) -> None:
        per_fault = {
            "client-disconnect": {
                "layer1_repetition": _report("A", (_v("b_inv", 100), _v("a_inv", 200))),
                "layer2_lifecycle": _report("A", (_v("z_inv", 5),)),
            },
            "cancel-mid-request": {
                "layer1_repetition": _report("A"),
                "layer2_lifecycle": _report("A"),
            },
        }
        divs = diff_faults(per_fault)
        keys = [(d.layer, d.invariant_name, d.iteration) for d in divs]
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# run_fault_matrix — end-to-end.
# ---------------------------------------------------------------------------
class TestRunFaultMatrix:
    def test_non_fault_injectable_plugin_rejected(self) -> None:
        with pytest.raises(FaultMatrixError, match="FaultInjectable"):
            run_fault_matrix(plugin=_StubBasePlugin(), target_commit="A")

    def test_unknown_fault_rejected(self) -> None:
        with pytest.raises(FaultMatrixError, match="unknown fault"):
            run_fault_matrix(
                plugin=_StubFaultPlugin(),
                target_commit="A",
                faults=("does-not-exist",),
            )

    def test_empty_faults_rejected(self) -> None:
        plugin = _StubFaultPlugin(_faults=())
        with pytest.raises(FaultMatrixError, match="Nothing to run"):
            run_fault_matrix(plugin=plugin, target_commit="A")

    def test_matrix_runs_and_produces_report(self) -> None:
        plugin = _StubFaultPlugin()
        matrix = run_fault_matrix(
            plugin=plugin,
            target_commit="A",
            iterations_l1=3,
            rounds_l2=1,
            rounds_l3=1,
        )
        assert isinstance(matrix, FaultMatrixReport)
        assert set(matrix.faults) == {"client-disconnect", "cancel-mid-request"}
        assert "client-disconnect" in matrix.per_fault
        assert "cancel-mid-request" in matrix.per_fault
        # Both faults' probes were exercised.
        assert set(plugin.probe_calls) == {"client-disconnect", "cancel-mid-request"}


# ---------------------------------------------------------------------------
# JSON byte-stability.
# ---------------------------------------------------------------------------
class TestJsonSerialization:
    def test_to_json_sort_keyed(self) -> None:
        matrix = FaultMatrixReport(
            schema_version=FAULT_MATRIX_SCHEMA_VERSION,
            plugin_name="stub-fault",
            target_commit="A",
            faults=("client-disconnect", "cancel-mid-request"),
            per_fault={},
            divergences=(
                FaultDivergence(
                    layer="layer1_repetition",
                    invariant_name="rss",
                    iteration=100,
                    violating_faults=("client-disconnect",),
                    passing_faults=("cancel-mid-request",),
                ),
            ),
        )
        import json as _json

        parsed = _json.loads(matrix.to_json())
        assert parsed["schema_version"] == FAULT_MATRIX_SCHEMA_VERSION
        assert list(parsed.keys()) == sorted(parsed.keys())


# ---------------------------------------------------------------------------
# Canonical fault ordering — regression pin.
# ---------------------------------------------------------------------------
def test_canonical_fault_list_stable() -> None:
    assert CANONICAL_FAULTS == (
        "background-exception",
        "cancel-mid-request",
        "client-disconnect",
    )


def test_canonical_faults_sort_first() -> None:
    from harnesses.fault_matrix import _sorted_canonical_first

    faults = ("vendor-x", "client-disconnect", "background-exception")
    assert _sorted_canonical_first(faults) == (
        "background-exception",
        "client-disconnect",
        "vendor-x",
    )


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
class TestCli:
    def test_unknown_plugin_exits_precondition(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cli.main import EXIT_FAULT_MATRIX_PRECONDITION, main

        rc = main(["fault-matrix", "does_not_exist"])
        assert rc == EXIT_FAULT_MATRIX_PRECONDITION
        assert "unknown plugin" in capsys.readouterr().err.lower()

    def test_non_opted_in_plugin_exits_precondition(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Bundled `stub` doesn't implement FaultInjectable."""
        from cli.main import EXIT_FAULT_MATRIX_PRECONDITION, main

        rc = main(["fault-matrix", "stub"])
        assert rc == EXIT_FAULT_MATRIX_PRECONDITION
        assert "FaultInjectable" in capsys.readouterr().err
