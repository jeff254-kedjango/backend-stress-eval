"""Own-words writeup audit (Gate 3 of the sourcing gates).

Framework-agnostic. See :file:`upgrade-plan.md` §4 Gate 3 and :file:`rules.md`
Rule 13 for the standing rules that back this module.

This module implements the mechanical enforcement of "the writeup files
shipped with a candidate must be authored from the affidavit's
``observed_behaviour``, not paraphrased from the upstream issue thread."
The failure mode this closes: authors reading the issue, absorbing the
thread's framing, and reproducing that framing in ``initial-prompt.md``.
On dramatiq #431 the thread's fictional history entered the writeup that
way.

The audit:

1. Loads the candidate's affidavit (Chunk A) to get ``upstream_issue_url``.
2. Fetches the issue body + top N comments over the GitHub REST API.
   Uses ``urllib.request`` — no runtime dep on ``requests``.
3. Loads every writeup file in the candidate directory (``initial-prompt.md``,
   ``README.md``, ``grading-criteria.md``, ``RUBRIC.md`` — every one that
   exists).
4. Extracts all contiguous ≥ 8-word phrases from the writeup files.
5. Reports every phrase that appears verbatim in the upstream text, unless
   it is annotated in-line as a legitimately-shared technical term
   (``<!-- own-words: shared-term -->`` marker).

A snapshot of the upstream text is written alongside the audit report so
offline audits and reruns are reproducible. If the network is unavailable
at audit time and a snapshot exists on disk, the snapshot is used and the
report notes the fallback.

Design notes:

* Pure stdlib. ``urllib.request`` covers GitHub's public JSON API — no
  auth needed for public repos within rate limits. Authenticated
  audits (higher rate limits) are a future addition.
* Every network call has a bounded timeout (10 seconds) and a bounded
  response size (2 MiB per request, hard-capped by reading a limited
  number of bytes). Rule 2: fail closed on oversized input.
* Rule 1: n-gram extraction is O(w) in writeup words. Substring matching
  is O(u) in upstream chars per phrase, but we do it once against a
  concatenated corpus so the total is O(w + u) not O(w · u).
* Rule 5: every failure path returns a structured record with an
  actionable message.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from core.affidavit import Affidavit, load_affidavit

__all__ = [
    "AUDIT_REPORT_FILENAME",
    "SNAPSHOT_FILENAME",
    "WRITEUP_FILES",
    "AuditFinding",
    "AuditReport",
    "WriteupAuditError",
    "run_writeup_audit",
]

# ---------------------------------------------------------------------------
# Gate constants. See upgrade-plan.md §4 Gate 3.
# ---------------------------------------------------------------------------
AUDIT_REPORT_FILENAME: Final = "writeup-audit.txt"
SNAPSHOT_FILENAME: Final = "upstream-issue-snapshot.txt"

WRITEUP_FILES: Final = (
    "initial-prompt.md",
    "README.md",
    "grading-criteria.md",
    "RUBRIC.md",
)

# Minimum phrase length that counts as a "match". Longer = fewer false
# positives from generic English ("in the case of", "we should be able to");
# shorter = more sensitivity to inherited prose. 8 words matches the rule
# text and the reviewer's implicit standard.
_MIN_WORDS_FOR_MATCH: Final = 8

# Bounded network reads. See Rule 2.
_HTTP_TIMEOUT_SECONDS: Final = 10.0
_HTTP_MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024  # 2 MiB
# GitHub returns comments paginated; fetch at most this many pages.
# Real threads with more comments than 100 per-page over 5 pages (500)
# are unusually long and typically indicate the "convergent" shape the
# gate exists to reject anyway.
_MAX_COMMENT_PAGES: Final = 5
_GH_PER_PAGE: Final = 100
# GitHub issue URL path shape: /<owner>/<repo>/issues/<n> → 4 segments.
_GH_ISSUE_PATH_SEGMENTS: Final = 4

# Regex for the own-words annotation marker.
# Format: <!-- own-words: <reason> --> — the marker sits on its own line
# immediately after a paragraph or heading and applies to matches within
# the containing paragraph.
_OWN_WORDS_MARKER_RE: Final = re.compile(r"<!--\s*own-words:\s*(?P<reason>[^-]+?)\s*-->")

# Tokenizer for n-gram extraction. We split on non-word characters and
# lowercase; this keeps punctuation-adjacent matches equivalent to plain
# text ones.
_WORD_RE: Final = re.compile(r"[A-Za-z0-9_']+")


# ---------------------------------------------------------------------------
# Public data + error types.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AuditFinding:
    """One flagged contiguous-word match between a writeup file and the upstream text."""

    writeup_file: str
    phrase: str
    word_count: int
    starting_word_index: int


@dataclass(frozen=True, slots=True)
class AuditReport:
    """The overall audit verdict for one candidate."""

    candidate_dir: str
    upstream_issue_url: str
    upstream_source: str  # "live" | "snapshot"
    findings: tuple[AuditFinding, ...]
    files_scanned: tuple[str, ...] = field(default=())

    @property
    def passed(self) -> bool:
        """No unresolved matches == the gate passes."""
        return len(self.findings) == 0

    def to_text(self) -> str:
        """Human-readable report — this is what's written to writeup-audit.txt."""
        verdict = "PASS" if self.passed else "REJECT"
        lines = [
            f"writeup audit: {verdict}",
            f"  candidate: {self.candidate_dir}",
            f"  issue: {self.upstream_issue_url}",
            f"  source: {self.upstream_source}",
            f"  files scanned: {', '.join(self.files_scanned) or '(none)'}",
            f"  findings: {len(self.findings)}",
        ]
        for i, f in enumerate(self.findings, 1):
            lines.append(
                f"  #{i} [{f.writeup_file}] words {f.starting_word_index}-"
                f"{f.starting_word_index + f.word_count - 1}: {f.phrase!r}"
            )
        return "\n".join(lines) + "\n"


