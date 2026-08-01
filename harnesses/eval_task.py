"""Eval-task packager — writes a discovery run to a byte-stable directory.

Given ``reports: dict[str, Report]`` and an output directory, produces:

* ``report.json`` — byte-stable JSON aggregating every layer's Report under
  its layer name key. Reuses :func:`core.reporter.to_json` per layer so the
  Chunk-5 contract still holds.
* ``summary.txt`` — one :func:`core.reporter.human_summary` per layer.
* ``reproduce.py`` — a minimal, self-contained runnable stub showing how a
  grader replays the harness against the same target commit.

The whole output is deterministic: running the packager twice with the same
input dict yields byte-identical files.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from core.reporter import Report, human_summary, to_json

__all__ = ["DISCOVERY_SCHEMA_VERSION", "package_eval_task"]


DISCOVERY_SCHEMA_VERSION: Final = "1"


def _discovery_bytes(reports: Mapping[str, Report]) -> bytes:
    """Aggregate every layer's Report into one byte-stable JSON blob.

    Layer names are sorted → same-input → same-bytes. Each layer's payload
    is the exact bytes :func:`core.reporter.to_json` would produce for it,
    round-tripped through ``json.loads`` so the outer document stays a
    single valid JSON object.
    """
    layers: dict[str, object] = {}
    for name in sorted(reports):
        layer_bytes = to_json(reports[name])
        layers[name] = json.loads(layer_bytes.decode("utf-8"))
    payload = {
        "discovery_schema_version": DISCOVERY_SCHEMA_VERSION,
        "layers": layers,
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _summary_text(reports: Mapping[str, Report]) -> str:
    """One human summary per layer, layer names sorted."""
    lines: list[str] = []
    for name in sorted(reports):
        lines.append(f"=== {name} ===")
        lines.append(human_summary(reports[name]))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_REPRODUCE_STUB: Final = """\
#!/usr/bin/env python3
\"\"\"Replay the discovery sweep for this target_commit.

Requires ``pip install -e .[<plugin>]`` from the backend-stress-eval repo,
then run this script from anywhere. The resulting report should match the
one adjacent to this file (byte-equal); any deviation means the environment
under test has changed.
\"\"\"

from __future__ import annotations

from pathlib import Path

from harnesses.discovery import run_discovery
from harnesses.eval_task import package_eval_task
from plugins.registry import load_manifests


def main() -> None:
    plugin_name = PLUGIN_NAME_PLACEHOLDER
    target_commit = TARGET_COMMIT_PLACEHOLDER

    manifests = load_manifests()
    if plugin_name not in manifests:
        raise RuntimeError(
            f\"plugin {plugin_name!r} not registered; \"
            f\"available: {sorted(manifests)}. Did you `pip install -e .`?\"
        )
    manifest = manifests[plugin_name]
    plugin = manifest.plugin_factory(manifest.default_app_factory)
    variants = manifest.variants or None
    variant_plugin_factory = (
        (lambda af: manifest.plugin_factory(af)) if variants is not None else None
    )

    reports = run_discovery(
        plugin=plugin,
        target_commit=target_commit,
        variants=variants,
        variant_plugin_factory=variant_plugin_factory,
    )
    out_dir = Path(__file__).parent / \"replay\"
    package_eval_task(reports=reports, out_dir=out_dir)
    print(f\"wrote replay to {out_dir}\")


if __name__ == \"__main__\":
    main()
"""


def package_eval_task(*, reports: Mapping[str, Report], out_dir: Path) -> Path:
    """Write ``report.json``, ``summary.txt``, and ``reproduce.py``.

    Returns ``out_dir``. Creates ``out_dir`` if it does not exist. Rule 1
    complexity: O(len(reports) + total_violations). Rule 9: byte-stable
    output — running this twice against the same input yields identical
    files on disk.
    """
    if not reports:
        raise ValueError("reports must not be empty")

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) report.json — the byte-stable grading artifact.
    (out_dir / "report.json").write_bytes(_discovery_bytes(reports))

    # 2) summary.txt — human-readable.
    (out_dir / "summary.txt").write_text(_summary_text(reports), encoding="utf-8")

    # 3) reproduce.py — a repro stub with target commit + plugin name inlined.
    # ``target_commit`` is shared across reports; ``target`` names the plugin
    # for per-layer plugin reports and the sentinel ``"variants"`` for the
    # Layer 3 aggregate. Pick the first non-``"variants"`` label so the stub
    # can invoke the registry cleanly; if only variants exist, fall back to
    # ``"variants"`` (grader will see a clear registry-lookup error rather
    # than a silent misroute — Rule 5).
    a_report = next(iter(reports.values()))
    target_commit = a_report.metadata.target_commit
    plugin_name = next(
        (r.metadata.target for r in reports.values() if r.metadata.target != "variants"),
        a_report.metadata.target,
    )
    stub = _REPRODUCE_STUB.replace("TARGET_COMMIT_PLACEHOLDER", repr(target_commit)).replace(
        "PLUGIN_NAME_PLACEHOLDER", repr(plugin_name)
    )
    (out_dir / "reproduce.py").write_text(stub, encoding="utf-8")

    return out_dir
