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

Requires ``pip install -e .[fastapi]`` from the backend-stress-eval repo,
then run this script from anywhere. The resulting report should match the
one adjacent to this file (byte-equal); any deviation means the environment
under test has changed.
\"\"\"

from __future__ import annotations

from pathlib import Path

from harnesses.discovery import run_discovery
from harnesses.eval_task import package_eval_task


def main() -> None:
    target_commit = TARGET_COMMIT_PLACEHOLDER
    reports = run_discovery(target_commit=target_commit)
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

    # 3) reproduce.py — a repro stub with the target commit inlined.
    # Pull the target_commit from any of the reports (they all share it).
    a_report = next(iter(reports.values()))
    stub = _REPRODUCE_STUB.replace(
        "TARGET_COMMIT_PLACEHOLDER", repr(a_report.metadata.target_commit)
    )
    (out_dir / "reproduce.py").write_text(stub, encoding="utf-8")

    return out_dir
