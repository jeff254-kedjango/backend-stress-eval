"""Divergence probe (Gate 4 — final gate before packaging).

Framework-agnostic. See :file:`upgrade-plan.md` §6 and :file:`rules.md`
for the standing rules that back this module.

The failure mode this closes: crisply-reported bugs converge (every strong
model produces the same root-cause hypothesis once they diagnose
correctly). We package them, invest in graders, and only discover
cross-model convergence after the fact. dramatiq #431 (3/3 converged)
and procrastinate #1495 (2/2 converged) both died at this stage,
post-packaging. Chunk D moves the check to BEFORE packaging.

The probe:

1. Reads the candidate's affidavit to get ``observed_behaviour`` and
   ``initial_prompt`` (from ``initial-prompt.md``).
2. Spawns N=3 headless ``claude -p`` sessions in sealed tmpdirs. Each
   session receives ONLY the evidence — no repo, no fix hints, no
   thread text. The prompt asks for a two-sentence root-cause
   diagnosis written to ``diagnosis.json``.
3. Reads each session's ``diagnosis.json`` and normalises the
   ``root_cause`` field.
4. Clusters diagnoses by shared normalised-word overlap (≥ K words
   in common → same cluster). ≥ 2 clusters = DIVERGENT = PROCEED.
   1 cluster = CONVERGENT = REJECT.

Design notes:

* Driver shape mirrors ``core/difficulty.py``: same subprocess+timeout
  discipline, same tmpdir isolation, same fake-script testability.
  Deliberately duplicative — Chunk D is single-chunk-shippable that
  way, and factoring a shared driver base is a follow-up chunk if
  either driver grows further.
* Rule 1 (complexity): clustering is O(N²·W) where N is diagnoses
  (=3 in production) and W is the word count per diagnosis. At those
  sizes the O(N²) is trivial.
* Rule 2: subprocess argvs fully constructed; the affidavit-derived
  content flows through the prompt string only, not through argv.
* Rule 5: every failure path is a structured record with an
  actionable ``detail``. Sessions that fail to produce
  ``diagnosis.json`` count as "no signal", not as an implicit
  cluster.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Final

from core.affidavit import load_affidavit

__all__ = [
    "DIVERGENCE_MIN_CLUSTERS",
    "DIVERGENCE_N_ATTEMPTS",
    "DIVERGENCE_SCHEMA_VERSION",
    "DiagnosisRecord",
    "DivergenceError",
    "DivergenceReport",
    "TRIAGE_REPORT_FILENAME",
    "run_divergence_probe",
]

# ---------------------------------------------------------------------------
# Gate constants. See upgrade-plan.md §6.
# ---------------------------------------------------------------------------
DIVERGENCE_N_ATTEMPTS: Final = 3
DIVERGENCE_MIN_CLUSTERS: Final = 2
DIVERGENCE_CEILING_MINUTES: Final = 15.0  # Diagnosis is fast; cap short.
DIVERGENCE_SCHEMA_VERSION: Final = "1"

TRIAGE_REPORT_FILENAME: Final = "triage-report.json"
DIAGNOSIS_FILENAME: Final = "diagnosis.json"
_INITIAL_PROMPT_FILENAME: Final = "initial-prompt.md"

# Clustering threshold: two diagnoses cluster together iff their normalised
# root_cause word-sets share at least this many words. Tuned against typical
# root-cause phrases ("the deferred handler is not cancelled when the
# cache entry expires" ≈ 12 content words); 4 shared content words is a
# reasonable signal without over-clustering.
_CLUSTER_MIN_SHARED_WORDS: Final = 4

# Stopwords omitted from clustering so shared function-words don't cause
# spurious clusters. Kept short — the real signal is nouns and verbs
# specific to the root cause.
_STOPWORDS: Final = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "because",
        "been",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "not",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "then",
        "there",
        "this",
        "to",
        "was",
        "were",
        "when",
        "which",
        "with",
    }
)

_WORD_RE: Final = re.compile(r"[A-Za-z0-9_']+")

# The prompt handed to each headless session. Deliberately terse and
# adversarial-neutral. Do NOT include the pinned SHA, repo URL, or any
# link to the upstream thread — the point of the probe is to make each
# session diagnose from the symptom alone.
_DIVERGENCE_PROMPT_TEMPLATE: Final = """\
You are diagnosing a Python backend bug. Below is the symptom. Do NOT
write a fix, do NOT search the web, do NOT reason about implementation
details you cannot verify from the symptom alone.

Return a two-sentence root-cause hypothesis. Write it to a file named
`diagnosis.json` in the current directory with this exact shape:

{{
  "root_cause": "<one sentence naming the specific mechanism you think is failing>",
  "one_sentence": "<one sentence explaining why that mechanism produces the observed symptom>",
  "key_evidence": ["<phrase from the symptom that led you here>", ...]
}}

Symptom
=======

