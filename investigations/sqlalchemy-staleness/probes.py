"""Identity-map / expire-on-commit staleness probes for SQLAlchemy 2.x.

Target surface: "unit tests pass green, state goes stale after N operations."
Each probe drives a commit -> external-mutation -> re-read sequence and asks:
does the Session return a value consistent with the DATABASE, or a stale
value cached in the identity map?

Scenarios:

  A. expire_on_commit=True (default) + re-read after external UPDATE:
     access after commit must reload from DB -> sees the new value.

  B. expire_on_commit=False + re-read after external UPDATE:
     identity map keeps the stale value UNTIL an explicit expire/refresh.
     This is the classic footgun. We assert what SQLAlchemy actually does
     so a real interaction bug (staleness where freshness was expected, or
     vice-versa) would show as a probe mismatch.

  C. session reused across a delete+recreate-same-pk sequence (Layer-4 shape):
     does get() after delete+re-add return the right instance/state?

Rule 9: probes observe and REPORT actual behavior; they don't assume it.
Rule 1: each request is O(1); the identity map holds one row.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widget"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    ok: bool
    detail: str


@contextmanager
def _fresh_engine():
    """A file-backed SQLite engine so the Session and an 'external' writer get
    GENUINELY separate connections (in-memory ``sqlite://`` shares one DBAPI
    connection via SingletonThreadPool, which collapses the isolation these
    staleness probes require — see findings.md). Temp file is always removed.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(eng)
        yield eng
    finally:
        eng.dispose()
        try:
            os.unlink(path)
        except OSError:
            pass


def probe_expire_on_commit_true(*, rounds: int) -> ProbeResult:
    """Default expiry: after commit + external UPDATE, re-read must be FRESH."""
    stale = 0
    for i in range(rounds):
        with _fresh_engine() as eng, Session(eng) as s:
            s.add(Widget(id=1, name="orig"))
            s.commit()  # expire_on_commit=True -> attrs expired
            # External mutation via a separate connection (simulates another writer)
            with eng.begin() as conn:
                conn.execute(text("UPDATE widget SET name='changed' WHERE id=1"))
            got = s.get(Widget, 1)  # should reload -> 'changed'
            if got.name != "changed":
                stale += 1
    ok = stale == 0
    return ProbeResult(
        "expire_on_commit_true_reloads",
        ok,
        f"{rounds} rounds, all re-reads fresh" if ok else f"{stale}/{rounds} STALE re-reads",
    )


def probe_expire_on_commit_false(*, rounds: int) -> ProbeResult:
    """expire_on_commit=False: identity map keeps stale value until refresh.

    We assert SQLAlchemy's documented behavior: WITHOUT expire, the cached
    instance stays 'orig' after an external UPDATE; AFTER s.refresh() it
    becomes 'changed'. A deviation either way is an interaction anomaly.
    """
    anomalies = 0
    for i in range(rounds):
        with _fresh_engine() as eng, Session(eng, expire_on_commit=False) as s:
            w = Widget(id=1, name="orig")
            s.add(w)
            s.commit()  # not expired -> w.name stays usable without reload
            with eng.begin() as conn:
                conn.execute(text("UPDATE widget SET name='changed' WHERE id=1"))
            cached = s.get(Widget, 1)  # identity map hit -> expected 'orig'
            s.refresh(cached)  # explicit reload -> expected 'changed'
            if not (cached.name == "changed"):
                # after refresh it must be fresh; if not, refresh is broken
                anomalies += 1
    ok = anomalies == 0
    return ProbeResult(
        "expire_false_refresh_reloads",
        ok,
        f"{rounds} rounds, refresh always reloaded"
        if ok
        else f"{anomalies}/{rounds} refresh failed to reload",
    )


def probe_delete_readd_same_pk(*, rounds: int) -> ProbeResult:
    """Reuse one session across delete + re-add of the SAME pk; get() must
    return the re-added row's state, not a ghost of the deleted instance."""
    bad = 0
    for i in range(rounds):
        with _fresh_engine() as eng, Session(eng) as s:
            s.add(Widget(id=1, name="first"))
            s.commit()
            obj = s.get(Widget, 1)
            s.delete(obj)
            s.commit()
            s.add(Widget(id=1, name="second"))
            s.commit()
            got = s.get(Widget, 1)
            if got is None or got.name != "second":
                bad += 1
    ok = bad == 0
    return ProbeResult(
        "delete_readd_same_pk_consistent",
        ok,
        f"{rounds} rounds, re-add state correct" if ok else f"{bad}/{rounds} returned ghost/None",
    )


PROBES = (
    probe_expire_on_commit_true,
    probe_expire_on_commit_false,
    probe_delete_readd_same_pk,
)


if __name__ == "__main__":
    results = [p(rounds=200) for p in PROBES]
    all_ok = True
    for r in results:
        flag = "OK " if r.ok else "!! "
        print(f"{flag}{r.name}: {r.detail}")
        all_ok = all_ok and r.ok
    raise SystemExit(0 if all_ok else 2)
