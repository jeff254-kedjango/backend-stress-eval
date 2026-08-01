"""Tests for :mod:`core.runner` — planted-bug fixtures.

Rule 9: all fixtures are synthetic. We never assert against real timings or
real RSS; the invariant either fires deterministically for a designed input
or it doesn't. Same-input → same-output — the harness's whole thesis.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from core.invariant import CheckResult, InvariantRegistry, Ok, Violation
from core.runner import (
    Cadence,
    Runner,
    RunResult,
    cadence_end_only,
    cadence_every_iteration,
    cadence_every_k,
)

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@dataclass
class _AlwaysOk:
    name: str = "always_ok"

    def setup(self, _state: object, /) -> None:
        return None

    def check(self, _state: object, _baseline: None, _iter: int, /) -> CheckResult:
        return Ok(invariant_name=self.name)


@dataclass
class _FiresOnceOn:
    """Fires exactly one Violation on ``target_iteration``. Deterministic."""

    target_iteration: int
    name: str = "fires_once_on"

    def setup(self, _state: object, /) -> None:
        return None

    def check(self, _state: object, _baseline: None, iteration: int, /) -> CheckResult:
        if iteration == self.target_iteration:
            return Violation(
                invariant_name=self.name,
                detail=f"planted violation at iteration {iteration}",
                evidence={"iteration": iteration},
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)


@dataclass
class _CountsCalls:
    """Records every iteration index seen — for cadence assertions."""

    seen: list[int] = field(default_factory=list)
    name: str = "counts_calls"
    cadence: Cadence = field(default_factory=cadence_every_iteration)

    def setup(self, _state: object, /) -> None:
        return None

    def check(self, _state: object, _baseline: None, iteration: int, /) -> CheckResult:
        self.seen.append(iteration)
        return Ok(invariant_name=self.name)


@dataclass
class _CapturesBaseline:
    """setup() returns a value derived from state; check() asserts round-trip."""

    seen_baselines: list[object] = field(default_factory=list)
    name: str = "captures_baseline"

    def setup(self, state: object, /) -> object:
        return state

    def check(self, _state: object, baseline: object, _i: int, /) -> CheckResult:
        self.seen_baselines.append(baseline)
        return Ok(invariant_name=self.name)


def _static_state_producer(value: object) -> Callable[[int], object]:
    """Return a state_producer that yields ``value`` for every iteration."""

    def _producer(_iteration: int) -> object:
        return value

    return _producer


# ---------------------------------------------------------------------------
# Cadence semantics.
# ---------------------------------------------------------------------------


class TestCadence:
    def test_every_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            Cadence(every=0)

    def test_every_k_fires_on_multiples(self) -> None:
        reg = InvariantRegistry()
        inv = _CountsCalls(cadence=cadence_every_k(3))
        reg.register(inv)
        Runner(reg, _static_state_producer(None), iterations=10).run()
        # 0, 3, 6, 9
        assert inv.seen == [0, 3, 6, 9]

    def test_every_iteration_fires_every_time(self) -> None:
        reg = InvariantRegistry()
        inv = _CountsCalls(cadence=cadence_every_iteration())
        reg.register(inv)
        Runner(reg, _static_state_producer(None), iterations=5).run()
        assert inv.seen == [0, 1, 2, 3, 4]

    def test_end_only_fires_once_at_final(self) -> None:
        reg = InvariantRegistry()
        inv = _CountsCalls(cadence=cadence_end_only())
        reg.register(inv)
        Runner(reg, _static_state_producer(None), iterations=5).run()
        assert inv.seen == [4]


# ---------------------------------------------------------------------------
# Baseline threading.
# ---------------------------------------------------------------------------


class TestBaselines:
    def test_baseline_captured_from_neg_one_call(self) -> None:
        # state_producer distinguishes -1 (baseline) from real iterations.
        def producer(i: int) -> str:
            return "baseline_state" if i == -1 else f"iter_state_{i}"

        reg = InvariantRegistry()
        inv = _CapturesBaseline()
        reg.register(inv)
        Runner(reg, producer, iterations=3).run()
        # Same baseline every check.
        assert inv.seen_baselines == ["baseline_state"] * 3


# ---------------------------------------------------------------------------
# Planted-bug detection — Rule 9 core.
# ---------------------------------------------------------------------------


class TestPlantedBugDetection:
    def test_iteration_37_leak_caught_at_iteration_37(self) -> None:
        # The Playwright/Vite archetype: fails every N-th run.
        reg = InvariantRegistry()
        reg.register(_FiresOnceOn(target_iteration=37))
        result = Runner(reg, _static_state_producer(None), iterations=100).run()
        assert result.success is False
        assert len(result.violations) == 1
        assert result.violations[0].iteration == 37

    def test_same_input_same_output_ten_repeats(self) -> None:
        # Determinism guarantee. Run 10 identical Runners; every payload byte-equal.
        payloads: list[tuple[Violation, ...]] = []
        for _ in range(10):
            reg = InvariantRegistry()
            reg.register(_FiresOnceOn(target_iteration=7))
            payloads.append(
                Runner(reg, _static_state_producer(None), iterations=20).run().violations
            )
        first = payloads[0]
        for p in payloads[1:]:
            assert p == first

    def test_stop_on_first_violation_short_circuits(self) -> None:
        # Registration order matters: 'a' fires at iter 3 BEFORE 'counter'
        # gets its turn in the inner loop. stop_on_first_violation means
        # "return as soon as any invariant reports Violation" — so on iter 3,
        # 'counter' is never called. This is the deterministic, documented
        # semantic; the test locks it in.
        reg = InvariantRegistry()
        reg.register(_FiresOnceOn(target_iteration=3, name="a"))
        counter = _CountsCalls(name="counter")
        reg.register(counter)
        result = Runner(
            reg,
            _static_state_producer(None),
            iterations=10,
            stop_on_first_violation=True,
        ).run()
        assert result.success is False
        # 4 iterations were entered (0..3); on iter 3, 'a' broke us out
        # before 'counter' ran, so counter.seen == [0, 1, 2].
        assert result.iterations_completed == 4
        assert counter.seen == [0, 1, 2]

    def test_run_to_completion_gathers_all_violations(self) -> None:
        reg = InvariantRegistry()
        reg.register(_FiresOnceOn(target_iteration=2, name="early"))
        reg.register(_FiresOnceOn(target_iteration=8, name="late"))
        result = Runner(reg, _static_state_producer(None), iterations=10).run()
        iters = [v.iteration for v in result.violations]
        assert iters == [2, 8]
        # invariants_evaluated preserves registry insertion order.
        assert result.invariants_evaluated == ("early", "late")


# ---------------------------------------------------------------------------
# Runner constructor guards.
# ---------------------------------------------------------------------------


class TestRunnerConstructor:
    def test_iterations_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            Runner(InvariantRegistry(), _static_state_producer(None), iterations=0)


# ---------------------------------------------------------------------------
# RunResult shape.
# ---------------------------------------------------------------------------


class TestRunResult:
    def test_success_is_true_when_no_violations(self) -> None:
        reg = InvariantRegistry()
        reg.register(_AlwaysOk())
        result: RunResult = Runner(reg, _static_state_producer(None), iterations=5).run()
        assert result.success is True
        assert result.violations == ()
        assert result.iterations_completed == 5
