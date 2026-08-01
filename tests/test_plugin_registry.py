"""Tests for :mod:`plugins.registry` — auto-discovery of ``plugins/<name>/manifest.py``.

Rule 9: no timing, no environmental randomness. All assertions are on the
actual manifests currently present in the repo (``fastapi``, ``stub``).
"""

from __future__ import annotations

from plugins.registry import Manifest, load_manifests


class TestManifestDiscovery:
    def test_discovers_bundled_plugins(self) -> None:
        manifests = load_manifests()
        # These two ship with the repo and should always be found.
        assert "fastapi" in manifests
        assert "stub" in manifests

    def test_manifest_records_are_the_declared_type(self) -> None:
        for m in load_manifests().values():
            assert isinstance(m, Manifest)

    def test_fastapi_manifest_shape(self) -> None:
        m = load_manifests()["fastapi"]
        assert m.name == "fastapi"
        assert m.pip_packages == ("fastapi", "starlette", "httpx2")
        assert m.resolve_version_package() == "fastapi"
        # Layer-3 variants declared for FastAPI.
        assert {name for name, _ in m.variants} == {"minimal", "canonical"}

    def test_stub_manifest_shape(self) -> None:
        m = load_manifests()["stub"]
        assert m.name == "stub"
        assert m.pip_packages == ()
        # Zero runtime deps → no version_package.
        assert m.resolve_version_package() is None
        # Stub has no Layer-3 variants — plugin author's choice.
        assert m.variants == ()

    def test_default_target_commit_uses_version(self) -> None:
        m = load_manifests()["fastapi"]
        assert m.default_target_commit("0.141.1") == "fastapi-0.141.1"
        assert m.default_target_commit(None) == "fastapi-installed"

    def test_view_is_read_only(self) -> None:
        # load_manifests returns a MappingProxyType view.
        import pytest

        m = load_manifests()
        with pytest.raises(TypeError):
            m["evil"] = None  # type: ignore[index]
