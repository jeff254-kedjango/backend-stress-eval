# Backpocket — discovered-bug ledger

Durable registry of every bug/surface investigated for the eval. **Nothing gets
lost:** even rejected candidates stay here so we never re-hunt a dead surface,
and so we can fall back to our best verified find if nothing stronger appears.

## Bar (reviewer policy)
A shippable task needs ALL of: real >1k-star OSS repo · bug live at a pinned
commit · **not fixed upstream** (open issue, no merged PR) · backend domain ·
**hard enough that a frontier model only partially succeeds (~1-2 hr)** ·
≥3 independent grading criteria. The binding constraint in practice is
**difficulty** — most mature-library bugs are easy-to-fix-once-described.

## The FOUR qualities (a bug must clear ALL — rank within this whole context)
A candidate is only shippable if it passes every one. Don't advance a bug that
aces one axis but fails another; don't reject one for a single axis without
checking it against the rest.
1. **Reproducible** — >1k-star OSS, live at a pinned commit, deterministic repro.
2. **Novel** — not fixed upstream (open issue, no merged/approved PR).
3. **Difficult** — frontier model only partially succeeds (~1-2 hr of work).
4. **Divergent** — the two models produce *measurably different* outcomes
   (approach OR time-to-fix). This is the axis the 4 shipped tasks + #431 fail:
   strong models converge on the one correct fix.

## Ranked fallback order (use if hunting comes up null)
1. **dramatiq #431** — validated objective grader + known behavior. Clears
   reproducible/novel/difficult; divergence weak on approach (3 blind converged),
   unproven cross-model on time-to-fix.
2. **procrastinate #1495** — validated 3-gate grader + repro on real PG, active
   repo. Clears reproducible/novel/difficult; divergence weak on approach (2 blind
   converged on SQL-fn reclaim). May diverge cross-model on time-to-fix.
(both are approach-convergent — the open question for both is whether two DIFFERENT
frontier models diverge on time-to-fix. Neither shipped yet.)