class WriteupAuditError(RuntimeError):
    """Structural precondition failed — no affidavit, no writeup, no network+snapshot."""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def run_writeup_audit(
    candidate_dir: Path,
    /,
    *,
    write_report: bool = True,
    fetch_live: bool = True,
) -> AuditReport:
    """Run the audit and return a structured report.

    Preconditions (raise :class:`WriteupAuditError`):

    * ``candidate_dir`` is a directory containing a valid ``repro-affidavit.json``.
    * At least one writeup file from ``WRITEUP_FILES`` exists.
    * Upstream text is reachable: either the network fetch succeeds or a
      committed ``upstream-issue-snapshot.txt`` exists.

    Writes ``writeup-audit.txt`` and (on live fetch) refreshes
    ``upstream-issue-snapshot.txt``. ``write_report=False`` and
    ``fetch_live=False`` exist for tests.
    """
    affidavit = _load_affidavit_or_raise(candidate_dir)
    files, corpus = _collect_writeup_files(candidate_dir)
    upstream_text, source = _obtain_upstream_text(
        candidate_dir=candidate_dir,
        issue_url=affidavit.upstream_issue_url,
        fetch_live=fetch_live,
    )
    upstream_normalised = _normalise_text(upstream_text)

    findings: list[AuditFinding] = []
    for filename, text in files:
        annotated_ranges = _annotated_ranges(text)
        for phrase, start_word_index in _iter_phrases(text):
            if _phrase_in_range(text, start_word_index, annotated_ranges):
                continue
            if _normalise_text(phrase) in upstream_normalised:
                findings.append(
                    AuditFinding(
                        writeup_file=filename,
                        phrase=phrase,
                        word_count=len(phrase.split()),
                        starting_word_index=start_word_index,
                    )
                )

    report = AuditReport(
        candidate_dir=str(candidate_dir.resolve()),
        upstream_issue_url=affidavit.upstream_issue_url,
        upstream_source=source,
        findings=_dedupe_findings(findings),
        files_scanned=tuple(f for f, _ in files),
    )

    if write_report:
        (candidate_dir / AUDIT_REPORT_FILENAME).write_text(report.to_text(), encoding="utf-8")
        if source == "live":
            (candidate_dir / SNAPSHOT_FILENAME).write_text(upstream_text, encoding="utf-8")

    return report


# ---------------------------------------------------------------------------
# Preconditions and I/O.
# ---------------------------------------------------------------------------
def _load_affidavit_or_raise(candidate_dir: Path) -> Affidavit:
    """Wrap :func:`load_affidavit` to remap the error class."""
    try:
        return load_affidavit(candidate_dir)
    except Exception as exc:
        raise WriteupAuditError(
            f"cannot audit {candidate_dir}: affidavit prerequisite failed: {exc}"
        ) from exc


def _collect_writeup_files(candidate_dir: Path) -> tuple[list[tuple[str, str]], str]:
    """Return [(filename, contents), ...] for every WRITEUP_FILES that exists.

    Returns a concatenated corpus alongside for downstream use if needed.
    Raises if none of the writeup files are present — an empty candidate
    isn't auditable and the operator should know.
    """
    out: list[tuple[str, str]] = []
    for name in WRITEUP_FILES:
        p = candidate_dir / name
        if p.is_file():
            out.append((name, p.read_text(encoding="utf-8")))
    if not out:
        raise WriteupAuditError(
            f"no writeup files under {candidate_dir}. "
            f"At least one of {list(WRITEUP_FILES)} must exist."
        )
    corpus = "\n\n".join(text for _, text in out)
    return out, corpus


