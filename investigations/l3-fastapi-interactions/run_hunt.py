"""Drive Layer 3 over the interaction variants and report drift.

We ignore rss_slope_bounded (known anyio backend creep, already characterized
and shelved) and surface only route-registry drift and response
non-determinism — the two signals that mark a real feature-interaction bug.
"""
from __future__ import annotations

import sys

from harnesses.layer3_variants import run_layer3_variants
from plugins.fastapi import FastAPIPlugin

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from variants import VARIANTS  # noqa: E402

_IGNORE = {"rss_slope_bounded"}


def main() -> int:
    rep = run_layer3_variants(
        plugin_factory=lambda af: FastAPIPlugin(app_factory=af),
        variants=VARIANTS,
        request_callable=lambda c: c.get("/"),
        route_signature_of=FastAPIPlugin(app_factory=VARIANTS[0][1]).route_signature,
        rounds=50,
        target_commit="fastapi-0.141.1",
    )
    interesting = [
        v for v in rep.result.violations
        if v.invariant_name.split("::", 1)[-1] not in _IGNORE
    ]
    print(f"total violations: {len(rep.result.violations)}")
    print(f"interaction-relevant (non-rss): {len(interesting)}")
    for v in interesting:
        print(f"  - {v.invariant_name}: {v.detail}")
    return 0 if not interesting else 2


if __name__ == "__main__":
    raise SystemExit(main())
