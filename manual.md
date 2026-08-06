# backend-stress-eval — Operator's Manual

> Companion docs: [`discovery-strategy.md`](./discovery-strategy.md) (the "why"),
> [`rules.md`](./rules.md) (the thirteen standing working rules — Rules 11-13
> are the sourcing gates added 2026-08-06),
> [`upgrade-plan.md`](./upgrade-plan.md) (the reviewer-bar roadmap;
> authoritative for the next round of work — supersedes the 2026-08-05
> C+ → A version).
> This file is the **how**: how to run it, how to hunt bugs with it, how to
> extend it.

---

## 0. Elevator pitch

`backend-stress-eval` is a **lifecycle + determinism** harness. Point it at a
web-framework version (currently FastAPI; other adapters are a `plugins/<name>/`
implementation away) and it will:

1. Fire real traffic through the framework's real ASGI/lifespan stack.
2. Sample kernel-truthful process metrics between requests (RSS, open file
   descriptors, threads, GC objects) from `/proc/self/*`.
3. Assert **invariants** — properties that should hold on a well-behaved
   backend: memory returns to baseline, FDs return to baseline, routes don't
   drift, identical requests return identical bytes.
4. Emit a **byte-stable JSON report** — same input → same output bytes → usable
   as a machine-checkable grading artifact for frontier-model debugging evals.

It is **not** a load tester, a fuzzer, or a benchmarking tool. It is a
correctness harness that puts lifecycle and determinism under repeated stress
and flags any drift with reproducible evidence.

---

## 1. Prerequisites

