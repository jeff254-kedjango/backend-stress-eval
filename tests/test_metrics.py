"""Tests for ``core.metrics`` — sampler correctness + planted-leak detection.

Rule 9 discipline: every failure branch is triggered by a *planted* fixture,
not by wall-clock timing or RSS thresholds that could flake. We never assert
"real RSS grew by exactly N bytes"; we assert on synthetic Samples we
construct directly.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from core.invariant import Ok, Violation
from core.metrics import (
    MetricsUnsupportedError,
    RssReturnToBaseline,
    Sample,
    delta,
    sample,
)

# ---------------------------------------------------------------------------
# Sample() smoke tests — non-negativity, structure, platform gate.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="core.metrics currently requires Linux /proc",
)
class TestSample:
    def test_returns_non_negative_fields(self) -> None:
        s = sample()
        assert s.rss_kb > 0
        assert s.fd_count >= 3  # stdin/stdout/stderr guaranteed
        assert s.thread_count >= 1
        assert s.gc_objects >= 0
        assert s.monotonic_ns > 0

    def test_is_frozen(self) -> None:
        s = sample()
        with pytest.raises(Exception):  # noqa: B017 — FrozenInstanceError subclass
            s.rss_kb = 0  # type: ignore[misc]

    def test_monotonic_increases_between_samples(self) -> None:
        # Real Rule-9 measurement: we don't assert on wall time, only that
        # ``monotonic_ns`` never goes backwards. Deterministic.
        first = sample()
        time.sleep(0.001)  # 1 ms — enough for monotonic_ns to advance
        second = sample()
        assert second.monotonic_ns > first.monotonic_ns


@pytest.mark.skipif(
    sys.platform.startswith("linux"),
    reason="Only meaningful on non-Linux platforms",
)
class TestNonLinuxRaises:
    def test_sample_raises_metrics_unsupported(self) -> None:
        with pytest.raises(MetricsUnsupportedError):
            sample()


# ---------------------------------------------------------------------------
# delta() — pure arithmetic on synthetic Samples.
# ---------------------------------------------------------------------------


def _make_sample(
    *,
    monotonic_ns: int = 0,
    rss_kb: int = 0,
    fd_count: int = 0,
    thread_count: int = 1,
    gc_objects: int = 0,
) -> Sample:
    return Sample(
        monotonic_ns=monotonic_ns,
        rss_kb=rss_kb,
        fd_count=fd_count,
        thread_count=thread_count,
        gc_objects=gc_objects,
    )


class TestDelta:
    def test_zero_delta_between_identical_samples(self) -> None:
        s = _make_sample(monotonic_ns=100, rss_kb=1000, fd_count=5)
        d = delta(s, s)
        assert d.elapsed_ns == 0
        assert d.rss_kb == 0
        assert d.fd_count == 0
        assert d.thread_count == 0
        assert d.gc_objects == 0

    def test_positive_delta_when_after_grew(self) -> None:
        before = _make_sample(monotonic_ns=100, rss_kb=1000, fd_count=5)
        after = _make_sample(monotonic_ns=200, rss_kb=1500, fd_count=7)
        d = delta(before, after)
        assert d.elapsed_ns == 100
        assert d.rss_kb == 500
        assert d.fd_count == 2

    def test_negative_delta_when_after_shrank(self) -> None:
        before = _make_sample(rss_kb=2000, fd_count=10)
        after = _make_sample(rss_kb=1500, fd_count=8)
        d = delta(before, after)
        assert d.rss_kb == -500
        assert d.fd_count == -2


# ---------------------------------------------------------------------------
# RssReturnToBaseline — the first concrete Invariant.
# Planted-fixture tests: state is fully synthetic. No flaky RSS thresholds.
# ---------------------------------------------------------------------------


class TestRssReturnToBaseline:
    def test_stable_rss_returns_ok(self) -> None:
        inv = RssReturnToBaseline(slack_kb=100)
        baseline_sample = _make_sample(rss_kb=10_000)
        baseline = inv.setup(baseline_sample)
        # Current RSS 50 KB above baseline — well inside slack.
        result = inv.check(_make_sample(rss_kb=10_050), baseline, 0)
        assert isinstance(result, Ok)
        assert result.invariant_name == "rss_return_to_baseline"

    def test_drift_within_slack_returns_ok(self) -> None:
        inv = RssReturnToBaseline(slack_kb=100)
        baseline = inv.setup(_make_sample(rss_kb=10_000))
        # Exactly at slack — boundary. > is the guard, so == is Ok.
        result = inv.check(_make_sample(rss_kb=10_100), baseline, 0)
        assert isinstance(result, Ok)

    def test_drift_beyond_slack_returns_violation_with_evidence(self) -> None:
        inv = RssReturnToBaseline(slack_kb=100)
        baseline = inv.setup(_make_sample(rss_kb=10_000))
        # Simulate a leak: 500 KB above baseline; slack is 100.
        # Protocol signature is positional-only (see Chunk 2 memory).
        result = inv.check(_make_sample(rss_kb=10_500), baseline, 37)
        assert isinstance(result, Violation)
        assert result.iteration == 37
        assert result.evidence["baseline_kb"] == 10_000
        assert result.evidence["current_kb"] == 10_500
        assert result.evidence["drift_kb"] == 500
        assert result.evidence["slack_kb"] == 100

    def test_detection_is_deterministic_across_repeats(self) -> None:
        # Rule 9 — the harness's whole point.
        inv = RssReturnToBaseline(slack_kb=100)
        baseline = inv.setup(_make_sample(rss_kb=10_000))
        outcomes = [
            isinstance(inv.check(_make_sample(rss_kb=10_500), baseline, i), Violation)
            for i in range(10)
        ]
        assert outcomes == [True] * 10

    def test_violation_is_json_serialisable(self) -> None:
        inv = RssReturnToBaseline(slack_kb=100)
        baseline = inv.setup(_make_sample(rss_kb=10_000))
        result = inv.check(_make_sample(rss_kb=10_500), baseline, 5)
        assert isinstance(result, Violation)
        payload = json.dumps(
            {
                "invariant_name": result.invariant_name,
                "detail": result.detail,
                "iteration": result.iteration,
                "evidence": dict(result.evidence),
            }
        )
        loaded = json.loads(payload)
        assert loaded["evidence"]["drift_kb"] == 500

    def test_shrinking_rss_returns_ok(self) -> None:
        # If cleanup actually happens, RSS may go *below* baseline. That's
        # never a violation — only upward drift matters.
        inv = RssReturnToBaseline(slack_kb=100)
        baseline = inv.setup(_make_sample(rss_kb=10_000))
        result = inv.check(_make_sample(rss_kb=9_000), baseline, 0)
        assert isinstance(result, Ok)
