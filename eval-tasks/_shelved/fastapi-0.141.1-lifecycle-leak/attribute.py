#!/usr/bin/env python3
"""One-shot tracemalloc attribution for the FastAPI 0.141.1 lifecycle leak.

Chunk 6b (novelty prerequisite): the L2 harness measures +8.72 KB/iter
of drift on the current pin, but upstream PR #16049 (FastAPI 0.140.0)
already refactored ``_CallIdentity`` in ``fastapi/dependencies/models.py``
for a 16x reduction in per-``Dependant`` footprint. Our pin (0.141.1)
contains that fix and still shows the drift.

This script runs the same loop shape as ``harnesses/layer2_lifecycle``
(build → lifecycle_start → probe → lifecycle_stop, N rounds), takes
tracemalloc snapshots at iteration 10 (after warmup) and iteration 490
(near the end), and prints the top allocating locations by growth.

The output tells us whether the residual growth attributes to the
already-fixed dependency-graph region (i.e., not novel — same class as
#16049, just incompletely fixed) or somewhere distinct (candidate for
novel bug).

Not part of the runtime harness — intentionally standalone so it doesn't
pollute the L2 code path with debug plumbing.
"""

from __future__ import annotations

import argparse
import sys
import tracemalloc
from pathlib import Path

# Repo-root import path so this can run via ``python eval-tasks/.../attribute.py``.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.fastapi import FastAPIPlugin, canonical_example_app  # noqa: E402

_DEFAULT_ROUNDS = 500
_SNAPSHOT_WARMUP_ITER = 10
_TOP_N = 20
_SHORT_PATH_COMPONENTS = 3  # tail this many path parts when trimming for display


def _run(rounds: int, warmup_iter: int, top_n: int) -> int:
    if rounds <= warmup_iter + 1:
        print(
            f"error: rounds ({rounds}) must exceed warmup_iter ({warmup_iter}) + 1",
            file=sys.stderr,
        )
        return 2

    plugin = FastAPIPlugin(app_factory=canonical_example_app)

    tracemalloc.start(25)  # keep 25 frames of Python-level attribution

    snap_early: tracemalloc.Snapshot | None = None
    snap_late: tracemalloc.Snapshot | None = None
    final_iter = rounds - 1

    for i in range(rounds):
        app = plugin.build_app()
        plugin.lifecycle_start(app)
        try:
            client = plugin.client(app)
            plugin.probe(client)
        finally:
            plugin.lifecycle_stop(app)

        if i == warmup_iter:
            snap_early = tracemalloc.take_snapshot()
        elif i == final_iter:
            snap_late = tracemalloc.take_snapshot()

    tracemalloc.stop()

    if snap_early is None or snap_late is None:
        print("error: failed to capture both snapshots", file=sys.stderr)
        return 2

    diff = snap_late.compare_to(snap_early, "lineno")

    span_iters = final_iter - warmup_iter
    total_growth_kb = sum(s.size_diff for s in diff) / 1024
    per_iter = total_growth_kb / span_iters
    print("# tracemalloc attribution — fastapi 0.141.1 lifecycle leak")
    print(f"# rounds: {rounds}   snapshot span: iter {warmup_iter} -> iter {final_iter}")
    print(f"# iterations spanned: {span_iters}")
    print(f"# total heap delta:  {total_growth_kb:+.2f} KB  ({per_iter:+.4f} KB/iter)")
    print()
    print(f"# Top {top_n} allocating locations by size_diff (bytes):")
    print(f"# {'size_diff_kb':>12}  {'count_diff':>10}  {'kb_per_iter':>11}  location")
    for stat in diff[:top_n]:
        loc = stat.traceback[0]
        kb_diff = stat.size_diff / 1024
        kb_per_iter = kb_diff / span_iters
        # Trim filename to the last few path components so lines fit.
        parts = Path(loc.filename).parts
        if len(parts) > _SHORT_PATH_COMPONENTS:
            short = "/".join(parts[-_SHORT_PATH_COMPONENTS:])
        else:
            short = loc.filename
        row = (
            f"  {kb_diff:>+11.2f}   {stat.count_diff:>+10d}  "
            f"{kb_per_iter:>+10.4f}  {short}:{loc.lineno}"
        )
        print(row)
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--rounds", type=int, default=_DEFAULT_ROUNDS)
    ap.add_argument("--warmup-iter", type=int, default=_SNAPSHOT_WARMUP_ITER)
    ap.add_argument("--top", type=int, default=_TOP_N)
    ns = ap.parse_args(argv[1:])
    return _run(rounds=ns.rounds, warmup_iter=ns.warmup_iter, top_n=ns.top)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
