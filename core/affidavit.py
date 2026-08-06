"""Repro-affidavit schema + validator (Gate 1 of the sourcing gates).

Framework-agnostic. See :file:`upgrade-plan.md` §4 Gate 1 and :file:`rules.md`
Rule 11 for the standing rules that back this module.

The affidavit is the machine-readable proof that the author personally
reproduced a candidate bug on-bench at a specific pinned commit. Nothing in
this project packages a candidate without a valid affidavit — the CLI verb
``bse affidavit <candidate-dir>`` calls :func:`validate_affidavit` and refuses
to advance the candidate on any validation failure.

Design notes:

* Pure stdlib. No network, no subprocess. The transcript check is a bounded
  read of the ``.cast`` file's header + body — asciinema v2 files are JSONL
  with a header dict on line 1 and ``[t, "o", data]`` event tuples after
  (see https://docs.asciinema.org/manual/asciicast/v2/). We parse a bounded
  prefix and stop; there is no unbounded input.
* Every helper is O(n) in the transcript size in the worst case (single scan
  for two substrings). No hidden quadratic behaviour (Rule 1).
* Failures produce actionable messages: what field, what value, why rejected
  (Rule 5). Nothing is silently accepted.
* :func:`validate_affidavit` is the single entry point; every other symbol is
  either a dataclass or a private helper.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

__all__ = [
    "AFFIDAVIT_FILENAME",
    "AFFIDAVIT_SCHEMA_VERSION",
    "Affidavit",
    "AffidavitError",
    "ValidationFailure",
    "load_affidavit",
    "validate_affidavit",
]

# ---------------------------------------------------------------------------
# Constants that the CLI and tests key off. Any change here is a breaking
# change to the on-disk contract — bump AFFIDAVIT_SCHEMA_VERSION when altering
# the required field set.
# ---------------------------------------------------------------------------
AFFIDAVIT_FILENAME: Final = "repro-affidavit.json"
# v2 (2026-08-06, chunk C) adds the required `upstream_issue_url` field so
# the writeup audit (Gate 3) can fetch the linked thread. v1 shipped and
# was superseded on the same branch, before any v1 affidavit landed on
# disk anywhere — no migration path exists or is needed.
AFFIDAVIT_SCHEMA_VERSION: Final = "2"

# Transcript size cap. Real asciinema recordings of a reproduction step run
# well under this; anything larger is either a mistake or malicious.
_TRANSCRIPT_MAX_BYTES: Final = 8 * 1024 * 1024  # 8 MiB

# Bench observed_behaviour length bounds. Reviewer wants "two-to-four
# sentences" — enforce a rough byte range that catches empty stubs and
# copy-pasted issue bodies without becoming a prose linter.
_OBSERVED_MIN_CHARS: Final = 80
_OBSERVED_MAX_CHARS: Final = 2000

_ASCIICAST_V2_VERSION: Final = 2
_UPSTREAM_STATUS_OPEN: Final = "open"
_UPSTREAM_STATUS_CLOSED_FIXED: Final = "closed-fixed"
_UPSTREAM_STATUS_MERGED_PREFIX: Final = "merged-pr-"

# A full git SHA is exactly 40 lowercase hex chars. We do NOT accept short
# SHAs — they collide across large repos and would let a stale/renamed
# reference sneak in.
_FULL_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")

# Git URL check is deliberately loose — https / ssh / git protocols all
# accepted — but we do reject anything with shell metacharacters, since the
# URL may later be handed to git clone.
_UNSAFE_URL_CHARS: Final = frozenset(";|&`$<>\n\r\t ")

_REQUIRED_FIELDS: Final = (
    "schema_version",
    "pinned_commit",
    "repo_url",
    "upstream_issue_url",
    "bench_transcript_path",
    "observed_behaviour",
    "divergence_from_thread",
    "upstream_status",
    "signed_by",
    "signed_at",
)


# ---------------------------------------------------------------------------
# Public data + error types.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Affidavit:
    """Validated affidavit contents.

    All fields correspond 1:1 to on-disk JSON keys. Constructed only by
    :func:`load_affidavit` and :func:`validate_affidavit` — do not build one
    by hand outside tests.
    """

    schema_version: str
    pinned_commit: str
    repo_url: str
    upstream_issue_url: str
    bench_transcript_path: str
    observed_behaviour: str
    divergence_from_thread: str
    upstream_status: str
    signed_by: str
    signed_at: str


class AffidavitError(ValueError):
    """Base for affidavit validation errors. Callers catch this."""


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    """Structured failure record.

    ``field`` is the JSON key that failed (or ``"<file>"`` when the failure is
    at the file level, e.g. missing / not JSON). ``detail`` is a one-line,
    actionable message ready to print to the operator.
    """

    field: str
    detail: str


# ---------------------------------------------------------------------------
# Loader — file-level errors surface as AffidavitError; field-level errors
# come back as a list of ValidationFailure so the CLI can print all of them.
# ---------------------------------------------------------------------------
def load_affidavit(candidate_dir: Path, /) -> Affidavit:
    """Parse and structurally validate the affidavit under ``candidate_dir``.

    Raises :class:`AffidavitError` if the file is missing, not valid JSON, or
    missing required fields with the wrong types. Semantic checks (transcript
    exists, SHA well-formed, upstream open) belong to :func:`validate_affidavit`.
    """
    path = candidate_dir / AFFIDAVIT_FILENAME
    if not path.is_file():
        raise AffidavitError(
            f"no {AFFIDAVIT_FILENAME} at {path}. "
            "Every candidate needs a signed affidavit before packaging "
            "(rules.md Rule 11)."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AffidavitError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AffidavitError(f"{path} must contain a JSON object, got {type(raw).__name__}")

    missing = [k for k in _REQUIRED_FIELDS if k not in raw]
    if missing:
        raise AffidavitError(
            f"{path} is missing required field(s): {', '.join(sorted(missing))}. "
            f"Required set: {', '.join(_REQUIRED_FIELDS)}."
        )
    for k in _REQUIRED_FIELDS:
        if not isinstance(raw[k], str):
            raise AffidavitError(
                f"{path} field {k!r} must be a JSON string, got {type(raw[k]).__name__}"
            )
    return Affidavit(
        schema_version=raw["schema_version"],
        pinned_commit=raw["pinned_commit"],
        repo_url=raw["repo_url"],
        upstream_issue_url=raw["upstream_issue_url"],
        bench_transcript_path=raw["bench_transcript_path"],
        observed_behaviour=raw["observed_behaviour"],
        divergence_from_thread=raw["divergence_from_thread"],
        upstream_status=raw["upstream_status"],
        signed_by=raw["signed_by"],
        signed_at=raw["signed_at"],
    )


# ---------------------------------------------------------------------------
# Semantic validation — the interesting checks.
# ---------------------------------------------------------------------------
def validate_affidavit(candidate_dir: Path, /) -> list[ValidationFailure]:
    """Return every semantic failure found. Empty list == passes Gate 1.

    Order of checks is stable so operator output is diffable across runs.
    Does not raise on failure — returns them, so the CLI can print all of
    them in one pass rather than fail-first-fix-first.

    File-level structural errors (missing file, bad JSON, missing fields,
    wrong types) still raise :class:`AffidavitError` via :func:`load_affidavit`
    — those are pre-conditions, not validation findings.
    """
    aff = load_affidavit(candidate_dir)
    failures: list[ValidationFailure] = []

    if aff.schema_version != AFFIDAVIT_SCHEMA_VERSION:
        failures.append(
            ValidationFailure(
                field="schema_version",
                detail=(
                    f"expected {AFFIDAVIT_SCHEMA_VERSION!r}, got {aff.schema_version!r}. "
                    "Bump-and-migrate is a deliberate action, not a default."
                ),
            )
        )

    if not _FULL_SHA_RE.fullmatch(aff.pinned_commit):
        failures.append(
            ValidationFailure(
                field="pinned_commit",
                detail=(
                    f"{aff.pinned_commit!r} is not a full 40-char lowercase hex SHA. "
                    "Tag aliases, short SHAs, and version strings are rejected: "
                    "they can drift or collide."
                ),
            )
        )

    url_failure = _validate_repo_url(aff.repo_url)
    if url_failure is not None:
        failures.append(url_failure)

    issue_failure = _validate_upstream_issue_url(aff.upstream_issue_url)
    if issue_failure is not None:
        failures.append(issue_failure)

    transcript_failure = _validate_transcript(
        candidate_dir=candidate_dir,
        transcript_path=aff.bench_transcript_path,
        pinned_commit=aff.pinned_commit,
    )
    if transcript_failure is not None:
        failures.append(transcript_failure)

    observed_failure = _validate_observed_behaviour(aff.observed_behaviour)
    if observed_failure is not None:
        failures.append(observed_failure)

    upstream_failure = _validate_upstream_status(aff.upstream_status)
    if upstream_failure is not None:
        failures.append(upstream_failure)

    if not aff.signed_by.strip():
        failures.append(
            ValidationFailure(
                field="signed_by",
                detail="empty or whitespace-only. The author's name is a required attestation.",
            )
        )

    ts_failure = _validate_iso8601(aff.signed_at)
    if ts_failure is not None:
        failures.append(ts_failure)

    return failures


# ---------------------------------------------------------------------------
# Field-level helpers. Each returns Optional[ValidationFailure] — None means
# the field is fine. Keeping them one-per-field means validate_affidavit()
# reads like an ordered checklist.
# ---------------------------------------------------------------------------
def _validate_repo_url(url: str) -> ValidationFailure | None:
    """URL must be non-empty and free of shell metacharacters (Rule 2)."""
    if not url.strip():
        return ValidationFailure(
            field="repo_url",
            detail="empty. Provide the canonical git URL (https or ssh).",
        )
    bad = _UNSAFE_URL_CHARS.intersection(url)
    if bad:
        return ValidationFailure(
            field="repo_url",
            detail=(
                f"contains unsafe characters {sorted(bad)!r}. "
                "The URL is passed to git; shell metacharacters are refused."
            ),
        )
    return None


def _validate_upstream_issue_url(url: str) -> ValidationFailure | None:
    """The issue URL fuels Gate 3 (writeup audit). Must resolve to an issue,
    not a plain repo. GitHub is the only host recognised at v2; expanding to
    GitLab / Bitbucket is a future schema bump.
    """
    if not url.strip():
        return ValidationFailure(
            field="upstream_issue_url",
            detail=(
                "empty. Provide the URL of the upstream issue this candidate "
                "targets — Gate 3 diffs the writeup against its thread."
            ),
        )
    bad = _UNSAFE_URL_CHARS.intersection(url)
    if bad:
        return ValidationFailure(
            field="upstream_issue_url",
            detail=(
                f"contains unsafe characters {sorted(bad)!r}. "
                "The URL is fetched over HTTP; shell metacharacters are refused."
            ),
        )
    # Loose shape check — an "issue"-shaped URL contains /issues/<n>. We accept
    # both github.com and api.github.com to keep the field forgiving; the
    # writeup-audit resolver normalises to the API form.
    if "/issues/" not in url:
        return ValidationFailure(
            field="upstream_issue_url",
            detail=(
                f"{url!r} does not look like an issue URL (missing '/issues/'). "
                "Point at the GitHub issue, not the repo root."
            ),
        )
    return None


def _validate_observed_behaviour(text: str) -> ValidationFailure | None:
    """Bounds-check the author's on-bench description."""
    stripped = text.strip()
    n = len(stripped)
    if n < _OBSERVED_MIN_CHARS:
        return ValidationFailure(
            field="observed_behaviour",
            detail=(
                f"{n} chars is too short (min {_OBSERVED_MIN_CHARS}). "
                "Two-to-four sentences describing what you saw on-bench "
                "at the pinned commit — not what the issue thread says."
            ),
        )
    if n > _OBSERVED_MAX_CHARS:
        return ValidationFailure(
            field="observed_behaviour",
            detail=(
                f"{n} chars is too long (max {_OBSERVED_MAX_CHARS}). "
                "Keep this to the observed symptom; extended narrative "
                "belongs in the task README."
            ),
        )
    return None