def _obtain_upstream_text(
    *, candidate_dir: Path, issue_url: str, fetch_live: bool
) -> tuple[str, str]:
    """Try live fetch first (unless disabled), fall back to snapshot on disk.

    Returns (text, "live" | "snapshot"). Raises if neither is available.
    """
    snapshot_path = candidate_dir / SNAPSHOT_FILENAME
    if fetch_live:
        try:
            live = _fetch_issue_text(issue_url)
            return live, "live"
        except (urllib.error.URLError, WriteupAuditError, TimeoutError) as exc:
            if snapshot_path.is_file():
                return snapshot_path.read_text(encoding="utf-8"), "snapshot"
            raise WriteupAuditError(
                f"live fetch failed ({exc}) and no {SNAPSHOT_FILENAME} at "
                f"{snapshot_path}. Provide network or commit a snapshot."
            ) from exc
    if snapshot_path.is_file():
        return snapshot_path.read_text(encoding="utf-8"), "snapshot"
    raise WriteupAuditError(f"fetch_live=False and no {SNAPSHOT_FILENAME} at {snapshot_path}.")


def _fetch_issue_text(issue_url: str) -> str:
    """Fetch the issue body + all comments as one concatenated text block.

    Only GitHub-shaped URLs are supported at v2. Normalisation:
    ``https://github.com/<owner>/<repo>/issues/<n>`` becomes
    ``https://api.github.com/repos/<owner>/<repo>/issues/<n>``.
    """
    parsed = _parse_github_issue_url(issue_url)
    if parsed is None:
        raise WriteupAuditError(
            f"cannot parse GitHub issue URL {issue_url!r}. "
            "Expected https://github.com/<owner>/<repo>/issues/<n>."
        )
    owner, repo, number = parsed
    base = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"

    issue_json = _http_get_json(base)
    if not isinstance(issue_json, dict):
        raise WriteupAuditError(
            f"expected JSON object from {base}, got {type(issue_json).__name__}"
        )
    body = issue_json.get("body") or ""
    title = issue_json.get("title") or ""

    comments_url = f"{base}/comments?per_page={_GH_PER_PAGE}"
    comment_bodies: list[str] = []
    for page in range(1, _MAX_COMMENT_PAGES + 1):
        page_url = f"{comments_url}&page={page}"
        page_json = _http_get_json(page_url)
        if not isinstance(page_json, list) or not page_json:
            break
        for c in page_json:
            if isinstance(c, dict):
                cb = c.get("body")
                if isinstance(cb, str):
                    comment_bodies.append(cb)
        if len(page_json) < _GH_PER_PAGE:
            break

    return "\n\n".join([title, body, *comment_bodies])