{symptom_body}
"""

_DEFAULT_CLAUDE_BIN: Final = "claude"


# ---------------------------------------------------------------------------
# Data records.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DiagnosisRecord:
    """One session's diagnosis. Ties a raw file back to normalised state."""

    index: int
    minutes: float
    root_cause: str
    one_sentence: str
    key_evidence: tuple[str, ...]
    session_returncode: int | None
    diagnosis_produced: bool
    working_dir: str


@dataclass(frozen=True, slots=True)
class DivergenceReport:
    """Overall gate verdict."""

    schema_version: str
    candidate_dir: str
    diagnoses: tuple[DiagnosisRecord, ...]
    clusters: tuple[tuple[int, ...], ...]  # each inner tuple = indices in one cluster
    n_clusters: int
    threshold: int
    passed: bool

    def to_text(self) -> str:
        verdict = "DIVERGENT (proceed)" if self.passed else "CONVERGENT (reject)"
        lines = [
            f"divergence probe: {verdict}",
            f"  candidate: {self.candidate_dir}",
            f"  N diagnoses: {len(self.diagnoses)}"
            f" (of which produced: {sum(1 for d in self.diagnoses if d.diagnosis_produced)})",
            f"  clusters: {self.n_clusters} (threshold for DIVERGENT: ≥ {self.threshold})",
        ]
        for i, cluster in enumerate(self.clusters, 1):
            members = ", ".join(f"#{idx}" for idx in cluster)
            first_idx = cluster[0]
            root = self.diagnoses[first_idx].root_cause[:120]
            lines.append(f"  cluster {i} [{members}]: {root!r}")
        for d in self.diagnoses:
            if not d.diagnosis_produced:
                lines.append(
                    f"  session {d.index}: NO DIAGNOSIS produced "
                    f"(returncode={d.session_returncode}) — {d.working_dir}"
                )
        return "\n".join(lines) + "\n"

    def to_json(self) -> str:
        """Byte-stable JSON serialisation for triage-report.json."""
        payload = {
            "schema_version": self.schema_version,
            "candidate_dir": self.candidate_dir,
            "diagnoses": [asdict(d) for d in self.diagnoses],
            "clusters": [list(c) for c in self.clusters],
            "n_clusters": self.n_clusters,
            "threshold": self.threshold,
            "passed": self.passed,
        }
        return json.dumps(payload, sort_keys=True, indent=2)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------
class DivergenceError(RuntimeError):
    """Structural precondition failed. Distinct from a gate reject."""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def run_divergence_probe(
    candidate_dir: Path,
    /,
    *,
    n_attempts: int = DIVERGENCE_N_ATTEMPTS,
    min_clusters: int = DIVERGENCE_MIN_CLUSTERS,
    ceiling_minutes: float = DIVERGENCE_CEILING_MINUTES,
    claude_bin: str = _DEFAULT_CLAUDE_BIN,
    write_report: bool = True,
) -> DivergenceReport:
    """Run N=3 diagnosis sessions on ``candidate_dir`` and return the verdict.

    Preconditions:

    * ``candidate_dir`` is a directory with a valid affidavit and an
      ``initial-prompt.md``.
    * ``claude_bin`` resolves.

    Writes ``triage-report.json`` unless ``write_report=False`` (tests).
    """
    _validate_candidate_dir(candidate_dir)
    resolved_bin = _resolve_binary(claude_bin)
    symptom_body = _assemble_symptom(candidate_dir)
    prompt = _DIVERGENCE_PROMPT_TEMPLATE.format(symptom_body=symptom_body)

    diagnoses: list[DiagnosisRecord] = []
    for i in range(n_attempts):
        record = _run_one_session(
            index=i,
            claude_bin=resolved_bin,
            prompt=prompt,
            ceiling_minutes=ceiling_minutes,
        )
        diagnoses.append(record)

    clusters = _cluster_diagnoses(diagnoses)
    n_clusters = len(clusters)
    report = DivergenceReport(
        schema_version=DIVERGENCE_SCHEMA_VERSION,
        candidate_dir=str(candidate_dir.resolve()),
        diagnoses=tuple(diagnoses),
        clusters=clusters,
        n_clusters=n_clusters,
        threshold=min_clusters,
        passed=n_clusters >= min_clusters,
    )

    if write_report:
        (candidate_dir / TRIAGE_REPORT_FILENAME).write_text(
            report.to_json() + "\n", encoding="utf-8"
        )
    return report


# ---------------------------------------------------------------------------
# Preconditions.
# ---------------------------------------------------------------------------
def _validate_candidate_dir(candidate_dir: Path) -> None:
    if not candidate_dir.is_dir():
        raise DivergenceError(f"{candidate_dir} is not a directory.")
    # Affidavit is required — we pull observed_behaviour from it.
    try:
        load_affidavit(candidate_dir)
    except Exception as exc:
        raise DivergenceError(
            f"cannot run divergence probe on {candidate_dir}: "
            f"affidavit prerequisite failed: {exc}"
        ) from exc
    if not (candidate_dir / _INITIAL_PROMPT_FILENAME).is_file():
        raise DivergenceError(
            f"missing {_INITIAL_PROMPT_FILENAME} under {candidate_dir}. "
            "The divergence probe uses the initial prompt as the symptom body."
        )


