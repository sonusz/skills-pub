"""Unified design-stage cascade end-to-end staleness propagation."""
from __future__ import annotations

import json
from pathlib import Path

from autodev.artifacts.common import write_markdown_with_hash
from autodev.artifacts.design_packet import write_accepted_design, write_design_packet
from autodev.artifacts.implementation_index import write_implementation_index
from autodev.artifacts.prd_checklist import write_prd_checklist
from autodev.artifacts.scope import Scope, ScopeItem, write_scope
from autodev.artifacts.verdict import PanelVerdict, write_verdict
from autodev.paths import find_repo_root
from autodev.state.atomic import atomic_write, atomic_write_json
from autodev.state.cascade import ARTIFACTS, StalenessCascade
from autodev.state.hashing import hash_file


def _seed_all_ten(active: Path) -> None:
    """Seed every cascade artifact with matching upstream hashes."""
    repo_root = find_repo_root(active)
    (repo_root / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n",
        encoding="utf-8",
    )
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    # prd
    prd = active / "prd.md"
    atomic_write(prd, "# PRD\n## 1. Problem\n## 2. Users\n## 3. Requirements\n## 4. Constraints\n## 5. Success\n## 6. Out of scope\n")
    prd_h = hash_file(prd)

    write_markdown_with_hash(
        active / "design.md",
        "## 2. Primitives & commitments\nValidation commands: [\"pytest -q\"]\n\nbody\n",
        source=str(prd),
        source_hash=prd_h,
    )

    # scope.json
    scope = Scope(
        source=str(prd), source_hash=prd_h, written="2026-04-20",
        feature="demo", mode="fresh", diff_base="main",
        in_scope=[ScopeItem(id="s-1", description="x", prd_ref=["§1"], design_ref=["§1"])],
    )
    write_scope(active / "scope.json", scope)
    scope_h = hash_file(active / "scope.json")

    # trace.md, test-plan.md
    write_markdown_with_hash(active / "trace.md", "body\n",
                             source=str(prd), source_hash=prd_h)
    write_markdown_with_hash(active / "test-plan.md", "body\n",
                             source=str(prd), source_hash=prd_h)

    # design-packet.json
    packet = write_design_packet(active)
    packet_h = hash_file(packet)
    design_h = hash_file(active / "design.md")

    # panel-design-review.json
    v_design = PanelVerdict(
        gate="design-review", verdict="pass", findings=[],
        source=str(packet), source_hash=packet_h,
        prompt_file="x", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-20T00:00:00Z",
        consulted_docs=[
            {"path": str(active / "design.md"), "hash": design_h, "priority": "consulted"},
            {"path": str(active / "scope.json"), "hash": scope_h, "priority": "consulted"},
            {"path": str(active / "trace.md"), "hash": hash_file(active / "trace.md"), "priority": "consulted"},
            {"path": str(active / "test-plan.md"), "hash": hash_file(active / "test-plan.md"), "priority": "consulted"},
            {"path": str(active / "prd.md"), "hash": prd_h, "priority": "consulted"},
        ],
    )
    write_verdict(active / "panel-design-review.json", v_design)
    write_accepted_design(active)

    # build.json
    build_data = {
        "source": str(active / "scope.json"),
        "source_hash": scope_h,
        "written": "2026-04-20",
        "test_cmd_run": "pytest",
        "test_exit_code": 0,
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "files_changed": ["x.py"],
        "lint": {"passed": True},
        "deviations": [],
        "blocking": False,
        "workspace_dirty_at_stage_end": False,
    }
    atomic_write_json(active / "build.json", build_data)

    # implementation-index.json, implemented-spec.md, prd-checklist.json
    index_path = write_implementation_index(active, repo_root=active)
    index_h = hash_file(index_path)
    write_markdown_with_hash(active / "implemented-spec.md", "body\n",
                             source=str(index_path), source_hash=index_h)
    checklist_path = write_prd_checklist(active)

    # panel-close-approval.json
    v_close = PanelVerdict(
        gate="close-approval", verdict="pass", findings=[],
        source=str(active / "implemented-spec.md"),
        source_hash=hash_file(active / "implemented-spec.md"),
        prompt_file="x", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-20T00:00:00Z",
        consulted_docs=[
            {"path": str(active / "prd.md"), "hash": prd_h, "priority": "consulted"},
            {"path": str(checklist_path), "hash": hash_file(checklist_path), "priority": "consulted"},
        ],
    )
    write_verdict(active / "panel-close-approval.json", v_close)


def test_cascade_full_chain_all_fresh(feature_active):
    _seed_all_ten(feature_active)
    c = StalenessCascade(feature_active)
    fresh = c.fresh()
    # All artifacts fresh
    assert all(fresh.values()), f"not fresh: {[k for k,v in fresh.items() if not v]}"
    assert c.next_stage() == "done"


def test_cascade_prd_change_invalidates_full_chain(feature_active):
    _seed_all_ten(feature_active)
    # Mutate PRD → every downstream becomes stale
    (feature_active / "prd.md").write_text("# PRD v2\n## 1. Problem\n## 2. Users\n## 3. Requirements\n## 4. Constraints\n## 5. Success\n## 6. Out of scope\n")
    c = StalenessCascade(feature_active)
    fresh = c.fresh()
    # prd itself is fresh (root), but every downstream stale
    assert fresh["prd"] is True
    expected_stale = {
        "design", "scope", "trace", "test_plan", "design_packet",
        "panel_design_review", "accepted_design", "build",
        "implementation_index", "spec", "prd_checklist",
        "panel_close_approval",
    }
    for name in expected_stale:
        assert fresh[name] is False, f"{name} should be stale after PRD mutation"
    assert c.next_stage() == "design"


def test_cascade_scope_mutation_invalidates_downstream_only(feature_active):
    _seed_all_ten(feature_active)
    # Mutate scope.json. The design docs stay mechanically fresh because
    # they are source-pinned to PRD, but the harness-authored packet and
    # every artifact depending on accepted design go stale.
    scope_path = feature_active / "scope.json"
    import json as _json
    data = _json.loads(scope_path.read_text())
    data["in_scope"].append({
        "id": "s-2", "description": "new", "prd_ref": ["§2"], "status": "active"
    })
    scope_path.write_text(_json.dumps(data, indent=2) + "\n")

    c = StalenessCascade(feature_active)
    fresh = c.fresh()
    assert fresh["prd"] is True
    # scope itself: check depends on whether scope's recorded source_hash
    # matches PRD hash. PRD unchanged, scope.json's source_hash field
    # still matches PRD → cascade says scope fresh. Downstream stale
    # because THEY record scope's old hash.
    assert fresh["scope"] is True
    # The raw design bundle is source-pinned to PRD, so trace/test-plan remain
    # mechanically fresh. design-packet catches the changed scope hash and
    # invalidates the review, acceptance marker, build, and downstream gates.
    for name in ("design_packet", "panel_design_review", "accepted_design",
                 "build", "implementation_index", "spec",
                 "panel_close_approval"):
        assert fresh[name] is False, f"{name} should be stale"
    assert c.next_stage() == "design_packet"


def test_cascade_missing_build_blocks_spec(feature_active):
    _seed_all_ten(feature_active)
    (feature_active / "build.json").unlink()
    c = StalenessCascade(feature_active)
    fresh = c.fresh()
    assert fresh["build"] is False
    assert fresh["implementation_index"] is False
    assert fresh["spec"] is False
    assert c.next_stage() == "build"