- Linux (metrics read `/proc/self/*`; suite auto-skips on macOS/Windows)
- Python 3.12 (managed via [`uv`](https://docs.astral.sh/uv/) — no system Python
  contamination)
- Repo checked out at `/home/jeff/backend-stress-eval/`

### One-time setup

```bash
cd /home/jeff/backend-stress-eval
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev,fastapi]"
```

`dev` pulls the tooling gate (ruff, mypy, pytest, pip-audit). `fastapi` pulls
the runtime extras (`fastapi`, `starlette`, `httpx2`) needed by the FastAPI
plugin. Neither is required by `core/` — pure-core work needs only the base
install.

### Verify the install

```bash
./check.sh
```

Expected tail: `144 passed, 1 skipped` (the skip is a non-Linux gate) and
`==> all gates green`. If that isn't what you see, **stop** and fix the gate
before doing anything else. Rule 6.

---

## 2. Repository layout

```
backend-stress-eval/
├── check.sh                        # Tooling gate: ruff + mypy --strict + pytest + pip-audit
├── pyproject.toml                  # Pinned deps, per-file ignores, [dev] and [fastapi] extras
├── discovery-strategy.md           # Why + the six locked decisions
├── rules.md                        # Nine canonical working rules
├── manual.md                       # This file
│
├── core/                           # Framework-agnostic. Zero third-party runtime deps.
│   ├── invariant.py                # Invariant Protocol + Ok/Violation + registry
│   ├── metrics.py                  # /proc sampler + RSS/FD baseline invariants
│   ├── runner.py                   # Runner + cadences
│   ├── sequence.py                 # Ordered Step composition
│   ├── reporter.py                 # Byte-stable JSON grading contract
│   ├── plugin.py                   # Plugin Protocol (App, Client_co)
│   └── framework_invariants.py     # RouteRegistryStable + ResponseDeterminism
│
├── plugins/                        # Per-ecosystem adapters. FastAPI first.
│   ├── registry.py                 # Auto-discovers plugins/<name>/manifest.py — used by CLI
│   ├── stub/                       # Fake framework with a planted leak (used in tests)
│   │   ├── __init__.py             # StubPlugin implementation
│   │   └── manifest.py             # Registry entry
│   └── fastapi/                    # Real FastAPI adapter — TestClient sync facade over ASGI lifespan
│       ├── __init__.py             # FastAPIPlugin + canonical_example_app + minimal_example_app
│       └── manifest.py             # Registry entry
│
├── cli/                            # `bse` CLI (added 2026-08-01)
│   └── main.py                     # list / run / install subcommands
│
├── harnesses/                      # Composition — five layers per discovery-strategy.md §9
│   ├── __init__.py                 # HarnessState + adapter invariants
│   ├── layer1_repetition.py        # Repetition (memory/FD leak over N requests)
│   ├── layer2_lifecycle.py         # Lifecycle (leak/drift across start-stop)
│   ├── layer3_variants.py          # Feature-combination (Layer 2 across variants)
│   ├── layer4_sequence.py          # Ordered scenarios (LayerStep)
│   ├── discovery.py                # run_discovery(*, plugin, ...) — the generic full sweep
│   └── eval_task.py                # package_eval_task(...) — writes report.json/summary.txt/reproduce.py
│
├── tests/                          # 144 pytest cases; ./check.sh runs them
└── reports/                        # Output dir (JSON is gitignored; .gitkeep kept)
```

**Layer boundary rule of thumb:** anything under `core/` must remain
framework-agnostic. Anything framework-specific belongs under
`plugins/<name>/`. Composition of both lives in `harnesses/`.

---

## 3. Fast paths — three ways to use it from the terminal

Every path assumes you have activated the venv:

```bash
cd /home/jeff/backend-stress-eval
source .venv/bin/activate
```

### 3.1 The `bse` CLI — one command per framework (the fast path)

After `uv pip install -e ".[dev,fastapi]"` the `bse` executable is on your
venv `PATH`. Three subcommands.

**See what's available:**

```bash
bse list
```
```
fastapi   FastAPI + Starlette + httpx2 — HTTP request/response, ASGI lifespan.
            pip: fastapi   variants: 2
stub      In-memory fake framework — proves core drives a plugin end-to-end.
            pip: (no runtime deps)   variants: 0
```

**Run the full sweep against currently-installed FastAPI:**

```bash
bse run fastapi
```

**Pin to a specific release** (uv-installs, then runs):

```bash
bse run fastapi --version 0.141.1
```

**Custom counts, custom output directory:**

```bash
bse run fastapi \
    --version 0.141.1 \
    --iterations 5000 \
    --rounds-l2 500 \
    --rounds-l3 100 \
    --out reports/discovery/fastapi-hunt-0.141.1
```

**Use whatever version is already installed, no pip touch:**

```bash
bse run fastapi --no-install
```

Each run prints one PASS/FAIL line per layer and writes
`report.json` + `summary.txt` + `reproduce.py` to the output directory
(§7). Under a minute at defaults.

### 3.2 Add a new framework — one file (via `bse install` scaffold)

```bash
bse install celery                       # scaffolds plugins/celery/
$EDITOR plugins/celery/__init__.py       # fill in the ten Plugin methods
$EDITOR plugins/celery/manifest.py       # fill in pip_packages, description
uv pip install -e ".[dev,celery]"        # after you add the [celery] extra
bse list                                 # confirms the new plugin appears
bse run celery --version 5.4.0           # sweeps it
```

Full worked recipe in §8.

### 3.3 Python API — for when you want more control

The CLI is a thin wrapper over `run_discovery(*, plugin, ...)`. For custom
probes, custom invariants, or fine-grained layer control, drop to Python:

```bash
python <<'PY'
from pathlib import Path
from harnesses.discovery import run_discovery
from harnesses.eval_task import package_eval_task
from plugins.fastapi import FastAPIPlugin, canonical_example_app, minimal_example_app

plugin = FastAPIPlugin(app_factory=canonical_example_app)
reports = run_discovery(
    plugin=plugin,
    target_commit="fastapi-0.141.1",
    iterations_l1=5000,
    rounds_l2=500,
    variants=(
        ("minimal", minimal_example_app),
        ("canonical", canonical_example_app),
    ),
    variant_plugin_factory=lambda af: FastAPIPlugin(app_factory=af),
)
package_eval_task(reports=reports, out_dir=Path("reports/discovery/fastapi-hunt"))
PY
```

### 3.4 The pytest gate

```bash
./check.sh          # Full gate — ruff + mypy --strict + pytest + pip-audit
pytest -q           # Tests only
pytest tests/test_harness_layers.py -v   # One file
pytest -k lifecycle -v                   # Filter by name
```

`./check.sh` is the source of truth. Do not commit if it isn't green.

---

## 4. Capabilities in plain language

| Layer | What it hammers | The property it asserts |
|---|---|---|
| **Layer 1 — Repetition** | Fires the same request many times through one process. | RSS and open file descriptors return to baseline — no leak per request. |
| **Layer 2 — Lifecycle** | Starts and stops the app repeatedly. | Route table doesn't drift; no cumulative leak across restarts. |
| **Layer 3 — Variants** | Runs Layer 2 across several app shapes (e.g. minimal vs feature-rich). | Same properties hold regardless of enabled features. |
| **Layer 4 — Sequence** | Fires an ordered scripted mix of operations. | Responses stay byte-identical for equivalent inputs (response determinism), plus memory/FD checks. |
| **Layer 5 — Invariant checking** | Not a separate layer at runtime — it is the checking mechanism the other four use. | Every invariant is a value type; the runner evaluates each at a chosen cadence and records deterministic `Violation` records. |

Under the surface, five things make the harness useful:

1. **Measurement first.** RSS, FD count, threads, GC objects — sampled cheaply
   via `/proc/self/status` and `/proc/self/fd`. No `psutil`, no third-party
   dep.
2. **Deterministic detection.** Same target + same seed → **byte-identical**
   `report.json`. The four planted-bug fixtures in the test suite each fire
   10/10 replays.
3. **Machine-checkable grading contract.** `to_json(report)` writes UTF-8
   bytes with sorted keys, compact separators, no timestamps in the graded
   blob. A grader compares bytes.
4. **Framework-agnostic core.** `core/` has zero third-party runtime deps. Any
   framework becomes reachable by writing a `plugins/<name>/` that implements
   six methods (§8).
5. **Fail-loud construction.** Blank step names, empty variants, unknown
   invariants, empty reports — every hazard raises a typed exception at
   construction, not deep inside a run.

**What the harness does NOT do:**

- No p99 latency, no throughput measurement — this is not a load tester.
- No input generation / fuzzing — you write the request callables.
- No network fault injection.
- No coverage measurement.

---

## 5. The inputs you supply

`run_discovery` is the entire control surface. All arguments are keyword-only.

| Parameter | Default | What it means |
|---|---|---|
| `target_commit` | **required** | Provenance label baked into every layer's `metadata.target_commit`. E.g. `"fastapi-0.141.1"` or a git SHA. **The harness does not check anything out** — this is a string you set to describe what version of the target is installed in your venv. |
| `iterations_l1` | `500` | Layer 1: how many probe requests per process. |
| `rounds_l2` | `50` | Layer 2: how many start→request→stop cycles. |
| `rounds_l3` | `20` | Layer 3: how many rounds *per variant*. |
| `harness_version` | `"0.0.1"` | Recorded in the report. Bump when *the harness itself* changes. |

Layer 4 step count is defined in code at
`harnesses/discovery.py:186-202` (three `LayerStep`s). Edit the tuple to
change it.

### Suggested scales

| Purpose | `iterations_l1` / `rounds_l2` / `rounds_l3` | Wall time (laptop) |
|---|---|---|
| Smoke sweep (default) | 500 / 50 / 20 | ~1 minute |
| CI-fast | 20 / 5 / 3 | ~5 seconds |
| Trust-building sweep | 5000 / 500 / 100 | 5–10 minutes |
| Overnight | 50 000 / 5 000 / 500 | Hours |

Slow leaks that don't surface at 500 often surface at 5000. Rule 7 —
perfection over speed — reach for the higher counts once the defaults have
run clean.

### Other places you plug in inputs

- **Which app to hammer:** replace `canonical_example_app` in
  `harnesses/discovery.py:150,159,169,204` with your own zero-argument
  factory that returns a `FastAPI` app. To keep the change surgical, add
  your factory beside the existing two and switch the references.
- **A different framework entirely:** implement `core/plugin.py:Plugin`
  under `plugins/<yourframework>/` (§8), then call the layer harnesses
  directly rather than going through `run_discovery`.
- **A different probe:** `_one_probe_request` at
  `harnesses/discovery.py:104` does `client.get("/")` and asserts HTTP 200.
  Copy and replace to hit any endpoint / any method / any body.

---

## 6. Bug-hunting workflows — full recipes

The harness is only as useful as the bug you point it at. Rule 9: reproduce
first, capture the real failure, then form one theory.

### 6.1 Hunt a suspected leak in a specific FastAPI release

Steps you take at the terminal:

```bash
# 1. Pin the suspect release inside the venv.
source .venv/bin/activate
uv pip install --force-reinstall "fastapi==0.140.0"          # e.g. an older release

# 2. Confirm nothing else drifted.
./check.sh                                                   # all-green baseline

# 3. Run discovery with a matching target_commit label.
python - <<'PY'
from pathlib import Path
from harnesses.discovery import run_discovery
from harnesses.eval_task import package_eval_task

reports = run_discovery(
    target_commit="fastapi-0.140.0",
    iterations_l1=5000,
    rounds_l2=500,
    rounds_l3=100,
)
out = package_eval_task(reports=reports, out_dir=Path("reports/discovery/fastapi-0.140.0"))
print("wrote", out)
PY

# 4. Read the summary — it fits on one screen per layer.
cat reports/discovery/fastapi-0.140.0/summary.txt

# 5. If anything failed, inspect the exact violation.
python -c "
import json
d = json.loads(open('reports/discovery/fastapi-0.140.0/report.json').read())
for layer, blob in d['layers'].items():
    for v in blob['result']['violations']:
        print(layer, v)
"

# 6. Replay to confirm determinism (Rule 9 — one theory only when repro is stable).
python reports/discovery/fastapi-0.140.0/reproduce.py
diff reports/discovery/fastapi-0.140.0/report.json reports/discovery/fastapi-0.140.0/replay/report.json
# Empty output = byte-identical replay = real, deterministic finding.
```

### 6.2 Add a targeted probe to a suspected endpoint

Suppose you suspect the leak is on `/upload`, not `/`. Copy
`_one_probe_request`, aim it at `/upload`, wire it through Layer 1:

```python
# hunt_upload.py — drop next to check.sh
from harnesses.layer1_repetition import run_layer1_repetition
from harnesses.discovery import canonical_example_app
from plugins.fastapi import FastAPIPlugin
from core.reporter import human_summary, to_json

def probe_upload(client):
    r = client.post("/upload", files={"f": ("x.txt", b"hello")})
    if r.status_code != 200:
        raise RuntimeError(f"unexpected {r.status_code}")

report = run_layer1_repetition(
    plugin=FastAPIPlugin(app_factory=canonical_example_app),
    request_callable=probe_upload,
    iterations=5000,
    target_commit="fastapi-0.140.0",
)
print(human_summary(report))
with open("reports/hunt_upload.json", "wb") as f:
    f.write(to_json(report))
```

Run it:

```bash
python hunt_upload.py
```

### 6.3 Add a new invariant

Say you want to assert that the number of live threads returns to baseline.
`Sample.thread_count` is already collected — just adapt to `HarnessState`:

```python
# core/thread_invariant.py (illustrative)
from dataclasses import dataclass
from core.invariant import CheckResult, Ok, Violation
from harnesses import HarnessState

@dataclass(frozen=True, slots=True)
class ThreadReturnToBaseline:
    slack: int = 0
    name: str = "thread_return_to_baseline"

    def setup(self, state: HarnessState) -> int:
        return state.sample.thread_count

    def check(self, state: HarnessState, baseline: int, iteration: int) -> CheckResult:
        drift = state.sample.thread_count - baseline
        if drift > self.slack:
            return Violation(
                invariant_name=self.name,
                detail=f"thread count drifted +{drift} above baseline",
                evidence={"baseline": baseline, "current": state.sample.thread_count, "drift": drift},
                iteration=iteration,
            )
        return Ok(invariant_name=self.name)
```

Then pass a custom registry into the layer harness:

```python
from core.invariant import InvariantRegistry
from harnesses import RssReturnToBaselineOnHarnessState, FdReturnToBaselineOnHarnessState
from core.thread_invariant import ThreadReturnToBaseline

reg = InvariantRegistry()
reg.register(RssReturnToBaselineOnHarnessState())
reg.register(FdReturnToBaselineOnHarnessState())
reg.register(ThreadReturnToBaseline())

report = run_layer1_repetition(..., registry=reg)
```

Add tests under `tests/` and re-run `./check.sh`. Rule 6.

### 6.4 Reproduce someone else's report

Any packaged report is self-replaying:

```bash
python reports/discovery/fastapi-0.141.1/reproduce.py
diff reports/discovery/fastapi-0.141.1/report.json \
     reports/discovery/fastapi-0.141.1/replay/report.json
# Byte-identical output means environment matches. Diff means something drifted.
```

### 6.5 Rule 9 checklist before you form a theory

1. Did `./check.sh` pass on a clean baseline commit **before** the change?
2. Did you capture the *exact* violation evidence (`invariant_name`,
   `iteration`, `evidence` dict) — not a paraphrase?
3. Did you replay at least once to confirm the violation is deterministic?
4. Did you resist forming more than one theory until the answers to 1–3 are
   yes?

If yes to all four, you have a real finding worth investigating.

---

## 7. Outputs — where results go

### On disk — the eval-task package

`package_eval_task(...)` writes three files to `out_dir`:

```
reports/discovery/<target-label>/
├── report.json      # Byte-stable JSON, layers sorted, schema v1. This IS the grading artifact.
├── summary.txt      # Human one-liner per layer (from core.reporter.human_summary).
└── reproduce.py     # Runnable stub. target_commit is inlined via repr().
```

The JSON shape (top level):

```json
{
  "discovery_schema_version": "1",
  "layers": {
    "layer1_repetition": { "metadata": {...}, "result": {...}, "schema_version": "1" },
    "layer2_lifecycle":  { ... },
    "layer3_variants":   { ... },
    "layer4_sequence":   { ... }
  }
}
```

Every `result` block has the same shape:

```json
{
  "invariants_evaluated": ["rss_return_to_baseline", "fd_return_to_baseline"],
  "iterations_completed": 500,
  "success": true,
  "violations": []
}
```

`violations` (when non-empty) each carry `invariant_name`, `detail`,
`evidence` (a sorted dict), and `iteration` (or `null` for end-only checks).

**Gitignore:** `reports/*.json` is gitignored on purpose — reports are
outputs, not source. To archive one, either move it out of the repo or add
it explicitly with `git add -f`. The `reports/.gitkeep` keeps the directory
tracked.

### In memory — the return value

`run_discovery(...)` returns `dict[str, Report]`. `Report` is a frozen
dataclass (`core/reporter.py`) with `metadata` and `result`. The dict
returned lives only for the Python process — if you don't call
`package_eval_task`, nothing is persisted.

### Nothing else is persisted

No database, no logs, no telemetry, no network I/O. Pure `/proc` reads +
`stdout` (unless you write to disk yourself).

---

## 8. Extending — a new plugin (framework adapter)

A plugin implements `core.plugin.Plugin[App, Client_co]`. **Ten methods**,
all sync. Structural typing — no inheritance required. After the R1 refactor
(2026-08-01) three helpers hoisted from the layer harnesses onto the plugin
surface, so adding a framework is **one file** (plus a manifest).

### The ten methods

| Method | Purpose |
|---|---|
| `name` (property) | Stable identifier — e.g. `"fastapi"`. |
| `build_app()` | Return a fresh app instance. Deterministic. |
| `client(app)` | Return a request-issuing client bound to the app. |
| `lifecycle_start(app)` | Enter the app's lifespan. Idempotent. |
| `lifecycle_stop(app)` | Exit the lifespan. Idempotent. |
| `reset(app)` | Restore request-scoped state. O(1). |
| `feature_matrix()` | `Mapping[str, bool]` of features, read-only. |
| `probe(client)` | Fire one canonical probe request. |
| `route_signature(app)` | Sorted `tuple[str, ...]` of registered operations. |
| `response_digest(app)` | Stable string hash of the probe response, or `None`. |

### The fast path — scaffold, edit, run

```bash
bse install celery                       # writes plugins/celery/__init__.py + manifest.py
$EDITOR plugins/celery/__init__.py       # replace NotImplementedError bodies
$EDITOR plugins/celery/manifest.py       # set pip_packages, description
```

The scaffold gives you a working skeleton with the correct class names,
the correct manifest shape, and every one of the ten methods stubbed out
with `NotImplementedError` so nothing silently no-ops.

Then wire the pip extras (Celery's example):

```toml
# pyproject.toml
[project.optional-dependencies]
celery = ["celery==5.4.0"]
```

Install and run:

```bash
uv pip install -e ".[dev,celery]"
bse list                                 # celery should now appear
bse run celery --version 5.4.0
```

### Rules to obey

- **Sync only.** Wrap async lifespans in a sync facade (see FastAPI plugin's
  `TestClient.__enter__` / `__exit__` idiom).
- **`build_app()` deterministic.** Same environment → structurally identical
  app.
- **`reset(app)` must not accumulate state.** Rule 1 — iteration cost stays
  constant.
- **`lifecycle_stop(app)` idempotent.** The harness may call it during error
  recovery.
- **`feature_matrix()` read-only.** Return `MappingProxyType({...})`.
- **`probe(client)` raises on unexpected result.** Not silent drift. The
  harness records the exception as a real failure.
- **`route_signature(app)` sorted + stable.** Same app → same tuple every
  call.
- **`response_digest(app)` describes steady state, not a counter.** If it
  drifts across probes on a clean app, that's a plugin bug — the
  `ResponseDeterminism` invariant will fire spuriously.

### Concept-mapping for non-web frameworks

Not every framework has HTTP-shaped things. That's fine — map to what your
framework actually has:

| Web concept | Celery equivalent | Django equivalent |
|---|---|---|
| `probe(client)` | `client.send("noop.task")` | `client.get("/")` (same as FastAPI) |
| `route_signature(app)` | Sorted task names — `tuple(sorted(app.tasks))` | Sorted URL patterns |
| `response_digest(app)` | SHA-256 of task return `repr()` | SHA-256 of response body |
| `lifecycle_start(app)` | `app.finalize()` + eager mode | `django.setup()` |
| `lifecycle_stop(app)` | `app.close()` | Close DB conns, clear settings |

### The manifest (~10 lines)

```python
# plugins/celery/manifest.py — filled in from the scaffold
from typing import Final
from plugins.celery import CeleryPlugin, canonical_celery_app
from plugins.registry import Manifest

MANIFEST: Final = Manifest(
    name="celery",
    description="Celery task queue — eager mode, sync facade over apply_async.",
    pip_packages=("celery",),
    plugin_factory=lambda app_factory: CeleryPlugin(app_factory=app_factory),
    default_app_factory=canonical_celery_app,
)
```

The registry auto-discovers this — no editing of any central list.

### Tests

Add `tests/test_plugin_celery.py`. Model after `tests/test_plugin_fastapi.py`
and `tests/test_plugin_new_methods.py`. Minimum coverage:

- Protocol conformance (`isinstance(plugin, Plugin)`).
- Basic lifecycle (build → start → probe → stop → idempotent double-stop).
- `reset(app)` does not clear routes/middleware.
- `feature_matrix()` read-only.
- `probe` raises on error.
- `route_signature` sorted and stable.
- `response_digest` stable across probes for a clean app.
- One planted-bug fixture — deterministic 10/10 replays.

Then `./check.sh`. Rule 6.

---

## 9. Cheat-sheet — commands you will actually run

```bash
# Setup (one-time)
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev,fastapi]"

# The gate — treat as green-or-stop
./check.sh

# Tests
pytest -q
pytest -k lifecycle -v
pytest tests/test_harness_layers.py::TestLayer1::test_planted_leak_caught -v

# Format & lint
ruff check .
ruff format .

# Type
mypy --strict .

# Security
pip-audit --skip-editable

# The CLI — the fast path
bse list                                      # what plugins are available
bse run fastapi                               # sweep with currently-installed version
bse run fastapi --version 0.141.1             # pin, install, sweep
bse run fastapi --no-install                  # skip the pip install step
bse run fastapi --iterations 5000 --rounds-l2 500 --rounds-l3 100
bse run fastapi --out reports/discovery/my-hunt
bse install celery                            # scaffold a new plugin

# Read the summary of the last sweep
cat reports/discovery/fastapi-0.141.1/summary.txt

# Extract violation details from the JSON
python -c "
import json
d = json.load(open('reports/discovery/fastapi-0.141.1/report.json'))
for name, blob in d['layers'].items():
    print(name, 'violations:', len(blob['result']['violations']))
    for v in blob['result']['violations']:
        print(' ', v['invariant_name'], v['iteration'], v['detail'])
"

# Replay a packaged report
python reports/discovery/fastapi-0.141.1/reproduce.py
diff reports/discovery/fastapi-0.141.1/{report.json,replay/report.json}
```

---

## 10. Troubleshooting

**`./check.sh` fails on a clean checkout.**
Read the exact error — do not theorise. Common cases: (a) Python not 3.12
(`python --version`), (b) venv not activated, (c) `[dev]` or `[fastapi]`
extras not installed, (d) a new upstream security advisory (`pip-audit`
tells you which package, which CVE — bump it and rerun). Rule 9.

**Discovery reports `PASS` but I know there's a bug.**
Three possibilities: the bug isn't a lifecycle/determinism issue (this
harness won't catch it — that's an honest coverage gap, not a false pass);
the counts are too low (§5 — bump to 5000/500/100); or the probe endpoint
doesn't exercise the bug (§6.2 — add a targeted probe).

**Report bytes changed between two runs with the same input.**
This is a real bug in *the harness*, not the target. The grading contract
promises byte-stability. Capture both `report.json` files, diff them, open
an issue. Likely culprits: a new field with non-deterministic serialisation
(timestamp, random id, unsorted mapping) — every one of those is a Chunk-5
regression.

**Non-Linux platform.**
Everything under `core/metrics.py` requires `/proc/self/*`. The pytest
suite skips those tests on non-Linux automatically. There is no macOS/
Windows plan — a metrics adapter for those platforms would be a new
`core/metrics_darwin.py` behind a runtime probe.

**mypy fails with variance errors after I add a TypeVar.**
Re-read `core/plugin.py:30-40`. Rule of thumb: if `T` appears in both
parameter *and* return position, it must be **invariant**; return-only →
covariant; parameter-only → contravariant. This exact lesson bit twice
during the build (Chunks 2 and 6) — the notes there are the record.

**`ruff` and `mypy` disagree on `type X = ...` syntax.**
mypy 1.11 rejects PEP 695 `type` statements; ruff UP040 prefers them. The
project uses `TypeAlias` in `core/invariant.py` with a per-file-ignore in
`pyproject.toml`. Do the same if the situation recurs.

**A Write to a source file "silently failed" — do I retry?**
Verify first (`grep` for a token you know should be in the new content).
Rule 9. Retrying blindly is guessing; measure before you re-issue the
write.

**How do I know the harness itself is correct?**
Four planted-bug fixtures in the test suite fire deterministically 10/10
replays each: memory leak (`tests/test_plugin_stub.py`), FD leak
(`tests/test_harness_layers.py`), route drift
(`tests/test_harness_layers_3_4.py`), response drift
(`tests/test_harness_layers_3_4.py`). If those ever stop firing, the
harness itself is broken — same signal, opposite direction.

---

## 11. Design invariants (things you shouldn't casually change)

- `core/` has zero third-party runtime deps. Adding one requires an entry in
  `discovery-strategy.md` Decisions and a paragraph in this manual.
- `to_json(report)` produces byte-stable output. No timestamps, no random
  IDs, no unsorted mappings in the graded blob.
- Plugin `Protocol` surface is ten methods, sync (six original + three R1-
  hoisted: `probe`, `route_signature`, `response_digest`; plus the `name`
  property). Extending it is a breaking change for every existing plugin.
- Layer 3 does NOT extend the Plugin ABC (locked Chunk 9). Variants are
  caller-supplied tuples.
- `stop_on_first_violation=True` in `Runner` short-circuits the **inner**
  loop too — a test that asserted `[0,1,2,3]` and got `[0,1,2]` had a bug
  in the test, not the runner (locked Chunk 4).

If you need to change any of the above, do it as its own commit with a note
in `discovery-strategy.md` explaining the trade-off. Rule 7.

---

## 12. Referenced files

| Concern | File | Line anchor |
|---|---|---|
| Invariant Protocol + `Ok`/`Violation` | `core/invariant.py` | top |
| `/proc` sampler + baseline invariants | `core/metrics.py` | top |
| `Runner`, cadences, `RunResult` | `core/runner.py` | top |
| `Sequence`, `Step` | `core/sequence.py` | top |
| Byte-stable JSON | `core/reporter.py` | `to_json` |
| Plugin Protocol | `core/plugin.py` | `class Plugin` |
| Route + response invariants | `core/framework_invariants.py` | top |
| FastAPI adapter | `plugins/fastapi/__init__.py` | `class FastAPIPlugin` |
| FastAPI manifest | `plugins/fastapi/manifest.py` | `MANIFEST` |
| Stub adapter | `plugins/stub/__init__.py` | `class StubPlugin` |
| Plugin registry | `plugins/registry.py` | `load_manifests` |
| CLI entry point | `cli/main.py` | `main` |
| Layer 1 harness | `harnesses/layer1_repetition.py` | `run_layer1_repetition` |
| Layer 2 harness | `harnesses/layer2_lifecycle.py` | `run_layer2_lifecycle` |
| Layer 3 harness | `harnesses/layer3_variants.py` | `run_layer3_variants` |
| Layer 4 harness | `harnesses/layer4_sequence.py` | `run_layer4_sequence`, `LayerStep` |
| Discovery entry point | `harnesses/discovery.py` | `run_discovery` |
| Packager | `harnesses/eval_task.py` | `package_eval_task` |

---

*Read [`rules.md`](./rules.md) before you write code here. Read
[`discovery-strategy.md`](./discovery-strategy.md) before you change the
shape of a layer. This file explains how to use what's already there.*
