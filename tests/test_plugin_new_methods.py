"""Tests for the three R1-hoisted Plugin methods on both bundled plugins.

Rule 6 verification: after the refactor, ``probe``/``route_signature``/
``response_digest`` live on the plugin. These tests lock the contract for
both bundled plugins so future refactors that regress them fail loudly.
"""

from __future__ import annotations

from plugins.fastapi import FastAPIPlugin, canonical_example_app, minimal_example_app
from plugins.stub import StubPlugin


class TestFastAPINewMethods:
    def test_route_signature_is_sorted_tuple_of_strings(self) -> None:
        plugin = FastAPIPlugin(app_factory=canonical_example_app)
        app = plugin.build_app()
        try:
            sig = plugin.route_signature(app)
        finally:
            plugin.lifecycle_stop(app)
        assert isinstance(sig, tuple)
        assert all(isinstance(s, str) for s in sig)
        assert list(sig) == sorted(sig)
        # canonical_example_app declares GET / and GET /di.
        assert any(s.startswith("GET /") for s in sig)

    def test_route_signature_stable_across_calls(self) -> None:
        plugin = FastAPIPlugin(app_factory=canonical_example_app)
        app = plugin.build_app()
        try:
            first = plugin.route_signature(app)
            second = plugin.route_signature(app)
        finally:
            plugin.lifecycle_stop(app)
        assert first == second

    def test_route_signature_differs_between_minimal_and_canonical(self) -> None:
        minimal = FastAPIPlugin(app_factory=minimal_example_app).build_app()
        canonical = FastAPIPlugin(app_factory=canonical_example_app).build_app()
        m_sig = FastAPIPlugin(app_factory=minimal_example_app).route_signature(minimal)
        c_sig = FastAPIPlugin(app_factory=canonical_example_app).route_signature(canonical)
        assert m_sig != c_sig  # canonical has /di, minimal doesn't

    def test_probe_does_not_raise(self) -> None:
        plugin = FastAPIPlugin(app_factory=canonical_example_app)
        app = plugin.build_app()
        client = plugin.client(app)
        try:
            # probe() returns None per the Plugin contract — success is the
            # absence of a raised exception, not a return value.
            plugin.probe(client)
        finally:
            plugin.lifecycle_stop(app)

    def test_response_digest_is_stable_hex(self) -> None:
        plugin = FastAPIPlugin(app_factory=canonical_example_app)
        app = plugin.build_app()
        try:
            first = plugin.response_digest(app)
            second = plugin.response_digest(app)
        finally:
            plugin.lifecycle_stop(app)
        assert first is not None
        assert first == second
        assert len(first) == 64  # sha256 hex


class TestStubNewMethods:
    def test_route_signature_is_fixed_tuple(self) -> None:
        p = StubPlugin()
        app = p.build_app()
        assert p.route_signature(app) == ("ISSUE /request",)

    def test_probe_increments_request_count(self) -> None:
        p = StubPlugin()
        app = p.build_app()
        client = p.client(app)
        assert app.request_count == 0
        p.probe(client)
        assert app.request_count == 1

    def test_response_digest_stable_across_probes_for_clean_app(self) -> None:
        p = StubPlugin(planted_leak=False)
        app = p.build_app()
        client = p.client(app)
        first = p.response_digest(app)
        p.probe(client)
        p.reset(app)
        second = p.response_digest(app)
        assert first == second, "clean stub must return stable digest across requests"

    def test_response_digest_drifts_when_planted_leak(self) -> None:
        p = StubPlugin(planted_leak=True)
        app = p.build_app()
        client = p.client(app)
        first = p.response_digest(app)
        p.probe(client)  # bumps leaked_kb
        p.reset(app)  # does NOT clear leaked_kb (that's the fixture)
        second = p.response_digest(app)
        assert first != second, "planted-leak stub must drift the digest"