def _validate_upstream_status(status: str) -> ValidationFailure | None:
    """``open`` accepted; ``merged-pr-<n>`` and ``closed-fixed`` auto-reject."""
    if status == _UPSTREAM_STATUS_OPEN:
        return None
    if status == _UPSTREAM_STATUS_CLOSED_FIXED:
        return ValidationFailure(
            field="upstream_status",
            detail=(
                "closed-fixed: upstream has already shipped the fix. "
                "Fails the 'novel' quality; do not package."
            ),
        )
    if status.startswith(_UPSTREAM_STATUS_MERGED_PREFIX):
        pr_id = status[len(_UPSTREAM_STATUS_MERGED_PREFIX) :]
        if pr_id and pr_id.isdigit():
            return ValidationFailure(
                field="upstream_status",
                detail=(
                    f"merged-pr-{pr_id}: upstream PR has been merged. "
                    "Fails the 'novel' quality; do not package."
                ),
            )
    return ValidationFailure(
        field="upstream_status",
        detail=(
            f"{status!r} is not a recognised value. "
            f"Allowed: 'open', 'merged-pr-<N>', 'closed-fixed'."
        ),
    )


def _validate_iso8601(ts: str) -> ValidationFailure | None:
    """Parse an ISO-8601 timestamp; datetime.fromisoformat handles the shapes we care about."""
    stripped = ts.strip()
    if not stripped:
        return ValidationFailure(
            field="signed_at",
            detail="empty. Provide an ISO-8601 timestamp (e.g. 2026-08-06T14:32:00Z).",
        )
    # Python's fromisoformat accepts `Z` from 3.11 onward; we're on 3.12.
    try:
        datetime.fromisoformat(stripped)
    except ValueError as exc:
        return ValidationFailure(
            field="signed_at",
            detail=f"not a valid ISO-8601 timestamp ({exc}). Example: 2026-08-06T14:32:00Z.",
        )
    return None


