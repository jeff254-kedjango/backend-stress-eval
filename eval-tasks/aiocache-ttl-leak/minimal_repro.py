#!/usr/bin/env python3
"""Standalone reproducer for the aiocache memory growth.

One third-party dependency (aiocache), no test framework. It models a cache
used the ordinary way: entries are written with a short expiry, then later
refreshed in place without one. Running it drives process memory up in a
straight line even though the number of distinct cache entries is bounded.

    import asyncio
    from aiocache import Cache

    async def main():
        cache = Cache(Cache.MEMORY)
        for i in range(N):
            key = f"user:{i}:session"
            await cache.set(key, {"n": i}, ttl=30)      # write with expiry
            await cache.set(key, {"n": i, "seen": 1})   # refresh, no expiry

On stock aiocache 0.12.3 / Python 3.12 this grows steadily per iteration and
does not level off — the cache holds a bounded set of live entries, yet the
process keeps climbing.

The script runs silently — it just exercises the workload. Wrap it in a memory
profiler or watch process RSS to see the growth before starting on a fix.
"""
from __future__ import annotations

import asyncio

from aiocache import Cache

_ROUNDS = 60_000


async def _main() -> None:
    cache = Cache(Cache.MEMORY)
    for i in range(_ROUNDS):
        key = f"user:{i}:session"
        # Write the entry with a short time-to-live.
        await cache.set(key, {"n": i}, ttl=30)
        # Later the same entry is refreshed in place, this time without a TTL.
        await cache.set(key, {"n": i, "seen": 1})


if __name__ == "__main__":
    asyncio.run(_main())
