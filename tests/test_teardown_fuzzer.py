"""Tests for :mod:`harnesses.teardown_fuzzer`.

Stub plugin declares three hooks; some permutations raise, others don't.
Fuzzer walks all 3! = 6 orders, records outcomes, and flags divergent
orders.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import pytest

from harnesses.teardown_fuzzer import (
    TEARDOWN_FUZZ_SCHEMA_VERSION,
    TEARDOWN_MAX_HOOKS,
    OrderResult,
    TeardownFuzzError,
    TeardownFuzzReport,
    run_teardown_fuzzer,
)


# ---------------------------------------------------------------------------
# Stub plugin — three hooks. Configure per-test which orderings raise.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _StubApp:
    torn_down: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _StubTeardownPlugin:
    """Plugin exposing three hooks with configurable failure orders.

    ``failing_orders`` — tuple of orderings that should raise.
    """

    name: str = "stub-teardown"
    hooks_returned: tuple[str, ...] = ("close_db", "flush_queue", "shutdown_metrics")
    failing_orders: tuple[tuple[str, ...], ...] = ()

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

    # TeardownAware surface:
    def teardown_hooks(self, app: _StubApp, /) -> tuple[str, ...]:
        return self.hooks_returned

    def run_teardown(self, app: _StubApp, order: tuple[str, ...], /) -> None:
        if set(order) != set(self.hooks_returned):
            raise ValueError(f"non-permutation order {order!r}")
        if order in self.failing_orders:
            raise RuntimeError(f"teardown failed for order {order!r}")
        app.torn_down.extend(order)


@dataclass(slots=True)
class _StubBasePlugin:
    """Doesn't implement TeardownAware."""

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
# run_teardown_fuzzer end-to-end.
# ---------------------------------------------------------------------------
class TestRunTeardownFuzzer:
    def test_non_teardown_aware_rejected(self) -> None:
        with pytest.raises(TeardownFuzzError, match="TeardownAware"):
            run_teardown_fuzzer(plugin=_StubBasePlugin(), target_commit="A")

    def test_empty_hooks_rejected(self) -> None:
        plugin = _StubTeardownPlugin(hooks_returned=())
        with pytest.raises(TeardownFuzzError, match="Nothing to permute"):
            run_teardown_fuzzer(plugin=plugin, target_commit="A")

    def test_too_many_hooks_rejected(self) -> None:
        # 5 hooks > TEARDOWN_MAX_HOOKS (4). Even one over the ceiling raises.
        assert TEARDOWN_MAX_HOOKS == 4  # regression pin
        plugin = _StubTeardownPlugin(hooks_returned=("a", "b", "c", "d", "e"))
        with pytest.raises(TeardownFuzzError, match="capped at"):
            run_teardown_fuzzer(plugin=plugin, target_commit="A")

    def test_all_orders_clean_produces_no_divergence(self) -> None:
        plugin = _StubTeardownPlugin(failing_orders=())
        report = run_teardown_fuzzer(plugin=plugin, target_commit="A")
        # 3! = 6 permutations.
        assert len(report.results) == 6
        assert report.canonical_result.is_canonical
        assert report.canonical_result.raised is None
        assert report.divergent_orders == ()
        assert not report.has_divergence

    def test_divergent_order_flagged(self) -> None:
        # Make one non-canonical order raise; canonical stays clean.
        failing = (("flush_queue", "close_db", "shutdown_metrics"),)
        plugin = _StubTeardownPlugin(failing_orders=failing)
        report = run_teardown_fuzzer(plugin=plugin, target_commit="A")
        divs = report.divergent_orders
        assert len(divs) == 1
        assert divs[0].order == failing[0]
        assert "RuntimeError" in (divs[0].raised or "")
        assert report.has_divergence

    def test_canonical_order_matches_teardown_hooks(self) -> None:
        plugin = _StubTeardownPlugin()
        report = run_teardown_fuzzer(plugin=plugin, target_commit="A")
        assert report.hooks == plugin.hooks_returned
        assert report.canonical_result.order == plugin.hooks_returned


# ---------------------------------------------------------------------------
# TeardownFuzzReport.to_json — byte-stability.
# ---------------------------------------------------------------------------
class TestJsonSerialization:
    def test_to_json_sort_keyed(self) -> None:
        import json

        report = TeardownFuzzReport(
            schema_version=TEARDOWN_FUZZ_SCHEMA_VERSION,
            plugin_name="stub",
            target_commit="A",
            hooks=("a", "b"),
            results=(
                OrderResult(order=("a", "b"), is_canonical=True, raised=None),
                OrderResult(order=("b", "a"), is_canonical=False, raised="RuntimeError: boom"),
            ),
        )
        payload = report.to_json()
        parsed = json.loads(payload)
        assert parsed["schema_version"] == TEARDOWN_FUZZ_SCHEMA_VERSION
        assert list(parsed.keys()) == sorted(parsed.keys())
        # Result rows preserved in insertion order.
        assert parsed["results"][0]["is_canonical"] is True


# ---------------------------------------------------------------------------
# CLI wiring — negative paths.
# ---------------------------------------------------------------------------
class TestCli:
    def test_unknown_plugin_exits_precondition(self, capsys: pytest.CaptureFixture[str]) -> None:
        from cli.main import EXIT_TEARDOWN_PRECONDITION, main

        rc = main(["teardown-fuzz", "does_not_exist"])
        assert rc == EXIT_TEARDOWN_PRECONDITION
        assert "unknown plugin" in capsys.readouterr().err.lower()

    def test_non_opted_in_plugin_exits_precondition(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from cli.main import EXIT_TEARDOWN_PRECONDITION, main

        rc = main(["teardown-fuzz", "stub"])
        assert rc == EXIT_TEARDOWN_PRECONDITION
        assert "TeardownAware" in capsys.readouterr().err
