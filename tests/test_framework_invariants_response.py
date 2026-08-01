"""Tests for :class:`core.framework_invariants.ResponseDeterminism`.

Rule 9: synthetic fixtures — a stub with a mutable ``response_digest`` field.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.framework_invariants import ResponseDeterminism
from core.invariant import Ok, Violation


@dataclass
class _Stub:
    response_digest: str | None


class TestResponseDeterminism:
    def test_matching_digest_returns_ok(self) -> None:
        inv = ResponseDeterminism()
        s = _Stub(response_digest="deadbeef")
        baseline = inv.setup(s)
        result = inv.check(s, baseline, 0)
        assert isinstance(result, Ok)

    def test_differing_digest_returns_violation(self) -> None:
        inv = ResponseDeterminism()
        s = _Stub(response_digest="aaaa")
        baseline = inv.setup(s)
        s.response_digest = "bbbb"
        result = inv.check(s, baseline, 3)
        assert isinstance(result, Violation)
        assert result.iteration == 3
        assert result.evidence["baseline_digest"] == "aaaa"
        assert result.evidence["current_digest"] == "bbbb"

    def test_none_baseline_never_violates(self) -> None:
        # If no baseline captured (setup saw None), any current digest is Ok.
        inv = ResponseDeterminism()
        s = _Stub(response_digest=None)
        baseline = inv.setup(s)
        assert baseline is None
        s.response_digest = "later"
        result = inv.check(s, baseline, 0)
        assert isinstance(result, Ok)

    def test_none_current_never_violates(self) -> None:
        # A current None means "no probe this iter" — skip the check.
        inv = ResponseDeterminism()
        s = _Stub(response_digest="fixed")
        baseline = inv.setup(s)
        s.response_digest = None
        result = inv.check(s, baseline, 0)
        assert isinstance(result, Ok)
