"""Unit tests — mechanism 4 (design altitude) + coverage-map precompute."""
from __future__ import annotations

from autodev.artifacts.scope import Scope, ScopeItem, load_scope, write_scope
from autodev.assurance import max_depth, parse_assurance
from autodev.panel.coverage_map import (
    build_panel_coverage_map, coverage_table, depth_list,
    write_panel_coverage_map,
)
from autodev.state.hashing import hash_file
from autodev.panel.precheck import (
    _contract_section, _has_boundary_test, _sketch_nonempty,
)
from autodev.panel.rigor_filter import apply_rigor_filter
from autodev.artifacts.verdict import PanelFinding
from autodev.assurance import AssuranceMap

PRD4 = """# PRD: demo

## Requirements

### R1: Alpha
a

### R2: Beta
b

## Assurance

Default: loose

| Req | Rigor | Depth | Rationale |
|---|---|---|---|
| R1 | strict | upfront | core algorithm |
| R2 | core | defer | aux component |
"""


def test_depth_column_parses_and_defaults():
    m, errors = parse_assurance(PRD4)
    assert errors == []
    assert m.depth_for("R1") == "upfront"
    assert m.depth_for("R2") == "defer"
    assert m.depth_for("R9") == "auto"          # unspecified
    assert m.level_for("R1") == "strict"        # rigor still parses


def test_three_column_table_still_parses():
    prd = PRD4.replace(
        "| Req | Rigor | Depth | Rationale |\n|---|---|---|---|\n"
        "| R1 | strict | upfront | core algorithm |\n"
        "| R2 | core | defer | aux component |",
        "| Req | Rigor | Rationale |\n|---|---|---|\n"
        "| R1 | strict | core algorithm |")
    m, errors = parse_assurance(prd)
    assert errors == []
    assert m.level_for("R1") == "strict"
    assert m.depth_for("R1") == "auto"


def test_bad_depth_value_errors():
    prd = PRD4.replace("| R2 | core | defer |", "| R2 | core | never |")
    _, errors = parse_assurance(prd)
    assert any("depth" in e for e in errors)


def test_depth_amendment_override_latest_wins():
    prd = PRD4 + "\n## Amendment 2026-08-06\n\nDepth: R2 defer -> upfront\n"
    m, errors = parse_assurance(prd)
    assert errors == []
    assert m.depth_for("R2") == "upfront"


def test_max_depth_conservative():
    assert max_depth(["defer", "auto"]) == "auto"
    assert max_depth(["defer", "upfront", "auto"]) == "upfront"
    assert max_depth([]) == "auto"


def test_scope_design_depth_roundtrip(tmp_path):
    s = Scope(
        source="prd.md", source_hash="sha256:0", written="2026-08-06",
        feature="demo", mode="fresh", diff_base="main",
        in_scope=[
            ScopeItem(id="s-1", description="d", prd_ref=["R1"]),
            ScopeItem(id="s-2", description="d", prd_ref=["R2"],
                      design_depth="contract"),
        ],
    )
    p = tmp_path / "scope.json"
    write_scope(p, s)
    raw = p.read_text()
    assert '"design_depth": "contract"' in raw
    assert raw.count("design_depth") == 1     # default not emitted
    loaded = load_scope(p)
    assert loaded.in_scope[0].design_depth == "full"
    assert loaded.in_scope[1].design_depth == "contract"


DESIGN_MD = """# design

### Contract: s-2

- Interface: `run(x) -> y`
- Error semantics: returns None on bad input
- Sketch: single-pass dict lookup, no persistence.

### Other section
x
"""

TEST_PLAN = """
| Test ID | Scope ID | Description | Tier | Edges | Fixtures | Source |
|---|---|---|---|---|---|---|
| s-2.t1 | s-2 | boundary happy path | integration | - | - | Source: prd:R2 |
| s-1.t1 | s-1 | unit | unit | - | - | Source: prd:R1 |
"""


def test_contract_section_helpers():
    sec = _contract_section(DESIGN_MD, "s-2")
    assert sec is not None and "Error semantics" in sec
    assert _sketch_nonempty(sec)
    assert _contract_section(DESIGN_MD, "s-9") is None
    assert _has_boundary_test(TEST_PLAN, "s-2")
    assert not _has_boundary_test(TEST_PLAN, "s-1")   # unit tier only


def test_sketch_empty_detected():
    sec = "- Error semantics: x\n- Sketch:\n\n### next"
    assert not _sketch_nonempty(sec)


def test_underspecified_contract_blocks_on_loose():
    f = PanelFinding(
        severity="risk", vendor="claude", summary="contract mush",
        category="underspecified-contract", evidence_refs=["prd:R1"],
    )
    apply_rigor_filter(
        [f], AssuranceMap(present=True, default="loose"),
    )
    assert f.severity == "risk"                # blocks at every level


def _feature(tmp_path):
    base = tmp_path / "active"
    base.mkdir()
    (base / "prd.md").write_text(PRD4)
    (base / "scope.json").write_text("""{
      "source": "prd.md", "source_hash": "sha256:0",
      "written": "2026-08-06", "feature": "demo", "mode": "fresh",
      "diff_base": "main",
      "in_scope": [
        {"id": "s-1", "description": "d", "prd_ref": ["R1"], "status": "active"},
        {"id": "s-2", "description": "d", "prd_ref": ["R2"], "status": "active",
         "design_depth": "contract"}
      ], "excluded": []}""")
    (base / "trace.md").write_text("""
| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status | Source |
|---|---|---|---|---|---|---|---|
| 1 | s-1.r1 | s-1 | alpha works | s-1.t1 | -- | pending | Source: prd:R1 |
| 2 | s-2.r1 | s-2 | boundary ok | s-2.t1 | -- | pending | Source: prd:R2 |
""")
    return base


def test_depth_list_and_coverage_table(tmp_path):
    base = _feature(tmp_path)
    dl = depth_list(base)
    assert "s-2: contract" in dl and "s-1: full" in dl
    table = coverage_table(base)
    assert "| R1 | s-1 | s-1.r1 | s-1.t1 |" in table
    assert "| R2 | s-2 | s-2.r1 | s-2.t1 |" in table


def test_panel_coverage_map_is_materialized_with_hash_pinned_sources(tmp_path):
    base = _feature(tmp_path)
    payload = build_panel_coverage_map(base)
    assert payload is not None
    assert payload["kind"] == "panel-coverage-map"
    assert payload["design_depths"] == [
        {"scope_id": "s-1", "design_depth": "full"},
        {"scope_id": "s-2", "design_depth": "contract"},
    ]
    assert payload["coverage"][0] == {
        "requirement_id": "R1",
        "scope_items": ["s-1"],
        "trace_rows": ["s-1.r1"],
        "tests": ["s-1.t1"],
    }
    sources = {entry["path"]: entry["hash"] for entry in payload["sources"]}
    for name in ("prd.md", "scope.json", "trace.md"):
        path = base / name
        assert sources[str(path)] == hash_file(path)

    written = write_panel_coverage_map(base)
    assert written == base / "panel-coverage-map.json"
    assert written.exists()


def test_depth_list_none_without_contract_items(tmp_path):
    base = _feature(tmp_path)
    raw = (base / "scope.json").read_text().replace(
        ', "design_depth": "contract"', "").replace(
        ',\n         "design_depth": "contract"', "")
    (base / "scope.json").write_text(raw)
    assert depth_list(base) is None
