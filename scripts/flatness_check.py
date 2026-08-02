#!/usr/bin/env python3
"""Long-run flatness probe for the anyio lifecycle leak.

Runs the reproducer workload for many iterations and reports the RSS slope
(KB/iter) over the steady-state window, plus live event-loop count. A fixed
anyio holds flat (slope ~0, loops ~1); the stock leak climbs linearly
(loops == rounds).

Usage: <python> flatness_check.py [rounds]
Emits one line of JSON so a caller can compare venvs.
"""
from __future__ import annotations

import gc
import json
import resource
import sys

import anyio


async def work() -> None:
    await anyio.to_thread.run_sync(int)


def rss_kb() -> int:
    # ru_maxrss is KB on Linux
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def live_loops() -> int:
    import asyncio

    return sum(1 for o in gc.get_objects() if isinstance(o, asyncio.AbstractEventLoop))


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    warmup = min(200, rounds // 10)

    samples: list[tuple[int, int]] = []
    for i in range(rounds):
        anyio.run(work)
        if i >= warmup and i % 50 == 0:
            gc.collect()
            samples.append((i, rss_kb()))

    # Least-squares slope over the steady-state samples (KB per iter).
    n = len(samples)
    sx = sum(x for x, _ in samples)
    sy = sum(y for _, y in samples)
    sxx = sum(x * x for x, _ in samples)
    sxy = sum(x * y for x, y in samples)
    denom = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / denom if denom else 0.0

    gc.collect()
    print(
        json.dumps(
            {
                "rounds": rounds,
                "slope_kb_per_iter": round(slope, 3),
                "rss_start_kb": samples[0][1],
                "rss_end_kb": samples[-1][1],
                "rss_growth_kb": samples[-1][1] - samples[0][1],
                "live_loops": live_loops(),
            }
        )
    )


if __name__ == "__main__":
    main()
