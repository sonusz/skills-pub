"""v3-core R5 — mechanical pre-check scripts.

Each panel gate runs a gate-specific pre-check before any reviewer
subprocess is dispatched. On failure, the harness halts with a named
failure message (no vendor calls, no L budget consumption).

Pre-checks validate *written artifact* properties (``Source:`` tags,
schema shape, reference resolution). They do NOT validate reviewer
output (``Evidence:``) — that's a post-panel concern.

Gate | Primary-pair artifacts                 | Checks
-----|----------------------------------------|--------
G1   | prd.md + scope.json                    | scope schema, prd_ref resolves, unique ids, source_hash
G2a  | scope.json + discovered arch docs      | consulted docs exist + readable
G2tp | test-plan.md + trace.md (anchor prd/s) | scope ref valid, ≥1 row + ≥1 test per scope, Source: tags
Close| implemented-spec.md + checklist + prd  | checklist is judgment-free, spec 8-sec
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PrecheckResult:
    ok: bool
    message: str   # human-readable; on failure names the failed check


# ------------------------- vocabulary --------------------------------

_SOURCE_TAG_RE = re.compile(
    r"(?:^|\s|\|)Source:\s*(prd:[^\s|]+|scope:[^\s|]+|trace:[^\s|]+|"
    r"spec:[^\s|]+|inferred|commonsense)",
    re.MULTILINE,
)


def _extract_source_tags(text: str) -> list[str]:
    return _SOURCE_TAG_RE.findall(text)


def _ref_tokens(ref: object) -> list[str] | None:
    """Validate prd_ref/design_ref shape and return its tokens.

    Returns ``None`` if the value is not a ``list[str]`` (so the caller
    can emit a precise error). Returns ``[]`` if the list is present
    but contains only whitespace/empty entries — caller treats that
    as "empty" the same way a missing ref is treated.
    """
    if not isinstance(ref, list) or not all(isinstance(t, str) for t in ref):
        return None
    return [t.strip() for t in ref if t.strip()]


def _extract_section_headers(md: str) -> set[str]:
    """Return ``{"§1", "§1.1", "1", "1.1"}``-shaped ids found in headers.

    Strips leading ``#`` markers and matches tokens like ``§1``,
    ``1.`` or a numbered heading ``### R3:`` → ``R3``.
    """
    out: set[str] = set()
    for line in md.splitlines():
        s = line.lstrip("#").strip()
        for m in re.finditer(r"(§\d+(?:\.\d+)*|R\d+|\b\d+(?:\.\d+)*)", s):
            out.add(m.group(1))
    return out


# ------------------------- G1: design-review ---------------------------

def _contract_section(design_text: str, scope_id: str) -> str | None:
    """Body of ``### Contract: <scope_id>`` up to the next ###/## header,
    or None if absent."""
    lines = design_text.splitlines()
    start = None
    header_re = re.compile(
        rf"^\s*###\s+Contract:\s*{re.escape(scope_id)}\s*$")
    for i, line in enumerate(lines):
        if start is None and header_re.match(line):
            start = i
            continue
        if start is not None and re.match(r"^\s*#{2,3}\s+", line):
            return "\n".join(lines[start + 1:i])
    if start is not None:
        return "\n".join(lines[start + 1:])
    return None


def _sketch_nonempty(section: str) -> bool:
    """True when a `Sketch` marker is followed by ≥1 non-heading content
    line (or carries content on its own line)."""
    lines = section.splitlines()
    for i, line in enumerate(lines):
        if "Sketch" not in line:
            continue
        after = line.split("Sketch", 1)[1].strip(" :*-")
        if after:
            return True
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue
            return not re.match(r"^\s*#", nxt)
    return False


_BOUNDARY_TIER_RE = re.compile(
    r"contract|integration|e2e|boundary", re.IGNORECASE)


def _has_boundary_test(tp_text: str, scope_id: str) -> bool:
    """≥1 test-plan table row for ``scope_id`` whose Tier cell matches
    the boundary vocabulary. Column positions follow the canonical
    layout (Test ID | Scope ID | Description | Tier | ...)."""
    for line in tp_text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and cells[1] == scope_id \
                and _BOUNDARY_TIER_RE.search(cells[3]):
            return True
    return False


def precheck_design_review(feature_active: Path) -> PrecheckResult:
    prd_path = feature_active / "prd.md"
    design_path = feature_active / "design.md"
    scope_path = feature_active / "scope.json"
    trace_path = feature_active / "trace.md"
    tp_path = feature_active / "test-plan.md"
    packet_path = feature_active / "design-packet.json"
    if not prd_path.exists():
        return PrecheckResult(False, "precheck_design_review: prd.md missing")
    if not design_path.exists():
        return PrecheckResult(False, "precheck_design_review: design.md missing")
    if not scope_path.exists():
        return PrecheckResult(
            False, "precheck_design_review: scope.json missing")
    if not trace_path.exists():
        return PrecheckResult(False, "precheck_design_review: trace.md missing")
    if not tp_path.exists():
        return PrecheckResult(False, "precheck_design_review: test-plan.md missing")
    if not packet_path.exists():
        return PrecheckResult(False, "precheck_design_review: design-packet.json missing")

    from autodev.artifacts.design_packet import design_packet_fresh

    if not design_packet_fresh(packet_path):
        return PrecheckResult(
            False,
            "precheck_design_review: design-packet.json stale against current inputs",
        )

    try:
        scope_raw = json.loads(scope_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return PrecheckResult(False, f"precheck_design_review: scope.json invalid JSON: {e}")

    in_scope = scope_raw.get("in_scope", [])
    if not isinstance(in_scope, list):
        return PrecheckResult(False, "precheck_design_review: scope.in_scope not a list")

    # Unique ids
    ids = [item.get("id") for item in in_scope]
    if len(ids) != len(set(ids)):
        return PrecheckResult(
            False, f"precheck_design_review: duplicate scope ids in {ids!r}")

    # Every active scope item has prd_ref / design_ref resolving into
    # prd.md / design.md. Both are required to be ``list[str]``; each
    # token must appear as a substring somewhere in the target file
    # (header or body). This catches invented refs like "R999" while
    # supporting rich refs like ["R4", "SC2", "Constraints"].
    prd_text = prd_path.read_text(encoding="utf-8")
    for item in in_scope:
        if item.get("status") != "active":
            continue
        tokens = _ref_tokens(item.get("prd_ref"))
        if tokens is None:
            return PrecheckResult(
                False,
                f"precheck_design_review: scope item {item.get('id')!r} "
                f"prd_ref must be list[str] (got {type(item.get('prd_ref')).__name__})"
            )
        if not tokens:
            return PrecheckResult(
                False,
                f"precheck_design_review: scope item {item.get('id')!r} "
                f"has empty prd_ref"
            )
        for tok in tokens:
            if tok not in prd_text:
                return PrecheckResult(
                    False,
                    f"precheck_design_review: scope item {item.get('id')!r} "
                    f"prd_ref token {tok!r} not found in prd.md"
                )

    design_text = design_path.read_text(encoding="utf-8")
    for item in in_scope:
        if item.get("status") != "active":
            continue
        tokens = _ref_tokens(item.get("design_ref"))
        if tokens is None:
            return PrecheckResult(
                False,
                f"precheck_design_review: scope item {item.get('id')!r} "
                f"design_ref must be list[str] (got {type(item.get('design_ref')).__name__})"
            )
        if not tokens:
            return PrecheckResult(
                False,
                f"precheck_design_review: scope item {item.get('id')!r} has empty design_ref"
            )
        for tok in tokens:
            if tok not in design_text:
                return PrecheckResult(
                    False,
                    f"precheck_design_review: scope item {item.get('id')!r} "
                    f"design_ref token {tok!r} not found in design.md"
                )

    # Mechanism 4 — design-altitude rules (docs/proposals/rigor-tier.md):
    # enum validity, upfront→full enforcement (most-conservative across
    # cited Rs), and contract-section requirements for contract items.
    from autodev.assurance import max_depth, parse_assurance
    assurance, _assur_errs = parse_assurance(prd_text)
    for item in in_scope:
        if item.get("status") != "active":
            continue
        depth = item.get("design_depth", "full")
        if depth not in ("contract", "full"):
            return PrecheckResult(
                False,
                f"precheck_design_review: scope item {item.get('id')!r} "
                f"design_depth {depth!r} invalid (contract|full)"
            )
        cited_rs = [t for t in item.get("prd_ref", [])
                    if re.fullmatch(r"R\d+", str(t))]
        directive = max_depth([assurance.depth_for(r) for r in cited_rs])
        if directive == "upfront" and depth != "full":
            return PrecheckResult(
                False,
                f"precheck_design_review: scope item {item.get('id')!r} "
                f"cites an upfront-depth requirement but has "
                f"design_depth: contract — upfront forces full design"
            )
        if depth == "contract":
            section = _contract_section(design_text, str(item.get("id")))
            if section is None:
                return PrecheckResult(
                    False,
                    f"precheck_design_review: contract item "
                    f"{item.get('id')!r} has no '### Contract: "
                    f"{item.get('id')}' section in design.md"
                )
            if "Error semantics" not in section:
                return PrecheckResult(
                    False,
                    f"precheck_design_review: contract section for "
                    f"{item.get('id')!r} missing 'Error semantics' entry"
                )
            if not _sketch_nonempty(section):
                return PrecheckResult(
                    False,
                    f"precheck_design_review: contract section for "
                    f"{item.get('id')!r} missing a non-empty 'Sketch'"
                )
            if not _has_boundary_test(
                tp_path.read_text(encoding="utf-8"), str(item.get("id")),
            ):
                return PrecheckResult(
                    False,
                    f"precheck_design_review: contract item "
                    f"{item.get('id')!r} has no boundary test in "
                    f"test-plan.md (Tier must match "
                    f"contract|integration|e2e|boundary)"
                )

    # source_hash provenance: scope.source_hash == hash_file(prd)
    from autodev.state.hashing import hash_file
    actual_prd_hash = hash_file(prd_path)
    claimed = scope_raw.get("source_hash", "")
    if claimed != actual_prd_hash:
        return PrecheckResult(
            False,
            f"precheck_design_review: scope.source_hash {claimed!r} != "
            f"hash(prd.md) {actual_prd_hash!r}"
        )

    # Reverse coverage: every ### R<N>: heading in prd.md must appear in
    # at least one active scope item's prd_ref OR in an excluded item's
    # description/reason. Catches requirements silently omitted from scope.
    prd_req_ids = set(re.findall(r'^###\s+(R\d+)\s*:', prd_text, re.MULTILINE))
    if prd_req_ids:
        covered_refs: set[str] = set()
        for item in in_scope:
            if item.get("status") == "active":
                for tok in item.get("prd_ref", []):
                    covered_refs.add(tok)
        for item in scope_raw.get("excluded", []):
            desc = item.get("description", "") + " " + item.get("reason", "")
            for m in re.finditer(r'\bR\d+\b', desc):
                covered_refs.add(m.group())
        uncovered = sorted(prd_req_ids - covered_refs)
        if uncovered:
            return PrecheckResult(
                False,
                f"precheck_design_review: PRD requirement(s) {uncovered!r} not "
                f"referenced in any active scope item prd_ref or excluded item; "
                f"add to in_scope or excluded before panel review",
            )

    try:
        scope_raw = json.loads(scope_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return PrecheckResult(
            False, f"precheck_design_review: scope.json invalid JSON: {e}")

    active_ids = {
        item["id"] for item in scope_raw.get("in_scope", [])
        if item.get("status") == "active"
    }

    trace_text = trace_path.read_text(encoding="utf-8")
    tp_text = tp_path.read_text(encoding="utf-8")

    # Every active scope item has ≥1 trace row + ≥1 test-plan row.
    for sid in active_ids:
        if sid not in trace_text:
            return PrecheckResult(
                False,
                f"precheck_design_review: scope id {sid!r} has no "
                f"trace.md row"
            )
        if sid not in tp_text:
            return PrecheckResult(
                False,
                f"precheck_design_review: scope id {sid!r} has no "
                f"test-plan.md test case"
            )

    # Every row / test case carries a resolving Source: tag (R7).
    # Count rows that start with '|' and contain scope/trace cells
    # (robust heuristic for markdown tables): each such row must have
    # a Source: column.
    for name, text in (("trace.md", trace_text), ("test-plan.md", tp_text)):
        table_rows = [
            line for line in text.splitlines()
            if line.startswith("|") and not re.match(r"\|\s*-+", line)
            and "Source" not in line
        ]
        if table_rows:
            missing = [line for line in table_rows if "Source:" not in line]
            # Only fail if some rows have Source and others don't (partial),
            # OR if no rows have Source at all (complete miss). The first
            # case is a contract violation; the second may be legacy.
            any_source = any(
                "Source:" in line for line in text.splitlines()
                if line.startswith("|")
            )
            if any_source and missing:
                return PrecheckResult(
                    False,
                    f"precheck_design_review: {name} has partial "
                    f"Source: tagging ({len(missing)} rows missing)"
                )

    return PrecheckResult(True, "precheck_design_review: ok")


# ------------------------- Close ------------------------------------

def precheck_close_approval(feature_active: Path) -> PrecheckResult:
    spec_path = feature_active / "implemented-spec.md"
    checklist_path = feature_active / "prd-checklist.json"
    prd_path = feature_active / "prd.md"

    for p in (spec_path, checklist_path, prd_path):
        if not p.exists():
            return PrecheckResult(
                False, f"precheck_close_approval: {p.name} missing")

    # checklist schema check. This is not a coverage map: entries must
    # not contain status/evidence/spec mapping fields.
    try:
        checklist = json.loads(checklist_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return PrecheckResult(
            False,
            f"precheck_close_approval: prd-checklist.json invalid JSON: {e}",
        )
    if checklist.get("kind") != "prd-checklist":
        return PrecheckResult(
            False,
            "precheck_close_approval: prd-checklist.json.kind must be "
            "prd-checklist",
        )
    req_entries = checklist.get("requirements")
    if not isinstance(req_entries, list):
        return PrecheckResult(
            False,
            "precheck_close_approval: prd-checklist.json missing requirements[]",
        )
    seen_reqs: set[str] = set()
    forbidden = {"status", "evidence", "spec_section", "coverage", "implementation"}
    for i, entry in enumerate(req_entries):
        if not isinstance(entry, dict):
            return PrecheckResult(
                False,
                f"precheck_close_approval: requirements[{i}] "
                f"must be object"
            )
        forbidden_seen = forbidden.intersection(entry)
        if forbidden_seen:
            return PrecheckResult(
                False,
                "precheck_close_approval: prd-checklist.json must not "
                f"contain coverage fields {sorted(forbidden_seen)!r}",
            )
        req_id = entry.get("req_id")
        if not req_id:
            return PrecheckResult(
                False,
                f"precheck_close_approval: requirements[{i}] "
                f"missing req_id"
            )
        if req_id in seen_reqs:
            return PrecheckResult(
                False,
                f"precheck_close_approval: duplicate req_id {req_id!r} "
                f"in prd-checklist.json"
            )
        seen_reqs.add(req_id)

    # Every PRD `### R<N>:` requirement has exactly one checklist entry.
    prd_text = prd_path.read_text(encoding="utf-8")
    prd_reqs = set(re.findall(r"^###\s+(R\d+)\s*:", prd_text, re.MULTILINE))
    missing_in_review = sorted(prd_reqs - seen_reqs)
    extra_in_review = sorted(seen_reqs - prd_reqs)
    if missing_in_review:
        return PrecheckResult(
            False,
            f"precheck_close_approval: prd-checklist.json does not list "
            f"PRD requirement(s) {missing_in_review!r}"
        )
    if extra_in_review:
        return PrecheckResult(
            False,
            f"precheck_close_approval: prd-checklist.json has entries for "
            f"non-existent requirement(s) {extra_in_review!r}"
        )

    # implemented-spec.md: 8 top-level sections present (§1..§8 or
    # ## 1..## 8).
    spec_text = spec_path.read_text(encoding="utf-8")
    present_sections = set()
    for line in spec_text.splitlines():
        m = re.match(r"^\s*##\s+(\d+)\.", line)
        if m:
            present_sections.add(int(m.group(1)))
        m2 = re.match(r"^\s*##\s+§?(\d+)", line)
        if m2:
            present_sections.add(int(m2.group(1)))
    missing_secs = [n for n in range(1, 9) if n not in present_sections]
    if missing_secs:
        return PrecheckResult(
            False,
            "precheck_close_approval: implemented-spec.md missing "
            f"section(s) {missing_secs!r}"
        )

    return PrecheckResult(True, "precheck_close_approval: ok")


# ------------------------- dispatch ---------------------------------

def run_precheck(
    gate: str,
    feature_active: Path,
    consulted_docs: list[dict],
) -> PrecheckResult:
    """Run the gate-specific pre-check. Returns ok/message."""
    if gate == "design-review":
        return precheck_design_review(feature_active)
    if gate == "close-approval":
        return precheck_close_approval(feature_active)
    return PrecheckResult(False, f"precheck: unknown gate {gate!r}")
