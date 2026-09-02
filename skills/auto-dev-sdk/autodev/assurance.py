"""Per-requirement rigor levels — the PRD `## Assurance` section.

Grammar (see docs/proposals/rigor-tier.md):

    ## Assurance

    Default: loose
    Release threshold: P0

    | Req | Rigor | Rationale |
    |---|---|---|
    | R1 | strict | Core algorithm under test |

- The `Default:` line is required when the section is present.
- Table rows are needed only for Rs that deviate from the default
  (full enumeration also accepted). One row per R, non-empty
  rationale.
- Section absent entirely → every R is `strict` (byte-identical to
  pre-Assurance pipeline behavior; existing PRDs need no migration).
- Amendment blocks may override per-R levels with lines matching
  `Assurance: R<n> <from> -> <to>`. Later lines win (append-only
  PRD; the newest amendment is the lowest in the file).

Rigor lives in the PRD only — never in scope.json — so the design
agent cannot lower a requirement's level under gate pressure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

Level = str  # "strict" | "core" | "loose"

VALID_LEVELS: tuple[str, ...] = ("strict", "core", "loose")

# Mechanism 4 — forethought directive per R. `upfront` forces full
# interior design; `defer` forces contract-only; `auto` (default)
# delegates the choice to the design agent + panel adjudication.
VALID_DEPTHS: tuple[str, ...] = ("upfront", "auto", "defer")

# Most-conservative-wins across the Rs one scope item cites.
_DEPTH_RANK: dict[str, int] = {"defer": 0, "auto": 1, "upfront": 2}

# Ordering for Rule A (max across cited Rs): strict > core > loose.
_LEVEL_RANK: dict[str, int] = {"loose": 0, "core": 1, "strict": 2}

_H2_RE = re.compile(r"^\s*##\s+(.+?)\s*$")
_R_MARKER_RE = re.compile(r"^\s*###\s+R(\d+)\s*:")
_DEFAULT_RE = re.compile(r"^\s*Default\s*:\s*(\S+)\s*$", re.IGNORECASE)
_RELEASE_THRESHOLD_RE = re.compile(
    r"^\s*Release\s+threshold\s*:\s*(\S+)\s*$", re.IGNORECASE,
)
_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_R_TOKEN_RE = re.compile(r"^R(\d+)$")
# Amendment override: `Assurance: R3 core -> strict` (from-level optional).
_OVERRIDE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?Assurance\s*:\s*R(\d+)\s+(?:(\w+)\s+)?->\s*(\w+)\s*$",
    re.IGNORECASE,
)
# Depth override: `Depth: R3 auto -> upfront` (from-value optional).
_DEPTH_OVERRIDE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?Depth\s*:\s*R(\d+)\s+(?:(\w+)\s+)?->\s*(\w+)\s*$",
    re.IGNORECASE,
)


def max_level(levels: list[Level]) -> Level:
    """Strictest of ``levels`` (Rule A). Empty → strict (fail closed)."""
    if not levels:
        return "strict"
    return max(levels, key=lambda l: _LEVEL_RANK.get(l, _LEVEL_RANK["strict"]))


def max_depth(depths: list[str]) -> str:
    """Most conservative of ``depths`` (upfront > auto > defer).
    Empty → auto."""
    if not depths:
        return "auto"
    return max(depths, key=lambda d: _DEPTH_RANK.get(d, _DEPTH_RANK["upfront"]))


@dataclass
class AssuranceMap:
    """Resolved rigor map for one PRD."""
    present: bool = False
    default: Level = "strict"
    per_r: dict[str, Level] = field(default_factory=dict)
    rationale: dict[str, str] = field(default_factory=dict)
    # Mechanism 4 — per-R forethought directive; unspecified = "auto".
    depth_per_r: dict[str, str] = field(default_factory=dict)
    # P1 preserves the historical gate: invariant violations and risks
    # block, opinions do not. A PRD may opt into P0-only release gating.
    release_threshold: str = "P1"
    # Requirement markers that actually exist in the PRD (`### R<n>:`).
    # None = unknown (map constructed without PRD text; no filtering).
    # The rigor filter drops reviewer-cited `prd:R<n>` tokens not in
    # this set — otherwise a hallucinated/typo'd R resolves to the
    # `Default:` level and a blocking finding downgrades silently
    # (Rule B failing open).
    known_rs: set[str] | None = None

    def level_for(self, r: str) -> Level:
        """Rigor level for requirement token ``r`` (e.g. ``"R3"``)."""
        return self.per_r.get(r, self.default)

    def depth_for(self, r: str) -> str:
        """Forethought directive for ``r``: upfront | auto | defer."""
        return self.depth_per_r.get(r, "auto")

    def to_dict(self) -> dict:
        return {
            "present": self.present,
            "default": self.default,
            "per_r": dict(self.per_r),
            "release_threshold": self.release_threshold,
        }


def _section_body(text: str, name: str) -> str | None:
    """Body of the first ``## <name>`` section, or None if absent."""
    lines = text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        m = _H2_RE.match(line)
        if not m:
            continue
        header = _normalized_h2(m.group(1))
        if start is None and header == name:
            start = i
            continue
        if start is not None:
            return "\n".join(lines[start + 1:i])
    if start is not None:
        return "\n".join(lines[start + 1:])
    return None


