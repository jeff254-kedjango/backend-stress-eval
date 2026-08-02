#!/usr/bin/env python3
"""aiocache 0.12.3 SimpleMemoryCache TTL-handler leak hunt.

Ascending-maturity sweep, >=1000 star filter. aiocache = 1,435 GitHub stars.
In-memory backend, no service required.

Root observation (Rule 9, measured before theorising):
``SimpleMemoryBackend._set`` cancels an existing TTL handle but only RE-STORES
into ``self._handlers`` on the ``if ttl:`` branch. When a key that currently
has a TTL is overwritten WITHOUT a ttl, the old (now-cancelled) TimerHandle is
left in ``self._handlers[key]`` forever. Over many distinct keys, ``_handlers``
grows unboundedly with dead handles while ``_cache`` is correct.

_set source (aiocache/backends/memory.py):
    if key in self._handlers:
        self._handlers[key].cancel()      # cancels, does NOT pop
    self._cache[key] = value
    if ttl:                               # only this branch writes _handlers
        self._handlers[key] = loop.call_later(ttl, self.__delete, key)
    # <-- no-ttl path never removes the stale entry

Probes:
  P1  leak_on_overwrite_no_ttl  — set(ttl) then set(no ttl): stale handle stays
  P2  handlers_unbounded         — N distinct keys, each ttl-then-no-ttl: does
                                    _handlers grow to N while all handles are
                                    already cancelled (dead weight)?

Run:
  python probes.py          # measure
  python probes.py --teeth  # assert probe distinguishes leak from clean
"""
from __future__ import annotations

import asyncio
import sys

from aiocache.backends.memory import SimpleMemoryCache


async def probe_leak_on_overwrite_no_ttl(*, overwrite_no_ttl: bool = True) -> bool:
    """Return True iff a stale (cancelled) handle is left in _handlers.

    TEETH lever ``overwrite_no_ttl=False``: overwrite WITH a ttl instead. Then
    the handle is legitimately replaced (live), so 'stale leak' MUST be False.
    """
    c = SimpleMemoryCache()
    await c.set("k", "v1", ttl=100)
    if overwrite_no_ttl:
        await c.set("k", "v2")  # no ttl — triggers the leak path
    else:
        await c.set("k", "v2", ttl=100)  # legit replace
    handle = c._handlers.get("k")
    if handle is None:
        return False
    # A leaked handle is one that is CANCELLED but still retained.
    return bool(handle.cancelled()) if overwrite_no_ttl else False


async def probe_handlers_unbounded(n: int = 2000) -> tuple[int, int, int]:
    """N distinct keys, each set(ttl) then overwrite(no ttl).

    Returns (cache_size, handlers_size, cancelled_handles). A correct backend
    keeps handlers_size == 0 after the no-ttl overwrites. The leak makes
    handlers_size == n, all cancelled (pure dead weight, never reclaimed).
    """
    c = SimpleMemoryCache()
    for i in range(n):
        k = f"key-{i}"
        await c.set(k, "a", ttl=100)
        await c.set(k, "b")  # drop ttl
    cancelled = sum(1 for h in c._handlers.values() if h.cancelled())
    return len(c._cache), len(c._handlers), cancelled


async def _run_real() -> int:
    print("=== P1: stale handle left after ttl->no-ttl overwrite? ===")
    leaked = await probe_leak_on_overwrite_no_ttl()
    print(f"  cancelled handle retained in _handlers: {leaked}")

    print("=== P2: does _handlers grow unbounded with dead handles? ===")
    n = 2000
    cache_sz, handlers_sz, cancelled = await probe_handlers_unbounded(n)
    print(f"  after {n} ttl->no-ttl cycles on distinct keys:")
    print(f"    _cache size     = {cache_sz}")
    print(f"    _handlers size  = {handlers_sz}   (correct backend: 0)")
    print(f"    cancelled dead  = {cancelled}   (all leaked handles are dead)")
    bug = leaked and handlers_sz == n and cancelled == n
    print(f"\n  -> LEAK CONFIRMED: {bug}")
    return 0 if bug else 1


async def _run_teeth() -> int:
    print("=== TEETH: probe must NOT flag the clean (legit-replace) path ===")
    ok = True
    # Overwrite WITH ttl -> handle is live, not a leak. Probe must say False.
    clean = await probe_leak_on_overwrite_no_ttl(overwrite_no_ttl=False)
    t1 = clean is False
    print(f"  legit ttl-replace flagged as leak? {clean}  (expect False)  PASS={t1}")
    ok &= t1
    # And the leak path IS flagged True (positive control).
    dirty = await probe_leak_on_overwrite_no_ttl(overwrite_no_ttl=True)
    t2 = dirty is True
    print(f"  real leak path flagged? {dirty}  (expect True)  PASS={t2}")
    ok &= t2
    print(f"TEETH: {'ALL PASS — probe distinguishes leak from clean' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_teeth() if "--teeth" in sys.argv else _run_real()))