def _validate_transcript(
    *,
    candidate_dir: Path,
    transcript_path: str,
    pinned_commit: str,
) -> ValidationFailure | None:
    """Confirm the ``.cast`` file exists, parses as asciinema v2, and mentions the pin.

    The ``bench_transcript_path`` is resolved relative to ``candidate_dir``
    (relative paths are the common case; absolute paths are accepted for
    author convenience but discouraged for portability).

    Checks (each in its own helper below) are ordered so the cheapest ones
    run first: path existence → size → decode → header → pin substring. We
    return on the first failure; the operator sees one transcript error at
    a time, but validate_affidavit() as a whole still returns *every*
    field-level failure per Rule 5's actionability.

    We deliberately do NOT parse the whole cast file. Signal is a bounded
    substring search; the harness's job is refusal, not forensics.
    """
    if not transcript_path.strip():
        return ValidationFailure(
            field="bench_transcript_path",
            detail="empty. Provide the path to an asciinema .cast recording.",
        )

    p = Path(transcript_path)
    if not p.is_absolute():
        p = candidate_dir / p

    file_failure = _validate_transcript_file(p)
    if file_failure is not None:
        return file_failure

    text_or_failure = _read_transcript_text(p)
    if isinstance(text_or_failure, ValidationFailure):
        return text_or_failure
    text = text_or_failure

    header_failure = _validate_asciicast_header(p, text)
    if header_failure is not None:
        return header_failure

    return _validate_pin_in_transcript(p, text, pinned_commit)