def _normalized_h2(raw: str) -> str:
    header = re.sub(r"^(§?\d+[\.\)]?\s+)", "", raw.strip().lower())
    return re.sub(r"\s+", " ", header)


def _unfenced_lines(body: str):
    """Yield directive-eligible lines, ignoring fenced examples."""
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


def _amendment_bodies(text: str) -> list[str]:
    """Bodies of explicit ``## Amendment ...`` sections."""
    bodies: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        match = _H2_RE.match(line)
        if match:
            if current is not None:
                bodies.append("\n".join(current))
            header = _normalized_h2(match.group(1))
            current = [] if re.fullmatch(
                r"amendment(?:\s+\d{4}-\d{2}-\d{2})?", header,
            ) else None
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        bodies.append("\n".join(current))
    return bodies


def _parse_rows(
    body: str, errors: list[str],
) -> tuple[dict[str, Level], dict[str, str], dict[str, str]]:
    """Header-driven table parse: supports both
    ``| Req | Rigor | Rationale |`` and
    ``| Req | Rigor | Depth | Rationale |`` (column order taken from
    the header row; defaults to the 3-column layout when no header is
    recognizable)."""
    per_r: dict[str, Level] = {}
    rationale: dict[str, str] = {}
    depth_per_r: dict[str, str] = {}
    col = {"req": 0, "rigor": 1, "rationale": 2}
    depth_col: int | None = None
    for line in body.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if not cells:
            continue
        lower = [c.lower() for c in cells]
        if lower[0] in ("req", "requirement"):
            # Header row — learn column positions.
            for i, name in enumerate(lower):
                if name in ("req", "requirement"):
                    col["req"] = i
                elif name == "rigor":
                    col["rigor"] = i
                elif name == "rationale":
                    col["rationale"] = i
                elif name == "depth":
                    depth_col = i
            continue
        if all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
            continue

        def cell(i: int | None) -> str:
            return cells[i] if i is not None and i < len(cells) else ""

        if not _R_TOKEN_RE.match(cell(col["req"])):
            errors.append(
                f"Assurance row {cell(col['req'])!r} is not a requirement "
                f"token (expected R<n>)"
            )
            continue
        r = cell(col["req"])
        if cell(col["rigor"]).lower() not in VALID_LEVELS:
            errors.append(
                f"Assurance row {r}: rigor level must be one of "
                f"{list(VALID_LEVELS)!r}, got {cell(col['rigor'])!r}"
            )
            continue
        if r in per_r:
            errors.append(f"Assurance row {r}: duplicate row")
            continue
        if not cell(col["rationale"]):
            errors.append(f"Assurance row {r}: rationale is required")
            continue
        if depth_col is not None and cell(depth_col):
            d = cell(depth_col).lower()
            if d not in VALID_DEPTHS:
                errors.append(
                    f"Assurance row {r}: depth must be one of "
                    f"{list(VALID_DEPTHS)!r}, got {cell(depth_col)!r}"
                )
                continue
            if d != "auto":
                depth_per_r[r] = d
        per_r[r] = cell(col["rigor"]).lower()
        rationale[r] = cell(col["rationale"])
    return per_r, rationale, depth_per_r


