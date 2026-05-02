"""v3-core R5: per-gate pre-checks for unified design-review + close."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.artifacts.design_packet import write_design_packet
from autodev.panel.precheck import (
    precheck_close_approval, precheck_design_review, run_precheck,
)
from autodev.state.hashing import hash_file


@pytest.fixture
def active(tmp_path):
    a = tmp_path / "active"
    a.mkdir()
    return a


def _write_design_packet(active: Path):
    (active / "docs").mkdir(parents=True, exist_ok=True)
    (active / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n",
        encoding="utf-8",
    )
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    prd = active / "prd.md"
    prd.write_text(
        "# PRD\n\n## 1. Problem\nfoo\n\n## 2. Users\nbar\n\n"
        "## 3. Requirements\n### R1: thing one\n### R2: thing two\n"
        "## 4. Constraints\n\n## 5. Success\n\n## 6. Out of scope\n"
    )
    prd_h = hash_file(prd)
    (active / "design.md").write_text(
        "# Design\n\n## Flow\n- Handle R1.\n- Handle R2.\n\n"
        "Validation commands: [\"pytest -q\"]\n"
    )
    scope = {
        "source": str(prd), "source_hash": prd_h,
        "written": "2026-04-20", "feature": "demo",
        "mode": "fresh", "diff_base": "main",
        "in_scope": [
            {"id": "s-1", "description": "do R1", "prd_ref": ["R1"],
             "design_ref": ["R1"], "status": "active"},
            {"id": "s-2", "description": "do R2", "prd_ref": ["R2"],
             "design_ref": ["R2"], "status": "active"},
        ],
    }
    (active / "scope.json").write_text(json.dumps(scope))
    (active / "trace.md").write_text(
        "# trace\n\n"
        "| # | Req ID | Scope ID | Requirement | Source |\n"
        "|---|--------|----------|-------------|--------|\n"
        "| 1 | s-1.r1 | s-1 | do R1 | Source: prd:R1 |\n"
        "| 2 | s-2.r1 | s-2 | do R2 | Source: prd:R2 |\n"
    )
    (active / "test-plan.md").write_text(
        "# test-plan\n\n"
        "| Scope ID | Description | Source |\n"
        "|----------|-------------|--------|\n"
        "| s-1 | t1 | Source: scope:s-1 |\n"
        "| s-2 | t2 | Source: scope:s-2 |\n"
    )
    write_design_packet(active)
    return prd, active / "design.md", active / "scope.json", prd_h


def test_design_review_precheck_happy_path(active):
    _write_design_packet(active)
    r = precheck_design_review(active)
    assert r.ok, r.message


def test_design_review_precheck_missing_design(active):
    prd = active / "prd.md"
    prd.write_text("# PRD\n")
    r = precheck_design_review(active)
    assert not r.ok
    assert "design.md" in r.message


def test_design_review_precheck_duplicate_ids(active):
    _, _, scope_p, _ = _write_design_packet(active)
    raw = json.loads(scope_p.read_text())
    raw["in_scope"].append(
        {"id": "s-1", "description": "dup", "prd_ref": ["R1"],
         "design_ref": ["R1"], "status": "active"}
    )
    scope_p.write_text(json.dumps(raw))
    write_design_packet(active)
    r = precheck_design_review(active)
    assert not r.ok
    assert "duplicate" in r.message


def test_design_review_precheck_empty_prd_ref(active):
    _, _, scope_p, _ = _write_design_packet(active)
    raw = json.loads(scope_p.read_text())
    raw["in_scope"][0]["prd_ref"] = []
    scope_p.write_text(json.dumps(raw))
    write_design_packet(active)
    r = precheck_design_review(active)
    assert not r.ok
    assert "empty prd_ref" in r.message


def test_design_review_precheck_unresolvable_design_ref(active):
    _, _, scope_p, _ = _write_design_packet(active)
    raw = json.loads(scope_p.read_text())
    raw["in_scope"][0]["design_ref"] = ["R999"]
    scope_p.write_text(json.dumps(raw))
    write_design_packet(active)
    r = precheck_design_review(active)
    assert not r.ok
    assert "design_ref token" in r.message


def test_design_review_precheck_source_hash_mismatch(active):
    _, _, scope_p, _ = _write_design_packet(active)
    raw = json.loads(scope_p.read_text())
    raw["source_hash"] = "sha256:" + "b" * 64
    scope_p.write_text(json.dumps(raw))
    write_design_packet(active)
    r = precheck_design_review(active)
    assert not r.ok
    assert "source_hash" in r.message


def test_design_review_precheck_inactive_item_skipped(active):
    _, _, scope_p, _ = _write_design_packet(active)
    raw = json.loads(scope_p.read_text())
    raw["in_scope"][0]["status"] = "removed"
    raw["in_scope"][0]["prd_ref"] = []
    raw["in_scope"][0]["design_ref"] = []
    scope_p.write_text(json.dumps(raw))
    write_design_packet(active)
    r = precheck_design_review(active)
    assert r.ok, r.message


def test_design_review_precheck_missing_trace_row(active):
    _write_design_packet(active)
    (active / "trace.md").write_text(
        "# trace\n\n"
        "| # | Req ID | Scope ID | Requirement | Source |\n"
        "|---|--------|----------|-------------|--------|\n"
        "| 1 | s-1.r1 | s-1 | do R1 | Source: prd:R1 |\n"
    )
    write_design_packet(active)
    r = precheck_design_review(active)
    assert not r.ok
    assert "s-2" in r.message and "trace.md" in r.message


def test_design_review_precheck_missing_test_case(active):
    _write_design_packet(active)
    (active / "test-plan.md").write_text(
        "# test-plan\n\n"
        "| Scope ID | Description | Source |\n"
        "|----------|-------------|--------|\n"
        "| s-1 | t1 | Source: scope:s-1 |\n"
    )
    write_design_packet(active)
    r = precheck_design_review(active)
    assert not r.ok
    assert "s-2" in r.message and "test-plan.md" in r.message


def test_design_review_precheck_scope_invalid_json(active):
    (active / "docs").mkdir(parents=True, exist_ok=True)
    (active / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n",
        encoding="utf-8",
    )
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    (active / "prd.md").write_text("# PRD\n")
    (active / "design.md").write_text("# Design\n\nValidation commands: [\"pytest -q\"]\n")
    (active / "scope.json").write_text("{ not json")
    (active / "trace.md").write_text("# trace\n")
    (active / "test-plan.md").write_text("# test-plan\n")
    write_design_packet(active)
    r = precheck_design_review(active)
    assert not r.ok
    assert "invalid JSON" in r.message


def _seed_for_close(active: Path):
    _write_design_packet(active)
    (active / "implemented-spec.md").write_text(
        "# spec\n\n"
        "## 1. Purpose\nx\n\n## 2. Users\nx\n\n## 3. Contract\nx\n\n"
        "## 4. Data model\nx\n\n## 5. Architecture\nx\n\n"
        "## 6. Out of scope\nx\n\n## 7. Testing\nx\n\n"
        "## 8. Operational notes\nx\n"
    )
    (active / "prd-checklist.json").write_text(json.dumps({
        "source": "prd.md",
        "source_hash": "sha256:" + "0" * 64,
        "written": "2026-04-22",
        "kind": "prd-checklist",
        "requirements": [
            {"req_id": "R1", "title": "x", "line": 1},
            {"req_id": "R2", "title": "y", "line": 2},
        ],
    }))


def test_close_precheck_happy(active):
    _seed_for_close(active)
    r = precheck_close_approval(active)
    assert r.ok, r.message


def test_close_precheck_missing_spec(active):
    _write_design_packet(active)
    (active / "prd-checklist.json").write_text(
        '{"kind":"prd-checklist","requirements":[]}'
    )
    r = precheck_close_approval(active)
    assert not r.ok
    assert "implemented-spec.md" in r.message


def test_close_precheck_missing_checklist(active):
    _write_design_packet(active)
    (active / "implemented-spec.md").write_text("x")
    r = precheck_close_approval(active)
    assert not r.ok
    assert "prd-checklist.json" in r.message


def test_close_precheck_rejects_coverage_fields(active):
    _seed_for_close(active)
    (active / "prd-checklist.json").write_text(json.dumps({
        "kind": "prd-checklist",
        "requirements": [
            {"req_id": "R1", "title": "x", "line": 1, "status": "covered"},
            {"req_id": "R2", "title": "y", "line": 2},
        ]
    }))
    r = precheck_close_approval(active)
    assert not r.ok
    assert "coverage fields" in r.message


def test_run_precheck_dispatch(active):
    _write_design_packet(active)
    assert run_precheck("design-review", active, []).ok
    assert not run_precheck("made-up", active, []).ok
