"""Harness-precomputed coverage map + design-depth artifact for panel
reviewers (rigor-tier proposal, Phase E + mechanism 4).

Bookkeeping out of panels: the R → scope-items → trace-rows → tests
mapping is deterministic string matching. Precomputing it saves each
reviewer from re-deriving (and mis-copying) it, and concentrates their
context on judgment: is the mapping *adequate*, not what it *is*.

The depth list is the trace-review panel's only legal channel for
`design_depth` — that panel is context-isolated from scope.json by
prompt contract, but altitude-aware coverage judgment ("contract items
are judged on boundary rows only") requires knowing which items are
contract-level.

All derivation is tolerant: a missing/unparseable input yields None
and the reviewer falls back to self-extraction. The depth list is the
exception in spirit — precheck validates scope.json before any panel
runs, so in practice it is always derivable when contract items exist.
"""
from __future__ import annotations

import json
import re
from typing import Any
from pathlib import Path

from autodev.state.atomic import atomic_write_json
from autodev.state.hashing import hash_file

_R_MARKER_RE = re.compile(r"^\s*###\s+(R\d+)\s*:", re.MULTILINE)
_R_TOKEN = re.compile(r"^R\d+$")
PANEL_COVERAGE_MAP_FILENAME = "panel-coverage-map.json"


def _table_rows(md_text: str) -> list[list[str]]:
    rows = []
    for line in md_text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
            rows.append(cells)
    return rows


def depth_entries(feature_active: Path) -> list[dict[str, str]] | None:
    """Structured active-item depths, or None when scope is unavailable.

    Preserve the old prompt behavior: when every active item is ``full`` the
    depth channel is unnecessary, so return an empty list.
    """
    scope_path = Path(feature_active) / "scope.json"
    try:
        raw = json.loads(scope_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    items = [i for i in raw.get("in_scope", [])
             if isinstance(i, dict) and i.get("status") == "active"]
    if not any(i.get("design_depth") == "contract" for i in items):
        return []
    return [
        {
            "scope_id": str(i.get("id")),
            "design_depth": str(i.get("design_depth", "full")),
        }
        for i in items
    ]


def depth_list(feature_active: Path) -> str | None:
    """Backward-compatible markdown view used by focused unit tests."""
    entries = depth_entries(feature_active)
    if not entries:
        return None
    lines = [
        f"- {entry['scope_id']}: {entry['design_depth']}"
        for entry in entries
    ]
    return "\n".join(lines)


def coverage_entries(feature_active: Path) -> list[dict[str, Any]] | None:
    """Structured R → scope → trace → tests mapping."""
    base = Path(feature_active)
    try:
        prd_text = (base / "prd.md").read_text(encoding="utf-8")
        scope_raw = json.loads((base / "scope.json").read_text(encoding="utf-8"))
        trace_text = (base / "trace.md").read_text(encoding="utf-8")
    except Exception:
        return None
    rs = sorted(set(_R_MARKER_RE.findall(prd_text)), key=lambda r: int(r[1:]))
    if not rs:
        return None
    items_by_r: dict[str, list[str]] = {r: [] for r in rs}
    for item in scope_raw.get("in_scope", []):
        if not isinstance(item, dict) or item.get("status") != "active":
            continue
        for tok in item.get("prd_ref", []):
            if isinstance(tok, str) and _R_TOKEN.match(tok) and tok in items_by_r:
                items_by_r[tok].append(str(item.get("id")))
    # trace.md canonical columns:
    # # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status | Source
    trace_by_item: dict[str, list[str]] = {}
    tests_by_item: dict[str, set[str]] = {}
    for cells in _table_rows(trace_text):
        if len(cells) < 5 or cells[1].lower() in ("req id", "req_id"):
            continue
        req_id, scope_id, tests = cells[1], cells[2], cells[4]
        trace_by_item.setdefault(scope_id, []).append(req_id)
        for t in re.split(r"[,\s]+", tests):
            if t and t != "--":
                tests_by_item.setdefault(scope_id, set()).add(t)
    out: list[dict[str, Any]] = []
    for r in rs:
        items = items_by_r[r]
        rows: list[str] = []
        tests: set[str] = set()
        for sid in items:
            rows.extend(trace_by_item.get(sid, []))
            tests.update(tests_by_item.get(sid, set()))
        out.append({
            "requirement_id": r,
            "scope_items": items,
            "trace_rows": sorted(rows),
            "tests": sorted(tests),
        })
    return out


def coverage_table(feature_active: Path) -> str | None:
    """Backward-compatible markdown view used by focused unit tests."""
    entries = coverage_entries(feature_active)
    if entries is None:
        return None
    out = [
        "| R | scope items | trace rows | tests |",
        "|---|---|---|---|",
    ]
    for entry in entries:
        out.append(
            f"| {entry['requirement_id']} "
            f"| {', '.join(entry['scope_items']) or '—'} "
            f"| {', '.join(entry['trace_rows']) or '—'} "
            f"| {', '.join(entry['tests']) or '—'} |"
        )
    return "\n".join(out)


def build_panel_coverage_map(feature_active: Path) -> dict[str, Any] | None:
    """Build the deterministic artifact consumed by both design panels."""
    base = Path(feature_active)
    depths = depth_entries(base)
    coverage = coverage_entries(base)
    if depths is None or coverage is None:
        return None

    source_paths = [base / "prd.md", base / "scope.json", base / "trace.md"]
    if not all(path.exists() for path in source_paths):
        return None
    return {
        "kind": "panel-coverage-map",
        "schema_version": 1,
        "sources": [
            {"path": str(path), "hash": hash_file(path)}
            for path in source_paths
        ],
        "design_depths": depths,
        "coverage": coverage,
    }


def write_panel_coverage_map(feature_active: Path) -> Path | None:
    """Materialize coverage/depth data and return its path.

    The panel receives this artifact through the normal consulted-docs
    path/hash/size manifest. No derived coverage rows are inlined in prompts.
    """
    base = Path(feature_active)
    path = base / PANEL_COVERAGE_MAP_FILENAME
    payload = build_panel_coverage_map(base)
    if payload is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None
    atomic_write_json(path, payload)
    return path