def _parse_github_issue_url(url: str) -> tuple[str, str, str] | None:
    """Return (owner, repo, number) or None if the URL isn't GitHub-issue-shaped."""
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc not in {"github.com", "www.github.com"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    # /<owner>/<repo>/issues/<n>
    if len(parts) != _GH_ISSUE_PATH_SEGMENTS or parts[2] != "issues" or not parts[3].isdigit():
        return None
    return parts[0], parts[1], parts[3]


def _http_get_json(url: str) -> object:
    """GET ``url`` and parse the response as JSON, with bounded I/O.

    urllib is chosen over ``requests`` to keep runtime deps at zero
    (Rule 4 / project policy). The ``User-Agent`` header is required
    by the GitHub API — omitting it 403s.
    """
    if not url.startswith("https://api.github.com/"):
        # Belt-and-braces: _fetch_issue_text builds this URL, but the
        # guard makes ruff S310 explicit and blocks any future refactor
        # that would let a caller pass in an untrusted scheme.
        raise WriteupAuditError(f"refusing to fetch non-GitHub-API URL {url!r}")
    req = urllib.request.Request(  # noqa: S310 -- scheme guarded above
        url,
        headers={
            "User-Agent": "backend-stress-eval-writeup-audit/1.0",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 -- scheme guarded above
            req, timeout=_HTTP_TIMEOUT_SECONDS
        ) as resp:
            raw = resp.read(_HTTP_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise WriteupAuditError(f"HTTP {exc.code} fetching {url}: {exc.reason}") from exc
    if len(raw) > _HTTP_MAX_RESPONSE_BYTES:
        raise WriteupAuditError(
            f"response from {url} exceeds {_HTTP_MAX_RESPONSE_BYTES} bytes; refusing."
        )
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise WriteupAuditError(f"non-JSON response from {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Phrase extraction + matching.
# ---------------------------------------------------------------------------
def _iter_phrases(text: str) -> list[tuple[str, int]]:
    """Yield every contiguous _MIN_WORDS_FOR_MATCH-word phrase in the text.

    Returns (phrase, starting_word_index) tuples. Word indices are into the
    tokenised word list, not char offsets — the report uses these for
    operator navigation ("words 42-49").

    We keep this to O(w) — one pass over the token list — because the
    downstream substring check dominates asymptotics anyway.
    """
    words = _WORD_RE.findall(text)
    out: list[tuple[str, int]] = []
    for i in range(len(words) - _MIN_WORDS_FOR_MATCH + 1):
        phrase = " ".join(words[i : i + _MIN_WORDS_FOR_MATCH])
        out.append((phrase, i))
    return out


def _normalise_text(text: str) -> str:
    """Lower-case, collapse whitespace, strip punctuation to word boundaries.

    Both the phrase and the upstream text pass through this so the match
    is punctuation- and case-insensitive without either side being modified
    in the report.
    """
    words = _WORD_RE.findall(text.lower())
    return " ".join(words)


def _annotated_ranges(text: str) -> list[tuple[int, int]]:
    """Return character ranges of paragraphs marked with the own-words annotation.

    An annotation applies to the paragraph it's inside — the smallest
    containing double-newline-delimited block. This is looser than
    scoped-to-preceding-line and stricter than scoped-to-file; matches
    the ergonomic sweet spot of "annotate one paragraph at a time."
    """
    ranges: list[tuple[int, int]] = []
    paragraphs = _paragraph_spans(text)
    for start, end in paragraphs:
        block = text[start:end]
        if _OWN_WORDS_MARKER_RE.search(block):
            ranges.append((start, end))
    return ranges


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Yield (start_char, end_char) for every paragraph in ``text``."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for chunk in re.split(r"\n\s*\n", text):
        chunk_len = len(chunk)
        spans.append((cursor, cursor + chunk_len))
        # +2 to skip the double-newline separator we split on.
        cursor += chunk_len + 2
    return spans


def _phrase_in_range(
    text: str,
    starting_word_index: int,
    annotated_ranges: list[tuple[int, int]],
) -> bool:
    """Return True if the phrase (by starting word index) is inside an annotated paragraph.

    We locate the phrase in the source text by scanning for its starting
    word; this is O(char_of_text) per call. For the phrase counts we
    actually see per candidate (dozens to low hundreds), the constant
    factor is negligible.
    """
    if not annotated_ranges:
        return False
    words_iter = list(_WORD_RE.finditer(text))
    if starting_word_index >= len(words_iter):
        return False
    char_offset = words_iter[starting_word_index].start()
    return any(start <= char_offset < end for start, end in annotated_ranges)


def _dedupe_findings(findings: list[AuditFinding]) -> tuple[AuditFinding, ...]:
    """Collapse overlapping findings from the same file to the longest match.

    Sliding-window phrase extraction produces N overlapping matches for
    every real hit (one per starting word within the match). The report
    should show one row per human-perceived match, so we merge windows
    that touch the same file and overlap in word range.
    """
    findings.sort(key=lambda f: (f.writeup_file, f.starting_word_index))
    out: list[AuditFinding] = []
    for f in findings:
        if out and out[-1].writeup_file == f.writeup_file:
            prev = out[-1]
            prev_end = prev.starting_word_index + prev.word_count
            if f.starting_word_index <= prev_end:
                # Extend the previous finding to cover this one.
                new_end = max(prev_end, f.starting_word_index + f.word_count)
                offset = f.starting_word_index - prev.starting_word_index
                out[-1] = AuditFinding(
                    writeup_file=prev.writeup_file,
                    phrase=_extend_phrase(prev.phrase, f.phrase, offset),
                    word_count=new_end - prev.starting_word_index,
                    starting_word_index=prev.starting_word_index,
                )
                continue
        out.append(f)
    return tuple(out)


def _extend_phrase(prev: str, curr: str, offset: int) -> str:
    """Concatenate the non-overlapping tail of ``curr`` onto ``prev``.

    ``offset`` is the difference in starting word index. If offset >= words
    in prev, they don't actually overlap and we glue with a space; if it
    does, we skip the overlapping prefix on curr.
    """
    prev_words = prev.split()
    curr_words = curr.split()
    if offset >= len(prev_words):
        return " ".join(prev_words + curr_words)
    tail = curr_words[len(prev_words) - offset :]
    return " ".join(prev_words + tail)
