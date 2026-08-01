"""Tests for :mod:`core.sequence` — ordered stateful steps + per-step invariants.

Rule 9: all fixtures synthetic. Sequence must preserve declared order,
attach the correct step's index to any Violation, and fail fast on unknown
invariant references.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from core.invariant import CheckResult, InvariantRegistry, Ok, UnknownInvariantError, Violation
from core.sequence import Sequence, Step

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@dataclass
class _AlwaysOk:
    name: str = "always_ok"

    def setup(self, _state: object, /) -> None:
        return None

    def check(self, _state: object, _b: None, _i: int, /) -> CheckResult:
        return Ok(invariant_name=self.name)


@dataclass
class _FailsIfStateContains:
    """Fires a Violation iff ``state`` is a set-like containing ``needle``."""

    needle: str
    name: str = "fails_if_state_contains"

    def setup(self, _state: object, /) -> None:
        return None

    def check(self, state: object, _b: None, iteration: int, /) -> CheckResult:
        if isinstance(state, set) and self.needle in state:
            return Violation(
                invariant_name=self.name,
                detail=f"state contained {self.needle!r}",
                evidence={"needle": self.needle},
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)


@dataclass
class _RecordsSteps:
    """Records the iteration index every time it runs."""

    seen: list[int] = field(default_factory=list)
    name: str = "records_steps"

    def setup(self, _state: object, /) -> None:
        return None

    def check(self, _s: object, _b: None, iteration: int, /) -> CheckResult:
        self.seen.append(iteration)
        return Ok(invariant_name=self.name)


def _add(item: str) -> Callable[[object], object]:
    def _action(state: object) -> object:
        assert isinstance(state, set)
        return state | {item}

    return _action


def _remove(item: str) -> Callable[[object], object]:
    def _action(state: object) -> object:
        assert isinstance(state, set)
        return state - {item}

    return _action


def _identity(state: object) -> object:
    return state


def _new_set(_state: object) -> object:
    return set()


# ---------------------------------------------------------------------------
# Step guards.
# ---------------------------------------------------------------------------


class TestStep:
    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            Step(name="", action=_identity)

    def test_whitespace_padded_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="trimmed"):
            Step(name=" login ", action=_identity)


# ---------------------------------------------------------------------------
# Sequence constructor guards.
# ---------------------------------------------------------------------------


class TestSequenceConstructor:
    def test_empty_steps_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            Sequence(steps=(), registry=InvariantRegistry())

    def test_unknown_invariant_reference_rejected(self) -> None:
        reg = InvariantRegistry()
        reg.register(_AlwaysOk())
        with pytest.raises(UnknownInvariantError, match="ghost"):
            Sequence(
                steps=(Step(name="s1", action=_identity, invariants=("ghost",)),),
                registry=reg,
            )

    def test_known_invariants_accepted(self) -> None:
        reg = InvariantRegistry()
        reg.register(_AlwaysOk())
        seq = Sequence(
            steps=(Step(name="s1", action=_identity, invariants=("always_ok",)),),
            registry=reg,
        )
        assert len(seq.steps) == 1


# ---------------------------------------------------------------------------
# Execution: order + step-scoped invariants + deterministic RunResult.
# ---------------------------------------------------------------------------


class TestSequenceExecution:
    def test_steps_execute_in_declared_order(self) -> None:
        reg = InvariantRegistry()
        inv = _RecordsSteps()
        reg.register(inv)
        steps = tuple(
            Step(name=f"step_{i}", action=_identity, invariants=("records_steps",))
            for i in range(4)
        )
        result = Sequence(steps=steps, registry=reg).run(initial_state=None)
        assert result.success is True
        assert inv.seen == [0, 1, 2, 3]
        assert result.iterations_completed == 4

    def test_step_invariant_only_fires_within_its_step(self) -> None:
        # login → create(x) → delete(x) → check-for-x
        # The `fails_if_state_contains` invariant is only attached to `check`.
        reg = InvariantRegistry()
        reg.register(_FailsIfStateContains(needle="x"))
        steps = (
            Step(name="login", action=_new_set),
            Step(name="create", action=_add("x")),
            # 'create' does NOT declare the invariant; so no violation here
            # even though the state contains 'x'.
            Step(name="delete", action=_remove("x")),
            Step(
                name="check_no_x",
                action=_identity,
                invariants=("fails_if_state_contains",),
            ),
        )
        result = Sequence(steps=steps, registry=reg).run(initial_state=None)
        # After delete, 'x' is gone → invariant is Ok.
        assert result.success is True

    def test_planted_bug_in_correct_step_produces_violation(self) -> None:
        # If we forget the delete step, the check should catch it.
        reg = InvariantRegistry()
        reg.register(_FailsIfStateContains(needle="x"))
        steps = (
            Step(name="login", action=_new_set),
            Step(name="create", action=_add("x")),
            Step(
                name="check_no_x",
                action=_identity,
                invariants=("fails_if_state_contains",),
            ),
        )
        result = Sequence(steps=steps, registry=reg).run(initial_state=None)
        assert result.success is False
        assert len(result.violations) == 1
        # Violation's iteration is the step index — 2 (0-indexed).
        assert result.violations[0].iteration == 2

    def test_stop_on_first_violation_halts_the_sequence(self) -> None:
        reg = InvariantRegistry()
        reg.register(_FailsIfStateContains(needle="x"))
        after_break = _RecordsSteps(name="after")
        reg.register(after_break)
        steps = (
            Step(name="login", action=_new_set),
            Step(name="create", action=_add("x")),
            Step(
                name="fail_here",
                action=_identity,
                invariants=("fails_if_state_contains",),
            ),
            # This step's invariant should NOT run — we stopped.
            Step(name="unreached", action=_identity, invariants=("after",)),
        )
        result = Sequence(steps=steps, registry=reg, stop_on_first_violation=True).run(
            initial_state=None
        )
        assert result.success is False
        assert result.iterations_completed == 3  # stopped after step 2 (0..2)
        assert after_break.seen == []  # never fired

    def test_same_input_same_output_ten_repeats(self) -> None:
        # Determinism.
        payloads: list[tuple[Violation, ...]] = []
        for _ in range(10):
            reg = InvariantRegistry()
            reg.register(_FailsIfStateContains(needle="x"))
            steps = (
                Step(name="login", action=_new_set),
                Step(name="create", action=_add("x")),
                Step(
                    name="check",
                    action=_identity,
                    invariants=("fails_if_state_contains",),
                ),
            )
            payloads.append(Sequence(steps=steps, registry=reg).run(initial_state=None).violations)
        first = payloads[0]
        for p in payloads[1:]:
            assert p == first
