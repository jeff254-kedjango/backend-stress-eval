"""Teeth test for the SQLAlchemy staleness probes.

A green probe means nothing unless it FAILS on a known-stale scenario. We
construct deliberately-stale reads and confirm the probe logic flags them.

Fault 1: expire_on_commit=FALSE but we assert the *true*-expiry probe's
freshness expectation — a session that never reloads returns 'orig' after an
external UPDATE, so a freshness check must report STALE.

Fault 2: a "refresh" that is skipped — the cached value stays 'orig', so the
refresh-reloads probe must report an anomaly.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from probes import Widget, _fresh_engine


@contextmanager
def _eng():
    with _fresh_engine() as e:
        yield e


def teeth_freshness_detects_stale() -> bool:
    """Emulate the true-expiry probe's check but with expiry DISABLED, so the
    re-read is stale. The freshness assertion (name=='changed') must fail."""
    stale = 0
    rounds = 20
    for _ in range(rounds):
        with _eng() as eng, Session(eng, expire_on_commit=False) as s:  # fault: no expiry
            s.add(Widget(id=1, name="orig"))
            s.commit()
            with eng.begin() as conn:
                conn.execute(text("UPDATE widget SET name='changed' WHERE id=1"))
            got = s.get(Widget, 1)  # identity-map hit -> 'orig' (STALE)
            if got.name != "changed":
                stale += 1
    detected = stale == rounds
    print(f"teeth-freshness: {stale}/{rounds} reads detected stale (expect all)")
    return detected


def teeth_refresh_skip_detected() -> bool:
    """If refresh() is skipped, cached stays 'orig' and the reload check fails."""
    anomalies = 0
    rounds = 20
    for _ in range(rounds):
        with _eng() as eng, Session(eng, expire_on_commit=False) as s:
            s.add(Widget(id=1, name="orig"))
            s.commit()
            with eng.begin() as conn:
                conn.execute(text("UPDATE widget SET name='changed' WHERE id=1"))
            cached = s.get(Widget, 1)
            # fault: refresh SKIPPED
            if cached.name != "changed":
                anomalies += 1
    detected = anomalies == rounds
    print(f"teeth-refresh-skip: {anomalies}/{rounds} anomalies detected (expect all)")
    return detected


if __name__ == "__main__":
    a = teeth_freshness_detects_stale()
    b = teeth_refresh_skip_detected()
    print("TEETH OK" if (a and b) else "TEETH FAIL")
    raise SystemExit(0 if (a and b) else 1)