def _validate_transcript_file(p: Path) -> ValidationFailure | None:
    """Exists-and-sized check — pure filesystem, no content read."""
    if not p.is_file():
        return ValidationFailure(
            field="bench_transcript_path",
            detail=(
                f"{p} does not exist or is not a regular file. "
                "Record with: asciinema rec bench.cast"
            ),
        )
    try:
        size = p.stat().st_size
    except OSError as exc:
        return ValidationFailure(
            field="bench_transcript_path",
            detail=f"cannot stat {p}: {exc}",
        )
    if size == 0:
        return ValidationFailure(
            field="bench_transcript_path",
            detail=f"{p} is empty. Record the actual reproduction session.",
        )
    if size > _TRANSCRIPT_MAX_BYTES:
        return ValidationFailure(
            field="bench_transcript_path",
            detail=(
                f"{p} is {size} bytes; cap is {_TRANSCRIPT_MAX_BYTES}. "
                "Trim to just the reproduction; long sessions are hard to audit."
            ),
        )
    return None


def _read_transcript_text(p: Path) -> str | ValidationFailure:
    """Read the file with a replace-on-decode-error policy.

    Returns the text on success or a ValidationFailure on I/O error. We use
    a sum-typed return rather than raising so the caller can compose it
    with the other transcript checks without try/except plumbing.
    """
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ValidationFailure(
            field="bench_transcript_path",
            detail=f"cannot read {p}: {exc}",
        )


def _validate_pin_in_transcript(p: Path, text: str, pinned_commit: str) -> ValidationFailure | None:
    """The pinned SHA must appear somewhere in the recording."""
    if pinned_commit in text:
        return None
    return ValidationFailure(
        field="bench_transcript_path",
        detail=(
            f"the pinned commit {pinned_commit!r} does not appear anywhere in "
            f"{p.name}. The transcript must include the git checkout of the "
            "pinned SHA — otherwise there is no evidence of reproduction at "
            "that commit."
        ),
    )


def _validate_asciicast_header(path: Path, text: str) -> ValidationFailure | None:
    """First non-empty line of an asciicast v2 file is a JSON header object."""
    header_line: str | None = None
    for line in text.splitlines():
        if line.strip():
            header_line = line
            break
    if header_line is None:
        return ValidationFailure(
            field="bench_transcript_path",
            detail=f"{path.name} contains no non-empty lines.",
        )
    try:
        header = json.loads(header_line)
    except json.JSONDecodeError as exc:
        return ValidationFailure(
            field="bench_transcript_path",
            detail=(
                f"{path.name} first line is not JSON ({exc}). "
                "Expected an asciicast v2 header — record with 'asciinema rec'."
            ),
        )
    if not isinstance(header, dict) or header.get("version") != _ASCIICAST_V2_VERSION:
        return ValidationFailure(
            field="bench_transcript_path",
            detail=(
                f"{path.name} header is not asciicast v2 "
                f"(got version={header.get('version') if isinstance(header, dict) else None!r}). "
                "Re-record with a current asciinema client."
            ),
        )
    return None
