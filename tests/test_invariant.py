"""Tests for ``core.invariant`` — the invariant Protocol and registry.

Rule-9 discipline: every failure branch is exercised with a *planted* bug
fixture, not asserted in the abstract. A hand-crafted invariant that lies
about its state gives us the deterministic Violation the runner will later
depend on.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass
from typing import Any

import pytest

from core.invariant import (
    CheckResult,
    DuplicateInvariantError,
    InvariantRegistry,
    Ok,
    UnknownInvariantError,
    Violation,
)

# ---------------------------------------------------------------------------
# Fixtures: minimal invariants for exercising the registry + result shapes.
# ---------------------------------------------------------------------------


@dataclass
class _AlwaysOk:
    """Trivial passing invariant. ``state`` is opaque; baseline is None."""

    name: str = "always_ok"

    def setup(self, _state: object, /) -> None:
        return None

    def check(self, _state: object, _baseline: None, _iteration: int, /) -> CheckResult:
        return Ok(invariant_name=self.name)


@dataclass
class _MemoryLeakDetector:
    """Planted-bug detector: state carries an ``rss_kb`` int; baseline is the
    first sample. If later samples exceed baseline by more than ``slack_kb``,
    return a Violation whose evidence lets a grader pinpoint the drift.
    """

    name: str = "rss_return_to_baseline"
    slack_kb: int = 1024

    def setup(self, state: dict[str, int], /) -> int:
        return state["rss_kb"]

    def check(self, state: dict[str, int], baseline: int, iteration: int, /) -> CheckResult:
        current = state["rss_kb"]
        drift = current - baseline
        if drift > self.slack_kb:
            return Violation(
                invariant_name=self.name,
                detail=f"RSS drifted +{drift} KB above baseline",
                evidence={
                    "baseline_kb": baseline,
                    "current_kb": current,
                    "drift_kb": drift,
                    "slack_kb": self.slack_kb,
                },
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)


# ---------------------------------------------------------------------------
# CheckResult shape.
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_ok_is_frozen(self) -> None:
        ok = Ok(invariant_name="x")
        with pytest.raises(FrozenInstanceError):
            ok.invariant_name = "y"  # type: ignore[misc]

    def test_violation_is_frozen(self) -> None:
        v = Violation(invariant_name="x", detail="d", evidence={})
        with pytest.raises(FrozenInstanceError):
            v.detail = "changed"  # type: ignore[misc]

    def test_violation_evidence_is_json_serialisable(self) -> None:
        v = Violation(
            invariant_name="x",
            detail="d",
            evidence={
                "flag": True,
                "n": 1,
                "ratio": 1.5,
                "labels": ["a", "b"],
                "nested": {"k": None},
            },
            iteration=7,
        )
        # Round-trip proves the JsonValue alias holds in practice.
        payload = json.dumps(
            {
                "invariant_name": v.invariant_name,
                "detail": v.detail,
                "iteration": v.iteration,
                "evidence": dict(v.evidence),
            }
        )
        loaded = json.loads(payload)
        assert loaded["evidence"]["nested"]["k"] is None
        assert loaded["iteration"] == 7


# ---------------------------------------------------------------------------
# Planted-bug fixture: leak detector must fire deterministically.
# ---------------------------------------------------------------------------


class TestPlantedBugDetection:
    def test_stable_rss_produces_ok(self) -> None:
        inv = _MemoryLeakDetector(slack_kb=100)
        state = {"rss_kb": 10_000}
        baseline = inv.setup(state)
        # Protocol signatures are positional-only (Rule 5 clarity — plugin
        # authors can rename params without breaking callers).
        result = inv.check(state, baseline, 0)
        assert isinstance(result, Ok)
        assert result.invariant_name == "rss_return_to_baseline"

    def test_growth_beyond_slack_produces_violation_with_evidence(self) -> None:
        inv = _MemoryLeakDetector(slack_kb=100)
        state = {"rss_kb": 10_000}
        baseline = inv.setup(state)
        # Simulate a leak: RSS climbs 500 KB above baseline (slack is 100).
        state["rss_kb"] = 10_500
        result = inv.check(state, baseline, 37)
        assert isinstance(result, Violation)
        assert result.iteration == 37
        assert result.evidence["drift_kb"] == 500
        assert result.evidence["baseline_kb"] == 10_000
        assert result.evidence["current_kb"] == 10_500

    def test_detection_is_deterministic_across_runs(self) -> None:
        # Rule 9 — the harness's whole point is deterministic detection.
        results: list[bool] = []
        for _ in range(5):
            inv = _MemoryLeakDetector(slack_kb=100)
            state = {"rss_kb": 10_000}
            baseline = inv.setup(state)
            state["rss_kb"] = 10_500
            results.append(isinstance(inv.check(state, baseline, 37), Violation))
        assert results == [True] * 5


# ---------------------------------------------------------------------------
# Registry — O(1) semantics, duplicate rejection, name validation.
# ---------------------------------------------------------------------------


class TestInvariantRegistry:
    def test_register_and_get_returns_exact_instance(self) -> None:
        reg = InvariantRegistry()
        inv = _AlwaysOk()
        reg.register(inv)
        assert reg.get("always_ok") is inv

    def test_len_and_contains(self) -> None:
        reg = InvariantRegistry()
        assert len(reg) == 0
        assert "always_ok" not in reg
        reg.register(_AlwaysOk())
        assert len(reg) == 1
        assert "always_ok" in reg
        assert 123 not in reg  # non-str keys must not raise

    def test_iter_yields_insertion_order(self) -> None:
        reg = InvariantRegistry()
        a = _AlwaysOk(name="a")
        b = _AlwaysOk(name="b")
        c = _AlwaysOk(name="c")
        for inv in (b, a, c):
            reg.register(inv)
        assert [inv.name for inv in reg] == ["b", "a", "c"]

    def test_duplicate_name_raises(self) -> None:
        reg = InvariantRegistry()
        reg.register(_AlwaysOk(name="dup"))
        with pytest.raises(DuplicateInvariantError, match="dup"):
            reg.register(_AlwaysOk(name="dup"))

    def test_unknown_name_raises(self) -> None:
        reg = InvariantRegistry()
        with pytest.raises(UnknownInvariantError):
            reg.get("nope")

    @pytest.mark.parametrize("bad_name", ["", "   ", "\t", "\n"])
    def test_blank_name_rejected_at_register(self, bad_name: str) -> None:
        reg = InvariantRegistry()
        with pytest.raises(ValueError, match="non-empty"):
            reg.register(_AlwaysOk(name=bad_name))

    def test_leading_or_trailing_whitespace_rejected(self) -> None:
        reg = InvariantRegistry()
        with pytest.raises(ValueError, match="whitespace"):
            reg.register(_AlwaysOk(name=" leading"))
        with pytest.raises(ValueError, match="whitespace"):
            reg.register(_AlwaysOk(name="trailing "))

    def test_non_string_name_rejected(self) -> None:
        reg = InvariantRegistry()

        @dataclass
        class _BadName:
            name: Any = 42

            def setup(self, _state: object, /) -> None:
                return None

            def check(self, _state: object, _baseline: None, _iteration: int, /) -> CheckResult:
                return Ok(invariant_name="x")

        with pytest.raises(TypeError, match="must be str"):
            reg.register(_BadName())

    def test_names_returns_read_only_view(self) -> None:
        reg = InvariantRegistry()
        reg.register(_AlwaysOk())
        view = reg.names()
        assert "always_ok" in view
        with pytest.raises(TypeError):
            view["always_ok"] = _AlwaysOk()  # type: ignore[index]