def _apply_overrides(
    text: str, per_r: dict[str, Level], rationale: dict[str, str],
    depth_per_r: dict[str, str], errors: list[str],
) -> None:
    """Apply `Assurance: R<n> <from> -> <to>` and
    `Depth: R<n> <from> -> <to>` amendment lines in file order
    (append-only PRD ⇒ last line wins)."""
    for line in text.splitlines():
        m = _OVERRIDE_RE.match(line)
        if m:
            r = f"R{m.group(1)}"
            to = m.group(3).lower()
            if to not in VALID_LEVELS:
                errors.append(
                    f"Assurance override for {r}: target level {m.group(3)!r} "
                    f"must be one of {list(VALID_LEVELS)!r}"
                )
                continue
            per_r[r] = to
            rationale.setdefault(r, "")
            rationale[r] = f"amended: {line.strip()}"
            continue
        m = _DEPTH_OVERRIDE_RE.match(line)
        if m:
            r = f"R{m.group(1)}"
            to = m.group(3).lower()
            if to not in VALID_DEPTHS:
                errors.append(
                    f"Depth override for {r}: target depth {m.group(3)!r} "
                    f"must be one of {list(VALID_DEPTHS)!r}"
                )
                continue
            if to == "auto":
                depth_per_r.pop(r, None)
            else:
                depth_per_r[r] = to


def parse_assurance(
    text: str, known_rs: set[str] | None = None,
) -> tuple[AssuranceMap, list[str]]:
    """Parse the PRD's Assurance map.

    Returns ``(map, errors)``. ``errors`` is non-empty only when the
    section (or an override line) is present but malformed; an absent
    section is valid and yields the all-strict default map.

    ``known_rs`` (e.g. from prd_intake's requirement markers) enables
    the unknown-R check; when None that check is skipped.
    """
    errors: list[str] = []
    body = _section_body(text, "assurance")

    per_r: dict[str, Level] = {}
    rationale: dict[str, str] = {}
    depth_per_r: dict[str, str] = {}
    default: Level = "strict"
    release_threshold = "P1"
    present = body is not None

    if body is not None:
        default_m = None
        for line in body.splitlines():
            m = _DEFAULT_RE.match(line)
            if m:
                default_m = m
                break
        if default_m is None:
            errors.append("Assurance section is missing the `Default:` line")
        elif default_m.group(1).lower() not in VALID_LEVELS:
            errors.append(
                f"Assurance Default level {default_m.group(1)!r} must be "
                f"one of {list(VALID_LEVELS)!r}"
            )
        else:
            default = default_m.group(1).lower()
        row_r, row_rat, row_depth = _parse_rows(body, errors)
        per_r.update(row_r)
        rationale.update(row_rat)
        depth_per_r.update(row_depth)

    # Amendment overrides apply whether or not the section exists —
    # an amendment may introduce the first per-R deviation.
    _apply_overrides(text, per_r, rationale, depth_per_r, errors)
    # Like per-R Assurance overrides, an amendment may change the release
    # threshold. Only directive sections count; prose and fenced examples in
    # requirements/constraints must never silently weaken the gate.
    threshold_bodies = ([body] if body is not None else []) + _amendment_bodies(text)
    for directive_body in threshold_bodies:
        for line in _unfenced_lines(directive_body):
            threshold_m = _RELEASE_THRESHOLD_RE.match(line)
            if not threshold_m:
                continue
            present = True
            candidate = threshold_m.group(1).upper()
            if candidate not in {"P0", "P1", "P2"}:
                errors.append(
                    "Assurance Release threshold must be one of "
                    f"['P0', 'P1', 'P2'], got {threshold_m.group(1)!r}"
                )
                continue
            release_threshold = candidate
    if per_r or depth_per_r:
        present = True

    if known_rs is not None:
        for r in sorted(set(per_r) | set(depth_per_r), key=lambda x: int(x[1:])):
            if r not in known_rs:
                errors.append(
                    f"Assurance references {r}, which is not a "
                    f"requirement marker in this PRD"
                )

    # The map always carries the PRD's real requirement markers so the
    # rigor filter can reject nonexistent `prd:R<n>` evidence tokens.
    markers = {
        f"R{m.group(1)}"
        for m in (_R_MARKER_RE.match(line) for line in text.splitlines())
        if m
    }

    return AssuranceMap(
        present=present, default=default, per_r=per_r, rationale=rationale,
        depth_per_r=depth_per_r, release_threshold=release_threshold,
        known_rs=markers,
    ), errors
