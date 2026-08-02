#!/usr/bin/env python3
"""SQLAlchemy 2.0 staleness hunt on real Postgres (concurrent writer).

The file-SQLite hunt (investigations/sqlalchemy-staleness/findings.md) proved
SQLAlchemy reloads correctly after commit() on READ COMMITTED, and could only
manufacture the well-known within-transaction identity-map footgun — which a
frontier model names on sight (zero investigation depth). It flagged Postgres
+ real MVCC as the one untried substrate where a *novel* staleness interaction
could live.

Substrate (verified): postgresql+psycopg:///bse_hunt, QueuePool, two isolated
backend PIDs — a genuine external writer, impossible on SQLite in-memory.

Hypothesis under test: does SQLAlchemy return stale data in a way that is NOT
the on-sight footgun — specifically at REPEATABLE READ, where the snapshot is
held for the whole transaction, interacting with expire_on_commit and the
identity map across a session's transaction boundary?

Rule 9: every probe is teeth-verified — inject a KNOWN stale/fresh condition
and assert the probe classifies it correctly BEFORE trusting a real result.

Run:
  python probes.py           # real scenarios across isolation levels
  python probes.py --teeth   # assert probes detect known stale vs fresh
"""
from __future__ import annotations

import sys

from sqlalchemy import Integer, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

DSN = "postgresql+psycopg:///bse_hunt"


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widget"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    val: Mapped[str] = mapped_column(String(64))


def _fresh_engine(isolation: str):  # noqa: ANN202
    """Engine pinned to a given isolation level. NullPool so each connection is
    a genuinely fresh backend (no pool reuse masking snapshot behaviour)."""
    from sqlalchemy.pool import NullPool

    return create_engine(
        DSN, future=True, isolation_level=isolation, poolclass=NullPool
    )


def _reset(eng, seed: str = "orig") -> None:  # noqa: ANN001
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("insert into widget (id, val) values (1, :v)"), {"v": seed})


def _external_write(new_val: str) -> None:
    """A genuinely separate connection (own backend PID) commits a change."""
    ext = create_engine(DSN, future=True)
    with ext.begin() as c:
        c.execute(text("update widget set val=:v where id=1"), {"v": new_val})
    ext.dispose()


def scenario_reread_same_txn(
    isolation: str, *, do_external_write: bool = True
) -> str:
    """Read w in an OPEN transaction, external writer commits, re-get(1) in the
    SAME transaction. Returns the value the second read sees.

    TEETH lever ``do_external_write=False``: no writer, so the value MUST stay
    'orig' (proves the probe isn't just always reporting stale).
    """
    eng = _fresh_engine(isolation)
    _reset(eng)
    s = Session(eng)
    first = s.get(Widget, 1)
    _ = first.val  # materialise
    if do_external_write:
        _external_write("changed")
    # Force a DB round-trip: expire, then re-get in the still-open txn.
    s.expire(first)
    second_val = s.get(Widget, 1).val
    s.close()
    eng.dispose()
    return second_val


def scenario_reread_after_commit(
    isolation: str, *, do_external_write: bool = True
) -> str:
    """Read w, commit() (ends txn), external writer commits, get(1) again in a
    NEW txn. On READ COMMITTED this must be fresh; the question is REPEATABLE
    READ behaviour after an explicit commit boundary."""
    eng = _fresh_engine(isolation)
    _reset(eng)
    s = Session(eng)  # default expire_on_commit=True
    _ = s.get(Widget, 1).val
    s.commit()
    if do_external_write:
        _external_write("changed")
    second_val = s.get(Widget, 1).val
    s.close()
    eng.dispose()
    return second_val


def _run_real() -> int:
    print("=== Scenario A: re-read in SAME open transaction (after s.expire) ===")
    for iso in ("READ COMMITTED", "REPEATABLE READ"):
        v = scenario_reread_same_txn(iso)
        stale = v == "orig"
        print(f"  {iso:16s}: second read = {v!r:10s}  stale={stale}")
    print("=== Scenario B: re-read after commit() boundary (new txn) ===")
    for iso in ("READ COMMITTED", "REPEATABLE READ"):
        v = scenario_reread_after_commit(iso)
        stale = v == "orig"
        print(f"  {iso:16s}: second read = {v!r:10s}  stale={stale}")
    print()
    print("Interpretation:")
    print("  READ COMMITTED stale in A = the on-sight identity-map/snapshot footgun.")
    print("  A NOVEL bug would be: stale where the isolation level says it")
    print("  should be fresh, OR fresh where a naive model expects stale.")
    return 0


def _run_teeth() -> int:
    print("=== TEETH: assert probes classify known stale vs fresh ===")
    ok = True
    # No external write -> MUST read 'orig' (not a false stale).
    a_nofresh = scenario_reread_same_txn("READ COMMITTED", do_external_write=False)
    t1 = a_nofresh == "orig"
    print(f"  A/no-writer reads orig (expect orig): {a_nofresh!r}  PASS={t1}")
    ok &= t1
    # READ COMMITTED, after commit + external write -> MUST be fresh 'changed'.
    b_fresh = scenario_reread_after_commit("READ COMMITTED", do_external_write=True)
    t2 = b_fresh == "changed"
    print(f"  B/RC-after-commit reads changed (expect changed): {b_fresh!r}  PASS={t2}")
    ok &= t2
    print(f"TEETH: {'ALL PASS — probes trustworthy' if ok else 'FAILED — probe blind'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_run_teeth() if "--teeth" in sys.argv else _run_real())
