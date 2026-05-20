"""Script-based extraction of structured data from reviewer markdown output.

Pre-processes reviewer prose into compact JSON the synthesizer receives
instead of raw text. Reduces synthesizer input by ~10-20x for a typical
design-review round.

Extraction quality levels:
  "clean"   — verdict + findings parsed; synthesizer does not need raw text
  "partial" — some fields uncertain; raw text included as synthesizer fallback
  "fallback" — could not parse meaningfully; full raw text forwarded
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data classes

@dataclass
class ExtractedFinding:
    severity: str          # invariant_violation | risk | opinion
    summary: str
    targets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "summary": self.summary,
                "targets": list(self.targets)}


@dataclass
class ExtractedCoverageRow:
    req_id: str
    status: str            # satisfied | partial | missing | deviated | ambiguous
    evidence: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"req_id": self.req_id, "status": self.status,
                "evidence": self.evidence, "notes": self.notes}


@dataclass
class ExtractedReview:
    verdict: str                                      # pass | needs_revision | fail
    findings: list[ExtractedFinding] = field(default_factory=list)
    coverage: list[ExtractedCoverageRow] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)  # R<N> absent from coverage
    quality: str = "clean"                            # clean | partial | fallback
    file_path: str | None = None                      # resolved indirection path, if any

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
            "coverage": [c.to_dict() for c in self.coverage],
            "quality": self.quality,
        }
        if self.coverage_gaps:
            d["coverage_gaps"] = self.coverage_gaps
        if self.file_path:
            d["file_path"] = self.file_path
        return d


# ---------------------------------------------------------------------------
# File indirection resolver

_INDIRECTION_RE = re.compile(
    r'(?:written to|saved to|output(?:\s+has been)?\s+(?:written|saved)\s+to|'
    r'results?\s+(?:written|saved)\s+to|completed.*?written\s+to)\s+'
    r'[`"\']?(/[^\s`"\']+\.md)[`"\']?',
    re.IGNORECASE | re.DOTALL,
)


def _resolve_indirection(text: str) -> tuple[str, str | None]:
    """If text is a short stub mentioning a file path, read and return that file."""
    if len(text.strip()) > 1500:
        return text, None
    m = _INDIRECTION_RE.search(text)
    if not m:
        return text, None
    path = Path(m.group(1))
    if not path.exists() or not path.is_file():
        return text, None
    try:
        return path.read_text(encoding="utf-8"), str(path)
    except OSError:
        return text, None


# ---------------------------------------------------------------------------
# Verdict extraction

_VERDICT_RE = re.compile(
    r'\bverdict[:\s]+(?:is\s+)?'
    r'(pass|needs[_\- ]revision|fail|proceed|reject|revise)\b',
    re.IGNORECASE,
)
_VERDICT_MAP = {
    "pass": "pass", "proceed": "pass",
    "fail": "fail", "reject": "fail",
    "revise": "needs_revision",
    "needs_revision": "needs_revision",
    "needs-revision": "needs_revision",
    "needs revision": "needs_revision",
}


def _extract_verdict(text: str) -> tuple[str, bool]:
    """Return (verdict_str, is_confident)."""
    m = _VERDICT_RE.search(text)
    if m:
        raw = m.group(1).lower().replace("-", "_").replace(" ", "_")
        return _VERDICT_MAP.get(raw, "needs_revision"), True
    # Secondary: look for clear overall-verdict statements
    for pat, v in (
        (r'\bI (?:pass|approve)\b', "pass"),
        (r'\boverall[:\s]+pass\b', "pass"),
        (r'\boverall[:\s]+fail\b', "fail"),
        (r'\bstate my verdict[:\s]+pass\b', "pass"),
        (r'\bstate my verdict[:\s]+fail\b', "fail"),
        (r'\bstate my verdict[:\s]+needs_revision\b', "needs_revision"),
    ):
        if re.search(pat, text, re.IGNORECASE):
            return v, True
    return "needs_revision", False


# ---------------------------------------------------------------------------
# Coverage table extraction

_COVERAGE_HDR_RE = re.compile(
    r'\|\s*req_id\s*\|\s*status\s*\|\s*evidence\s*\|',
    re.IGNORECASE,
)
_VALID_STATUSES = {"satisfied", "partial", "missing", "deviated", "ambiguous"}


def _extract_coverage(text: str) -> list[ExtractedCoverageRow]:
    m = _COVERAGE_HDR_RE.search(text)
    if not m:
        return []
    rows: list[ExtractedCoverageRow] = []
    lines = text[m.start():].splitlines()
    past_sep = False
    for line in lines[1:]:            # skip header line
        s = line.strip()
        if not s.startswith("|"):
            if past_sep and rows:
                break
            continue
        if re.match(r'\|\s*[-:]+', s):
            past_sep = True
            continue
        if not past_sep:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3:
            continue
        req_id = cells[0]
        if not re.match(r'^R\d+$', req_id):
            continue
        status = cells[1].lower()
        if status not in _VALID_STATUSES:
            status = "ambiguous"
        evidence = cells[2] if len(cells) > 2 else ""
        notes = cells[3] if len(cells) > 3 else ""
        rows.append(ExtractedCoverageRow(req_id=req_id, status=status,
                                          evidence=evidence, notes=notes))
    return rows


# ---------------------------------------------------------------------------
# Finding extraction

_SEV_RE = re.compile(
    r'`?severity`?\s*[:\-]\s*`?(invariant_violation|risk|opinion)`?',
    re.IGNORECASE,
)
_SUMMARY_RE = re.compile(r'`?summary`?\s*[:\-]\s*(.+?)(?=\n|$)', re.IGNORECASE)
_TARGETS_RE = re.compile(r'`?targets`?\s*[:\-]\s*(.+?)(?=\n|$)', re.IGNORECASE)

_TARGET_ITEM_RE = re.compile(r'((?:primary_pair|anchor)\.[^\s,`\[\]]+)')


def _parse_targets(raw: str) -> list[str]:
    return _TARGET_ITEM_RE.findall(raw)


def _extract_findings(text: str) -> list[ExtractedFinding]:
    positions = [(m.start(), m.group(1).lower()) for m in _SEV_RE.finditer(text)]
    findings: list[ExtractedFinding] = []
    for i, (pos, severity) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        block = text[pos:end]
        sm = _SUMMARY_RE.search(block)
        if not sm:
            continue
        summary = re.sub(r'`', '', sm.group(1)).strip()[:1000]
        if not summary:
            continue
        targets: list[str] = []
        tm = _TARGETS_RE.search(block)
        if tm:
            targets = _parse_targets(tm.group(1))
        findings.append(ExtractedFinding(severity=severity, summary=summary,
                                          targets=targets))
    return findings


# ---------------------------------------------------------------------------
# Main entry point

_GATE_GAP_TARGET = {
    "design-review": "primary_pair.design.md",
    "close-approval": "primary_pair.build.json",
}


def parse_reviewer_output(
    text: str,
    prd_req_ids: set[str] | None = None,
    gate: str = "design-review",
) -> ExtractedReview:
    """Parse reviewer markdown into compact structured form.

    prd_req_ids: if provided, coverage_gaps is populated with any R<N>
    that the reviewer's coverage table did not address.
    gate: used to emit the correct routing target on coverage gap findings.
    """
    actual_text, file_path = _resolve_indirection(text)
    verdict, verdict_confident = _extract_verdict(actual_text)
    coverage = _extract_coverage(actual_text)
    findings = _extract_findings(actual_text)

    # Coverage gaps: R<N> in PRD but absent from reviewer's table
    gaps: list[str] = []
    if prd_req_ids:
        covered = {row.req_id for row in coverage}
        gaps = sorted(prd_req_ids - covered)

    gap_target = _GATE_GAP_TARGET.get(gate, "primary_pair.design.md")
    for req_id in gaps:
        findings.append(ExtractedFinding(
            severity="risk",
            summary=f"coverage gap: reviewer did not address {req_id}",
            targets=[gap_target],
        ))

    # Quality: clean when both verdict and at least findings structure parsed
    if verdict_confident and findings:
        quality = "clean"
    elif verdict_confident or findings:
        quality = "partial"
    else:
        quality = "fallback"

    return ExtractedReview(
        verdict=verdict,
        findings=findings,
        coverage=coverage,
        coverage_gaps=gaps,
        quality=quality,
        file_path=file_path,
    )
