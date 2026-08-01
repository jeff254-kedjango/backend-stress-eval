"""Process metric samplers + the first concrete :class:`Invariant`.

Framework-agnostic. Zero third-party runtime deps — every measurement comes
from the standard library or ``/proc``. Every sampler is designed for a
per-iteration hot path (see Rule 1).

Metrics captured in a single :class:`Sample`:

* **rss_kb** — resident-set size in KB, parsed from ``/proc/self/status``
  (``VmRSS`` line). O(1) — one file read, one field lookup.
* **fd_count** — number of open file descriptors, counted from
  ``/proc/self/fd``. Strictly O(N_fds) but N_fds is bounded by the process
  ulimit (typically 1024) and does not grow with test iterations, so it is
  effectively O(1) for our purposes. Justified inline per Rule 1.
* **thread_count** — Python-visible thread count via
  :func:`threading.active_count`. O(1).
* **gc_objects** — sum of the three GC generation counters
  (:func:`gc.get_count`). O(1).
* **monotonic_ns** — :func:`time.monotonic_ns` snapshot; used for wall-time
  deltas without wall-clock skew.

Platform: Linux (including WSL2 — probed 2026-08-01). On a non-Linux host the
first :func:`sample` call raises :class:`MetricsUnsupportedError` rather than
silently returning garbage (Rule 5 — fail with an actionable message).
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from core.invariant import CheckResult, Ok, Violation

__all__ = [
    "FdReturnToBaseline",
    "MetricsUnsupportedError",
    "RssReturnToBaseline",
    "Sample",
    "delta",
    "sample",
]


# ---------------------------------------------------------------------------
# Errors + platform gate.
# ---------------------------------------------------------------------------
class MetricsUnsupportedError(RuntimeError):
    """Raised when the current platform cannot supply the metrics we need.

    We depend on ``/proc/self/status`` and ``/proc/self/fd``. That's Linux
    (including WSL2) only. macOS and Windows should raise this, not fake it.
    """


_STATUS_PATH: Final = Path("/proc/self/status")
_FD_PATH: Final = Path("/proc/self/fd")


def _require_linux_proc() -> None:
    """Fail-fast platform gate. Called once per :func:`sample`.

    Checked at call time (not import time) so importing ``core.metrics`` on a
    non-Linux dev machine doesn't blow up unrelated code. O(1).
    """
    if not sys.platform.startswith("linux"):
        raise MetricsUnsupportedError(
            f"core.metrics requires Linux /proc; running on {sys.platform!r}"
        )


# ---------------------------------------------------------------------------
# Sample shape. Frozen + slots — immutable and cheap to allocate.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Sample:
    """One point-in-time snapshot of the process. All fields non-negative."""

    monotonic_ns: int
    rss_kb: int
    fd_count: int
    thread_count: int
    gc_objects: int


@dataclass(frozen=True, slots=True)
class SampleDelta:
    """Difference between two :class:`Sample` instances. All fields signed."""

    elapsed_ns: int
    rss_kb: int
    fd_count: int
    thread_count: int
    gc_objects: int


# ---------------------------------------------------------------------------
# Parsers. Kept module-private; hot paths use them once per sample.
# ---------------------------------------------------------------------------
def _read_rss_kb() -> int:
    """Parse ``VmRSS:`` from ``/proc/self/status``. O(1)."""
    # ``/proc/self/status`` is a virtual file — ``read_text`` is a single
    # syscall; splitting by newlines and scanning for the prefix is bounded
    # by a fixed number of status lines (~55) that does not grow with load.
    for line in _STATUS_PATH.read_text().splitlines():
        if line.startswith("VmRSS:"):
            # Format: ``VmRSS:\t   12800 kB``
            _, value_kb, _unit = line.split()
            return int(value_kb)
    raise MetricsUnsupportedError("VmRSS field missing from /proc/self/status; refusing to sample")


def _count_open_fds() -> int:
    """Return the count of entries in ``/proc/self/fd``.

    Rule 1 note: strictly O(N_fds). N_fds is capped by ulimit (default 1024
    on Linux) and does not grow with stress iterations, so this is treated
    as effectively O(1). If a plugin ever bumps ulimit into the millions,
    swap this for parsing ``/proc/self/status``'s ``FDSize`` and add a
    separate O(1) invariant for leak detection.
    """
    # ``os.listdir`` is faster than ``Path.iterdir`` here — no per-entry
    # ``DirEntry`` allocation. We only need the count.
    return len(os.listdir(_FD_PATH))


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------
def sample() -> Sample:
    """Capture a single :class:`Sample`. O(1) per the notes above.

    Raises :class:`MetricsUnsupportedError` on non-Linux platforms.
    """
    _require_linux_proc()
    # Order is deliberate: cheapest fields first so the wall-time snapshot
    # captures as close to the process state as possible.
    thread_count = threading.active_count()
    # ``gc.get_count`` returns three ints (per generation); sum in O(1).
    gc_counts = gc.get_count()
    gc_objects = gc_counts[0] + gc_counts[1] + gc_counts[2]
    fd_count = _count_open_fds()
    rss_kb = _read_rss_kb()
    monotonic_ns = time.monotonic_ns()
    return Sample(
        monotonic_ns=monotonic_ns,
        rss_kb=rss_kb,
        fd_count=fd_count,
        thread_count=thread_count,
        gc_objects=gc_objects,
    )


def delta(before: Sample, after: Sample) -> SampleDelta:
    """Element-wise ``after - before``. O(1)."""
    return SampleDelta(
        elapsed_ns=after.monotonic_ns - before.monotonic_ns,
        rss_kb=after.rss_kb - before.rss_kb,
        fd_count=after.fd_count - before.fd_count,
        thread_count=after.thread_count - before.thread_count,
        gc_objects=after.gc_objects - before.gc_objects,
    )


# ---------------------------------------------------------------------------
# First concrete Invariant. Satisfies core.invariant.Invariant[Sample, int].
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RssReturnToBaseline:
    """Process RSS must not drift more than ``slack_kb`` above the baseline.

    Baseline is captured once (RSS at t=0). ``check`` reports a
    :class:`Violation` with structured evidence — grader-friendly.

    Rule 1: O(1) — dataclass field access + one int subtraction per check.
    """

    slack_kb: int = 1024
    name: str = "rss_return_to_baseline"

    def setup(self, state: Sample, /) -> int:
        """Return the initial RSS. Called once by the runner (Chunk 4)."""
        return state.rss_kb

    def check(self, state: Sample, baseline: int, iteration: int, /) -> CheckResult:
        drift = state.rss_kb - baseline
        if drift > self.slack_kb:
            return Violation(
                invariant_name=self.name,
                detail=f"RSS drifted +{drift} KB above baseline (slack {self.slack_kb} KB)",
                evidence={
                    "baseline_kb": baseline,
                    "current_kb": state.rss_kb,
                    "drift_kb": drift,
                    "slack_kb": self.slack_kb,
                },
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)


@dataclass(frozen=True, slots=True)
class FdReturnToBaseline:
    """Process FD count must not drift more than ``slack`` above the baseline.

    Sibling of :class:`RssReturnToBaseline`. Slack defaults to 0 — FDs are
    cheap to close and any drift is suspicious. Same evidence shape (``_kb``
    replaced with plain ``count`` / ``drift``).

    Rule 1: O(1) — one int comparison per check.
    """

    slack: int = 0
    name: str = "fd_return_to_baseline"

    def setup(self, state: Sample, /) -> int:
        return state.fd_count

    def check(self, state: Sample, baseline: int, iteration: int, /) -> CheckResult:
        drift = state.fd_count - baseline
        if drift > self.slack:
            return Violation(
                invariant_name=self.name,
                detail=f"FD count drifted +{drift} above baseline (slack {self.slack})",
                evidence={
                    "baseline_count": baseline,
                    "current_count": state.fd_count,
                    "drift": drift,
                    "slack": self.slack,
                },
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)
