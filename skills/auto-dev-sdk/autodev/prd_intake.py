"""Stage 0 — PRD intake validator.

Mechanical schema check that runs before the orchestrator dispatches
scope. Ensures downstream stages (scope → G1 precheck) see a PRD whose
shape they can rely on.

What this validates:

1. Six canonical top-level sections present (case-insensitive, order
   not enforced):
     Problem / Users / Requirements / Constraints / Success criteria
     / Out of scope
2. The Requirements section contains at least one ``### R<N>:``
   addressable marker; every marker's N is a positive integer and
   unique.
3. The PRD is not empty.

What this does NOT do (by design — those belong elsewhere):

- Semantic review of PRD quality → G1 panel review's job.
- Interactive interview for missing content → a future outer skill.
- Attribution-tag / Source: validation → that's R7 territory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


REQUIRED_SECTIONS: tuple[str, ...] = (
    "problem",
    "users",
    "requirements",
    "constraints",
    "success criteria",
    "out of scope",
)


@dataclass
class PrdIntakeResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    # For tooling that wants to display the parsed structure.
    sections_found: list[str] = field(default_factory=list)
    requirement_markers: list[str] = field(default_factory=list)

    def format_errors(self) -> str:
        return "\n".join(f"  - {e}" for e in self.errors)


_H2_RE = re.compile(r"^\s*##\s+(.+?)\s*$")
_R_MARKER_RE = re.compile(r"^\s*###\s+R(\d+)\s*:\s*(.+?)\s*$")


def _extract_h2_sections(text: str) -> list[tuple[int, str]]:
    """Return list of (line_index, normalized_name) for every ``## X`` header.

    Normalization strips leading/trailing whitespace + lowercases +
    collapses runs of whitespace to single space. ``## 3. Requirements``
    normalizes to ``requirements`` (leading-digit prefix stripped).
    """
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines()):
        m = _H2_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip().lower()
        # Strip optional numeric prefix ``1.`` / ``1)`` / ``§1``.
        name = re.sub(r"^(§?\d+[\.\)]?\s+)", "", name)
        # Collapse internal whitespace.
        name = re.sub(r"\s+", " ", name)
        out.append((i, name))
    return out


def _requirements_body(text: str, sections: list[tuple[int, str]]) -> str:
    """Extract the body text of the Requirements section (between its
    ``##`` header and the next ``##`` header or EOF)."""
    lines = text.splitlines()
    # Find requirements header index.
    req_idx = next((i for (i, n) in sections if n == "requirements"), None)
    if req_idx is None:
        return ""
    # Next H2 index (if any).
    next_h2 = next(
        (i for (i, _) in sections if i > req_idx),
        len(lines),
    )
    return "\n".join(lines[req_idx + 1:next_h2])


def _requirement_declaration_bodies(
    text: str, sections: list[tuple[int, str]],
) -> list[str]:
    """Bodies allowed to introduce addressable requirements.

    The original Requirements section and append-only, date-stamped Amendment
    sections are authoritative. This makes ``autodev update --amendment`` able
    to add a requirement without rewriting the PRD, while unrelated headings
    and fenced examples remain non-authoritative.
    """
    lines = text.splitlines()
    bodies: list[str] = []
    for position, (start, name) in enumerate(sections):
        if name != "requirements" and not re.fullmatch(
            r"amendment(?:\s+\d{4}-\d{2}-\d{2})?", name,
        ):
            continue
        end = sections[position + 1][0] if position + 1 < len(sections) else len(lines)
        bodies.append("\n".join(lines[start + 1:end]))
    return bodies


def _unfenced_lines(body: str):
    fence: str | None = None
    for line in body.splitlines():
        stripped = line.lstrip()
        marker = "```" if stripped.startswith("```") else (
            "~~~" if stripped.startswith("~~~") else None
        )
        if marker is not None:
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None:
            yield line


def validate_prd_text(text: str) -> PrdIntakeResult:
    """Run the Stage 0 mechanical checks against PRD markdown text."""
    errors: list[str] = []
    sections_found: list[str] = []
    markers_found: list[str] = []

    if not text.strip():
        return PrdIntakeResult(ok=False, errors=["prd is empty"])

    h2s = _extract_h2_sections(text)
    sections_found = [name for (_, name) in h2s]

    missing = [s for s in REQUIRED_SECTIONS if s not in sections_found]
    if missing:
        errors.append(
            f"missing required section(s) {missing!r}; expected all of "
            f"{list(REQUIRED_SECTIONS)!r}"
        )

    # Requirement markers in the original section plus append-only
    # amendments (only if Requirements section is present).
    if "requirements" in sections_found:
        seen_n: set[int] = set()
        for body in _requirement_declaration_bodies(text, h2s):
            for line in _unfenced_lines(body):
                m = _R_MARKER_RE.match(line)
                if not m:
                    continue
                n = int(m.group(1))
                marker = f"R{n}"
                if n in seen_n:
                    errors.append(
                        f"duplicate requirement marker {marker!r} across "
                        "Requirements and Amendment sections"
                    )
                seen_n.add(n)
                markers_found.append(marker)
        if not markers_found:
            errors.append(
                "Requirements section contains no `### R<N>:` markers"
            )

    # Optional `## Assurance` section (per-requirement rigor levels).
    # Absent section is valid (all-strict default); present-but-
    # malformed is a lint error.
    from autodev.assurance import parse_assurance
    _, assurance_errors = parse_assurance(text, known_rs=set(markers_found))
    errors.extend(assurance_errors)

    return PrdIntakeResult(
        ok=not errors,
        errors=errors,
        sections_found=sections_found,
        requirement_markers=markers_found,
    )


def validate_prd_file(path: Path) -> PrdIntakeResult:
    p = Path(path)
    if not p.exists():
        return PrdIntakeResult(ok=False, errors=[f"prd not found at {p}"])
    return validate_prd_text(p.read_text(encoding="utf-8"))