## Harvest bias learning (2026-08-03)
Triaging round-1 SHIP-WORTHY for DIAGNOSIS AMBIGUITY, nearly all have a SINGLE
clear root cause (that's WHY they were reported crisply — the harvest agents
keyed on clear repro). Examples: litestar #4700 (per-process lock→shared-Redis),
#4894 (subscribe/unsubscribe race, +open PR #4895), procrastinate #1495 (SQL
on-conflict), dramatiq #431 (atomic promote). Diagnosis-ambiguous bugs have
CONFUSED/CONTESTED issue threads and were likely filtered OUT by round 1.
IMPLICATION: don't mine the existing list for divergence — hunt the SHAPE
directly (issues with maintainer/reporter disagreement on cause, "not sure why",
multiple competing theories, reopened bugs, "works on my machine").
Also: several round-1 pins are TOO NEW (issue filed vs old version, pin is a
later beta where it's fixed) — litestar #3772 filed vs 2.12.1, pin 3.0.0b0 =
FIXED. Always confirm the issue's target version vs the pin.

## Divergence-probe learning (2026-08-03)
Blind same-model probes so far ALWAYS converge on approach (#431 3/3, #1495 2/2).
Strong models beeline the cleanest fix even when the theoretical fix space is wide.
This means "wide fix space" is NOT a reliable predictor of divergence. Real
divergence likely only shows up (a) cross-model, or (b) on genuinely
UNDER-DETERMINED bugs where the symptom doesn't pin the root cause — where models
must first DISAGREE on diagnosis. Next hunts should target diagnosis-ambiguous
bugs (multiple plausible root causes for one symptom), not just multi-fix bugs.

## Status legend
- `FALLBACK` — verified compliant + built grader; our ranked backup
- `FLOOR` — verified compliant, but below difficulty bar
- `REJECT-DIFF` — real+novel but too easy (difficulty gate)
- `REJECT-NOVEL` — real but fixed upstream / has merged-or-approved PR
- `REJECT-DESIGN` — correct-by-design / on-sight footgun, not a defect
- `REJECT-CONVERGE` — real+novel+hard but models converge (divergence gate)
- `CANDIDATE` — found, not yet fully verified
- `SHIPPED` — built into an eval task

---

## Verified / deep-investigated

| repo | stars | commit / version | issue# | novelty | difficulty | reproducer | status |
|------|------:|------------------|--------|---------|-----------|-----------|--------|
| samuelcolvin/arq | 2998 | `5ee4b48c` (0.28.0) | #402 | OPEN, no merged fix (verified) | **4/10, ~15min** | /tmp/arq-hunt/repro_402_hardcrash.py | **FLOOR** |
| taskiq-python/taskiq | 2270 | `ae2b7880` | #655 | open, no PR | ~5-line fix (on-sight) | /tmp/taskiq-hunt/repro_655.py | REJECT-DIFF |
| taskiq-python/taskiq | 2270 | `ae2b7880` | #626 | **approved open PR #627** | (deep) | — | REJECT-NOVEL |
| taskiq-python/taskiq | 2270 | `ae2b7880` | #586 | competing open PRs #639/#643 | (medium) | — | REJECT-NOVEL |
| taskiq-python/taskiq | 2270 | `ae2b7880` | #646/#556 | open, partial disclosure | ~1hr (flaky multi-proc) | — | CANDIDATE (weak) |
| encode/starlette | — | 1.3.1 | — | — | streaming-raise multi-cause; ~3 facet/~10min | /tmp/starlette-hunt/ | REJECT-DIFF |
| aio-libs/aiocache | 1435 | 0.12.3 | #563/#564/#366 | **fixed upstream (alpha)** | (multi-cause) | /tmp/aiocache-hunt/ | REJECT-NOVEL |

## Prior scoreboard (investigations/, teeth-verified negatives — do not re-hunt)

| surface | result | status |
|---------|--------|--------|
| FastAPI 0.141.1 interactions (L3 + widened) | dry / correct | REJECT-DESIGN |
| SQLAlchemy staleness — SQLite in-memory | wrong substrate (no real writer) | REJECT-DESIGN |
| SQLAlchemy staleness — file SQLite | correct-by-design | REJECT-DESIGN |
| SQLAlchemy staleness — Postgres MVCC | correct-by-design (isolation contract) | REJECT-DESIGN |
| Starlette 1.3.1 middleware+bg+contextvar | dry / correct | REJECT-DESIGN |
| ormar 0.26 relation-cache (`load()` vs `load_all()`) | documented contract | REJECT-DESIGN |
| anyio 4.14.2 lifecycle leak | REAL bug, but both models fully fix it | REJECT-DIFF (shipped as fix-quality task) |

## Shipped eval tasks (for reference — all double-PASSed A/B)
- piccolo-txn-data-loss, piccolo-txn-state-desync, aiocache-ttl-leak,
  anyio-lifecycle-leak-v2. See eval-tasks/. All solved ~10-13min → not
  differentiating on pass/fail.

## Environment notes (gates on what's huntable)
- Redis UP (127.0.0.1:6379) · Postgres UP (5432) · **Mongo NOT available**
  (blocks beanie 2691★, odmantic 1174★ until a Mongo service exists).

---

*Difficulty ratings come from a BLIND symptom-only probe (no web/issue/git
history). Screen difficulty EARLY — it's the gate that kills most candidates.*

---

## Pipeline round 1 harvest (2026-08-03) — 8 repos, 69 agents, wide-screen→triage→verify

22 SHIP-WORTHY (novel + reproduced + difficulty >=7), 2 FLOOR, ranked by difficulty.
Sub-agent verdicts; top-3 dramatiq novelty spot-checked by hand (all OPEN, 0 merged PRs).
Full per-candidate root-cause + repro paths + grading criteria in workflow journal:
`subagents/workflows/wf_6e91916d-ea6/journal.jsonl`.

| repo | issue# | commit | novelty | difficulty | repro | verdict |
|------|--------|--------|---------|-----------|-------|---------|
| Bogdanp/dramatiq | #845 | `288dc265` | OPEN,no fix | 9→7→**DEMOTED** | yes | REJECT-GRADE (deadlock not reliably reproducible on stock deps; stdlib+loguru both try/finally) |
| Bogdanp/dramatiq | #431 | `288dc265` | OPEN,no fix | 7/10 (blind-probed) | yes | **BANKED — RANKED #1 FALLBACK** grader VALIDATED (baseline FAIL/dup=2, fix PASS, structure-agnostic). 3 blind attempts CONVERGED on approach (all atomic srem-guarded Lua promote, all PASS/PASS) → weak approach-divergence, but likely diverges cross-model on time-to-fix. Known behavior. |
| Bogdanp/dramatiq | #692 | `288dc265` | OPEN,no fix | 8→**6/10 (blind-probed)** | yes | REJECT-DIFF (fully fixed ~15min) |
| sqlalchemy/alembic | #326 | `7b2af57e` | OPEN,no fix | 8→**6/10 (blind-probed)** | yes | REJECT-DIFF (fully fixed, real PG) |
| litestar-org/litestar | #3772 | `0f7fce6b` | OPEN,no fix | 8/10 | **NOT at this pin** | **REJECT-REPRO** — issue filed vs 2.12.1 but pin is 3.0.0b0; SSE/Stream generator `finally` cleanup RUNS on disconnect at this SHA (verified: normal + blocked-await/blpop-style both clean up). Bug fixed by 2.12→3.0b streaming refactor. Would need 2.12.1 checkout (different codebase). DEPRIORITIZE. |
| aio-libs/aiokafka | #844 | `0ff6e712` | OPEN,no fix | 8/10 | yes | SHIP-WORTHY |
| procrastinate-org/procrastinate | #1495 | `d9cf91de` | OPEN,no fix | 7/10 | **REPRO'd on real PG** | **RANKED FALLBACK #2 — grader VALIDATED** (baseline FAIL, fixes PASS 3/3). Divergence probe = CONVERGENT (2 blind both beelined SQL-fn on-conflict-reclaim). Wide theoretical fix space did NOT yield approach divergence. May diverge cross-model on time-to-fix. Grader+repro in investigations/procrastinate-1495-periodic-loss/. |
| procrastinate-org/procrastinate | #1591 | `d9cf91de` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| procrastinate-org/procrastinate | #1543 | `d9cf91de` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| litestar-org/litestar | #4700 | `0f7fce6b` | OPEN,no fix | 7/10 | filed vs 2.21.1 | SHIP-WORTHY* but CONVERGENT (per-process rate-limit lock → shared-Redis is THE fix; single root cause, not diagnosis-ambiguous). Skip for divergence. |
| litestar-org/litestar | #4894 | `0f7fce6b` | filed vs main | 7/10 | likely at pin | **REJECT-NOVEL-ish** open PR #4895 exists (fix proposed). Single clear root cause (channels subscribe/unsubscribe race). Convergent. Skip. |
| procrastinate-org/procrastinate | #1599 | `d9cf91de` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| sqlalchemy/alembic | #899 | `7b2af57e` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| litestar-org/litestar | #4699 | `0f7fce6b` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| encode/databases | #538 | `ae3fb16f` | OPEN,no fix | 7/10 | needs MySQL/aiomysql | SHIP-WORTHY* (repo ARCHIVED read-only — dead project, weakens "active OSS" spirit; also needs MySQL. DEPRIORITIZE) |
| encode/databases | #570 | `ae3fb16f` | OPEN,no fix | 7/10 | yes (sqlite) | SHIP-WORTHY* (repo ARCHIVED read-only — DEPRIORITIZE; but wide fix space: contextvar/fixture-scope) |
| aio-libs/aiokafka | #1095 | `0ff6e712` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| strawberry-graphql/strawberry | #3290 | `d22d2a83` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| strawberry-graphql/strawberry | #3414 | `d22d2a83` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| encode/databases | #176 | `ae3fb16f` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| strawberry-graphql/strawberry | #4326 | `d22d2a83` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| aio-libs/aiokafka | #1098 | `0ff6e712` | OPEN,no fix | 7/10 | yes | SHIP-WORTHY |
| Bogdanp/dramatiq | #445 | `288dc265` | OPEN,no fix | 6/10 | yes | FLOOR |
| aio-libs/aiokafka | #1145 | `0ff6e712` | OPEN,no fix | 6/10 | yes | FLOOR |
| sqlalchemy/alembic | #713 | `7b2af57e` | OPEN,no fix | 2/10 | no | REJECT-DIFF |
| strawberry-graphql/strawberry | #3991 | `d22d2a83` | NOT novel | 3/10 | no | REJECT-NOVEL |
| encode/databases | #467 | `ae3fb16f` | OPEN,no fix | 3/10 | yes | REJECT-OTHER |
| aio-libs/aiokafka | #911 | `0ff6e712` | OPEN,no fix | 2/10 | no | REJECT-OTHER |
| procrastinate-org/procrastinate | #1518 | `d9cf91de` | OPEN,no fix | 1/10 | no | REJECT-OTHER |
