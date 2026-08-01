#!/usr/bin/env python3
"""Measure anyio's lifecycle leak — 500-round soak, tracemalloc diff.

Runs the minimal reproducer (``minimal_repro.py``) for --rounds iterations
under tracemalloc, takes snapshots at iter ``--warmup-iter`` and final,
computes the ``compare_to("lineno")`` diff, and writes a JSON report to
--out (default: stdout).

The JSON shape is the grading contract (see RUBRIC.md G1-G4). Byte-stable
across runs: sorted top-lines by size_diff descending, floats rounded to
4dp, keys in fixed order.

Depends only on the Python stdlib and ``anyio``. No FastAPI/Starlette.
"""

from __future__ import annotations

import argparse
import json
import sys
import tracemalloc
from importlib.metadata import version
from pathlib import Path

import anyio

_DEFAULT_ROUNDS = 500
_SNAPSHOT_WARMUP_ITER = 10
_TOP_N = 10  # store more than 5 so RUBRIC G3 can look at the whole list
_ANYIO_PACKAGE_ROOT_HINT = "anyio"  # substring used to trim paths to "anyio/..."
_SCHEMA_VERSION = "1"
_MIN_PATH_TAIL_COMPONENTS = 2  # fallback: keep at least this many components


async def _work() -> None:
    """Invoke anyio's worker pool once.

    ``int()`` is the cheapest sync callable that satisfies
    ``anyio.to_thread.run_sync``. The whole point is to reach
    ``AsyncIOBackend.run_sync_in_worker_thread`` — the value returned is
    thrown away.
    """
    await anyio.to_thread.run_sync(int)


def _trim_path(filename: str) -> str:
    """Normalize to a portable, venv-independent path.

    Two rules, in order:

    - If the path contains ``site-packages/``, keep only what follows.
      This turns ``.../lib/python3.12/site-packages/anyio/_backends/_asyncio.py``
      into ``anyio/_backends/_asyncio.py`` on every venv layout.
    - Else, if the path contains ``lib/pythonX.Y/`` (a stdlib file),
      keep only what follows (e.g. ``asyncio/base_events.py``).

    If neither matches, return the last two components — enough to be
    readable, not so much that layout changes matter.
    """
    posix = filename.replace("\\", "/")
    idx = posix.find("site-packages/")
    if idx != -1:
        return posix[idx + len("site-packages/") :]
    # Find "lib/pythonX.Y/" for any minor.
    for token in ("/lib/python3.12/", "/lib/python3.13/", "/lib/python3.14/"):
        j = posix.find(token)
        if j != -1:
            return posix[j + len(token) :]
    parts = Path(filename).parts
    if len(parts) >= _MIN_PATH_TAIL_COMPONENTS:
        return "/".join(parts[-_MIN_PATH_TAIL_COMPONENTS:])
    return filename


def _measure(rounds: int, warmup_iter: int) -> dict[str, object]:
    if rounds <= warmup_iter + 1:
        raise ValueError(f"rounds ({rounds}) must exceed warmup_iter ({warmup_iter}) + 1")

    tracemalloc.start(25)
    snap_early: tracemalloc.Snapshot | None = None
    snap_late: tracemalloc.Snapshot | None = None
    final_iter = rounds - 1

    for i in range(rounds):
        anyio.run(_work)
        if i == warmup_iter:
            snap_early = tracemalloc.take_snapshot()
        elif i == final_iter:
            snap_late = tracemalloc.take_snapshot()

    tracemalloc.stop()

    if snap_early is None or snap_late is None:
        raise RuntimeError("failed to capture both snapshots")

    diff = snap_late.compare_to(snap_early, "lineno")
    span_iters = final_iter - warmup_iter
    total_delta_kb = sum(s.size_diff for s in diff) / 1024
    slope_kb_per_iter = total_delta_kb / span_iters

    top_lines: list[dict[str, object]] = []
    for stat in diff[:_TOP_N]:
        loc = stat.traceback[0]
        top_lines.append(
            {
                "file": _trim_path(loc.filename),
                "lineno": loc.lineno,
                "kb_diff": round(stat.size_diff / 1024, 4),
                "count_diff": stat.count_diff,
            }
        )

    return {
        "schema_version": _SCHEMA_VERSION,
        "anyio_version": version("anyio"),
        "python_version": ".".join(str(x) for x in sys.version_info[:3]),
        "rounds": rounds,
        "warmup_iter": warmup_iter,
        "span_iters": span_iters,
        "slope_kb_per_iter": round(slope_kb_per_iter, 4),
        "total_delta_kb": round(total_delta_kb, 2),
        "top_lines": top_lines,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--rounds", type=int, default=_DEFAULT_ROUNDS)
    ap.add_argument("--warmup-iter", type=int, default=_SNAPSHOT_WARMUP_ITER)
    ap.add_argument(
        "--out",
        type=str,
        default="-",
        help="output file for the JSON report (default '-' == stdout)",
    )
    ns = ap.parse_args(argv[1:])

    report = _measure(rounds=ns.rounds, warmup_iter=ns.warmup_iter)
    blob = json.dumps(report, indent=2, sort_keys=True) + "\n"

    if ns.out == "-":
        sys.stdout.write(blob)
    else:
        Path(ns.out).write_text(blob, encoding="utf-8")
        print(f"wrote {ns.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