def _resolve_binary(name_or_path: str) -> str:
    candidate = Path(name_or_path)
    if candidate.is_absolute():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise DivergenceError(
            f"claude_bin {name_or_path!r} is absolute but not an executable file."
        )
    resolved = shutil.which(name_or_path)
    if resolved is None:
        raise DivergenceError(
            f"claude_bin {name_or_path!r} not found on PATH. "
            "Install claude-code CLI or pass claude_bin=<absolute-path>."
        )
    return resolved


def _assemble_symptom(candidate_dir: Path) -> str:
    """The evidence handed to each session.

    Combines the initial-prompt (the model-facing symptom) with the
    affidavit's ``observed_behaviour`` (the author's on-bench
    description). Deliberately does NOT include: the repo URL, the
    issue URL, the pinned SHA, or any thread text. Sessions must
    diagnose from symptom alone.
    """
    affidavit = load_affidavit(candidate_dir)
    prompt_text = (candidate_dir / _INITIAL_PROMPT_FILENAME).read_text(encoding="utf-8")
    return (
        "AUTHOR'S ON-BENCH OBSERVATION:\n"
        f"{affidavit.observed_behaviour}\n\n"
        "SYMPTOM AS THE MODEL WOULD SEE IT:\n"
        f"{prompt_text}"
    )


# ---------------------------------------------------------------------------
# One session.
# ---------------------------------------------------------------------------
def _run_one_session(
    *,
    index: int,
    claude_bin: str,
    prompt: str,
    ceiling_minutes: float,
) -> DiagnosisRecord:
    """Spawn one headless diagnosis session and read its diagnosis.json."""
    workdir = Path(mkdtemp(prefix=f"bse-divergence-{index}-"))
    argv = [
        claude_bin,
        "--print",
        "--allow-dangerously-skip-permissions",
        prompt,
    ]
    start = time.monotonic()
    proc: subprocess.CompletedProcess[str] | None = None
    try:
        proc = subprocess.run(  # noqa: S603 -- argv fully constructed
            argv,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=ceiling_minutes * 60.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        proc = None
    elapsed_minutes = (time.monotonic() - start) / 60.0

    diag_path = workdir / DIAGNOSIS_FILENAME
    produced = False
    root_cause = ""
    one_sentence = ""
    key_evidence: tuple[str, ...] = ()
    if diag_path.is_file():
        try:
            raw = json.loads(diag_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                rc = raw.get("root_cause")
                os_ = raw.get("one_sentence")
                ke = raw.get("key_evidence")
                if isinstance(rc, str) and isinstance(os_, str):
                    root_cause = rc
                    one_sentence = os_
                    produced = True
                if isinstance(ke, list):
                    key_evidence = tuple(str(x) for x in ke)
        except json.JSONDecodeError:
            produced = False

    if produced:
        shutil.rmtree(workdir, ignore_errors=True)
        workdir_str = f"<cleaned: was under {workdir.parent}>"
    else:
        workdir_str = str(workdir)

    return DiagnosisRecord(
        index=index,
        minutes=elapsed_minutes,
        root_cause=root_cause,
        one_sentence=one_sentence,
        key_evidence=key_evidence,
        session_returncode=proc.returncode if proc is not None else None,
        diagnosis_produced=produced,
        working_dir=workdir_str,
    )


# ---------------------------------------------------------------------------
# Clustering.
# ---------------------------------------------------------------------------
def _cluster_diagnoses(diagnoses: list[DiagnosisRecord]) -> tuple[tuple[int, ...], ...]:
    """Union-find style clustering by shared normalised-word overlap.

    Only diagnoses that actually produced output participate. Sessions
    that failed to produce diagnosis.json are excluded from the cluster
    count entirely — they are neither their own cluster nor forced into
    someone else's. The report surfaces them separately.
    """
    produced_indices = [i for i, d in enumerate(diagnoses) if d.diagnosis_produced]
    if not produced_indices:
        return ()

    word_sets: dict[int, frozenset[str]] = {
        i: _content_words(diagnoses[i].root_cause) for i in produced_indices
    }

    # Union-find. O(N²) with N ≤ 3 — trivially fine.
    parent: dict[int, int] = {i: i for i in produced_indices}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in produced_indices:
        for j in produced_indices:
            if j <= i:
                continue
            shared = word_sets[i] & word_sets[j]
            if len(shared) >= _CLUSTER_MIN_SHARED_WORDS:
                union(i, j)

    # Materialise clusters, sorted by first-member index for stable output.
    groups: dict[int, list[int]] = {}
    for i in produced_indices:
        root = find(i)
        groups.setdefault(root, []).append(i)
    ordered = sorted(groups.values(), key=lambda g: g[0])
    return tuple(tuple(g) for g in ordered)


def _content_words(text: str) -> frozenset[str]:
    """Lowercase, tokenise, drop stopwords. Used for clustering keys."""
    return frozenset(
        w for w in (m.group(0).lower() for m in _WORD_RE.finditer(text)) if w not in _STOPWORDS
    )
