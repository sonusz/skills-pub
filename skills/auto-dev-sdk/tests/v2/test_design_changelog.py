"""Tests for design-changelog.json flowing through the design packet."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.artifacts.design_packet import build_design_packet
from autodev.state.atomic import atomic_write_json
from autodev.state.hashing import hash_file


@pytest.fixture
def active(tmp_path):
    a = tmp_path / "active"
    a.mkdir()
    (a / "docs").mkdir()
    (a / "docs" / "architecture-proposal.md").write_text(
        "# arch\n", encoding="utf-8",
    )
    (a / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    prd = a / "prd.md"
    prd.write_text(
        "# PRD\n\n## 1. Problem\nfoo\n\n## 2. Users\nbar\n\n"
        "## 3. Requirements\n### R1: thing one\n"
        "## 4. Constraints\n\n## 5. Success\n\n## 6. Out of scope\n",
        encoding="utf-8",
    )
    prd_h = hash_file(prd)
    (a / "design.md").write_text(
        f"<!-- source: {prd} -->\n"
        f"<!-- source_hash: {prd_h} -->\n"
        f"<!-- written: 2026-05-19 -->\n"
        f"# Design\n\n## 1. Context\n\n## 2. Primitives & commitments\n"
        f"Handle R1.\n\nValidation commands: [\"pytest -q\"]\n",
        encoding="utf-8",
    )
    (a / "scope.json").write_text(json.dumps({
        "source": str(prd), "source_hash": prd_h,
        "written": "2026-05-19", "feature": "demo",
        "mode": "fresh", "diff_base": "main",
        "in_scope": [{
            "id": "s-1", "description": "do R1",
            "prd_ref": ["R1"], "design_ref": ["1. Context"],
            "status": "active",
        }],
    }), encoding="utf-8")
    (a / "trace.md").write_text(
        f"<!-- source: {prd} -->\n"
        f"<!-- source_hash: {prd_h} -->\n"
        f"<!-- written: 2026-05-19 -->\n"
        f"# trace\n\n| # | Req ID | Scope ID | Source |\n"
        f"|---|---|---|---|\n"
        f"| 1 | s-1.r1 | s-1 | Source: prd:R1 |\n",
        encoding="utf-8",
    )
    (a / "test-plan.md").write_text(
        f"<!-- source: {prd} -->\n"
        f"<!-- source_hash: {prd_h} -->\n"
        f"<!-- written: 2026-05-19 -->\n"
        f"# test-plan\n\n## Test Strategy\n\n## Test Cases\n\n"
        f"| Scope ID | Source |\n|---|---|\n| s-1 | Source: scope:s-1 |\n"
        f"\n## Coverage Summary\n",
        encoding="utf-8",
    )
    return a


def test_changelog_absent_yields_empty_response_to_feedback(active):
    packet = build_design_packet(active)
    assert packet["response_to_feedback"] == []


def test_changelog_present_referenced_in_packet(active):
    changelog_path = active / "design-changelog.json"
    atomic_write_json(changelog_path, {
        "kind": "design-changelog",
        "schema_version": 1,
        "entries": [
            {"round": 1, "trigger": "initial", "reason": "first pass",
             "artifacts_changed": ["design.md"],
             "added": [], "removed": []},
        ],
    })
    packet = build_design_packet(active)
    assert packet["response_to_feedback"] == [
        {"path": str(changelog_path), "hash": hash_file(changelog_path)},
    ]


def test_changelog_hash_reflects_appended_entries(active):
    """Appending to changelog changes its hash; packet picks up new hash."""
    changelog_path = active / "design-changelog.json"
    atomic_write_json(changelog_path, {
        "kind": "design-changelog",
        "schema_version": 1,
        "entries": [
            {"round": 1, "trigger": "initial", "reason": "first pass",
             "artifacts_changed": ["design.md"],
             "added": [], "removed": []},
        ],
    })
    first_packet = build_design_packet(active)
    first_hash = first_packet["response_to_feedback"][0]["hash"]

    # Append round 2
    atomic_write_json(changelog_path, {
        "kind": "design-changelog",
        "schema_version": 1,
        "entries": [
            {"round": 1, "trigger": "initial", "reason": "first pass",
             "artifacts_changed": ["design.md"],
             "added": [], "removed": []},
            {"round": 2, "trigger": "design-review", "reason": "panel flagged R1",
             "artifacts_changed": ["scope.json"],
             "added": [{"artifact": "scope.json", "anchor": "s-1a"}],
             "removed": []},
        ],
    })
    second_packet = build_design_packet(active)
    second_hash = second_packet["response_to_feedback"][0]["hash"]

    assert first_hash != second_hash
    assert second_hash == hash_file(changelog_path)
