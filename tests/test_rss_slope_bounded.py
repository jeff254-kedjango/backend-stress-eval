"""Tests for :class:`harnesses.RssSlopeBoundedOnHarnessState`.

Planted-trajectory tests only — every input is a hand-built list of RSS
values so the fit output is deterministic and independent of real
allocator behaviour (Rule 9).
"""

from __future__ import annotations

from collections.abc import Mapping

from core.invariant import JsonValue, Ok, Violation
from core.metrics import Sample
from harnesses import HarnessState, RssSlopeBoundedOnHarnessState


def _state(rss_kb: int, trajectory: tuple[int, ...] = ()) -> HarnessState:
    """Build a HarnessState with a synthetic Sample. Sample fields other than
    ``rss_kb`` are irrelevant for the slope invariant."""
    return HarnessState(
        sample=Sample(
            monotonic_ns=1,
            rss_kb=rss_kb,
            fd_count=4,
            thread_count=1,
            gc_objects=0,
        ),
        route_signature=(),
        rss_trajectory=trajectory,
    )


def _evidence(v: Violation) -> Mapping[str, JsonValue]:
    """Kept for symmetry with the collapse-helper tests; :attr:`Violation.evidence`
    is already ``Mapping[str, JsonValue]`` so this is a no-op alias for
    readability at the call site."""
    return v.evidence


class TestRssSlopeBounded:
    def test_flat_trajectory_returns_ok(self) -> None:
        # 60 identical samples → zero slope → Ok. Fit floor is 50; 60 keeps
        # the invariant active while proving flat trajectories don't fire.
        inv = RssSlopeBoundedOnHarnessState(max_kb_per_iter=1.0)
        baseline = inv.setup(_state(rss_kb=10_000))
        state = _state(rss_kb=10_000, trajectory=tuple([10_000] * 60))
        result = inv.check(state, baseline, 59)
        assert isinstance(result, Ok)

    def test_slope_within_limit_returns_ok(self) -> None:
        # Grows +0.5 KB/iter over 60 samples — under the 1.0 KB/iter limit.
        inv = RssSlopeBoundedOnHarnessState(max_kb_per_iter=1.0)
        baseline = inv.setup(_state(rss_kb=10_000))
        traj = tuple(10_000 + int(0.5 * i) for i in range(60))
        state = _state(rss_kb=traj[-1], trajectory=traj)
        result = inv.check(state, baseline, len(traj) - 1)
        assert isinstance(result, Ok)

    def test_slope_above_limit_returns_violation_with_evidence(self) -> None:
        # +9 KB/iter — the actual FastAPI 0.141.1 leak shape.
        inv = RssSlopeBoundedOnHarnessState(max_kb_per_iter=1.0)
        baseline = inv.setup(_state(rss_kb=10_000))
        traj = tuple(10_000 + 9 * i for i in range(50))
        state = _state(rss_kb=traj[-1], trajectory=traj)
        result = inv.check(state, baseline, 49)
        assert isinstance(result, Violation)
        assert result.iteration == 49
        ev = _evidence(result)
        assert ev["baseline_kb"] == 10_000
        assert ev["samples"] == 50
        # Perfect line → slope exactly +9, R² = 1.0
        assert ev["slope_kb_per_iter"] == 9.0
        assert ev["r_squared"] == 1.0
        assert ev["max_kb_per_iter"] == 1.0
        # intercept should be ~10_000 (line starts at baseline)
        assert ev["intercept_kb"] == 10_000.0

    def test_short_trajectory_returns_ok_regardless_of_slope(self) -> None:
        # < 50 samples — fit noise floor is too high to trust the slope. We
        # fail *safe* rather than reporting a low-confidence slope. See the
        # ``_MIN_SLOPE_FIT_POINTS`` comment for the empirical rationale.
        inv = RssSlopeBoundedOnHarnessState(max_kb_per_iter=1.0)
        baseline = inv.setup(_state(rss_kb=10_000))
        # 30 samples, aggressively growing — still silent by design.
        traj = tuple(10_000 + 100 * i for i in range(30))
        state = _state(rss_kb=traj[-1], trajectory=traj)
        result = inv.check(state, baseline, len(traj) - 1)
        assert isinstance(result, Ok)

    def test_empty_trajectory_returns_ok(self) -> None:
        # Non-final iterations pass rss_trajectory=(). The invariant must
        # silently accept this — otherwise every non-end-only run flakes.
        inv = RssSlopeBoundedOnHarnessState(max_kb_per_iter=1.0)
        baseline = inv.setup(_state(rss_kb=10_000))
        state = _state(rss_kb=10_500, trajectory=())
        result = inv.check(state, baseline, 5)
        assert isinstance(result, Ok)

    def test_negative_slope_returns_ok(self) -> None:
        # If RSS actually shrinks (clean shutdown), slope < 0 < limit — Ok.
        inv = RssSlopeBoundedOnHarnessState(max_kb_per_iter=1.0)
        baseline = inv.setup(_state(rss_kb=10_000))
        traj = tuple(10_000 - 2 * i for i in range(60))
        state = _state(rss_kb=traj[-1], trajectory=traj)
        result = inv.check(state, baseline, len(traj) - 1)
        assert isinstance(result, Ok)

    def test_noisy_but_growing_trajectory_still_detected(self) -> None:
        # +2 KB/iter with ±3 KB alternating noise. Slope should still be
        # detected as growth; R² will be below 1.0 but reported in evidence.
        inv = RssSlopeBoundedOnHarnessState(max_kb_per_iter=1.0)
        baseline = inv.setup(_state(rss_kb=10_000))
        traj = tuple(10_000 + 2 * i + (3 if i % 2 == 0 else -3) for i in range(60))
        state = _state(rss_kb=traj[-1], trajectory=traj)
        result = inv.check(state, baseline, len(traj) - 1)
        assert isinstance(result, Violation)
        ev = _evidence(result)
        # Slope stays near +2 despite noise — exact value depends on how the
        # alternating perturbation lines up at the endpoints; 1.7..2.3 is a
        # generous but still-diagnostic band.
        slope = ev["slope_kb_per_iter"]
        assert isinstance(slope, float)
        assert 1.7 <= slope <= 2.3
        # R² noticeably below 1.0 given the noise.
        r2 = ev["r_squared"]
        assert isinstance(r2, float)
        assert 0.0 < r2 < 1.0

    def test_cadence_is_end_only(self) -> None:
        # The runner reads ``cadence`` on the invariant; end-only means the
        # fit fires exactly once per run. Rule 5: verified via public attr.
        inv = RssSlopeBoundedOnHarnessState()
        assert inv.cadence.end_only is True

    def test_detection_is_deterministic_across_repeats(self) -> None:
        # Rule 9 — planted trajectory, must be byte-stable.
        inv = RssSlopeBoundedOnHarnessState(max_kb_per_iter=1.0)
        baseline = inv.setup(_state(rss_kb=10_000))
        traj = tuple(10_000 + 9 * i for i in range(50))
        state = _state(rss_kb=traj[-1], trajectory=traj)
        outcomes = [inv.check(state, baseline, 49) for _ in range(10)]
        assert all(isinstance(o, Violation) for o in outcomes)
        # All ten violations should carry identical evidence dicts.
        first_ev = _evidence(outcomes[0])  # type: ignore[arg-type]
        for o in outcomes[1:]:
            assert dict(_evidence(o)) == dict(first_ev)  # type: ignore[arg-type]
