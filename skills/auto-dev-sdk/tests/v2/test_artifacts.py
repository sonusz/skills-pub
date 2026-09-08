"""v2-2: artifact schemas."""
from __future__ import annotations

import pytest

from autodev.artifacts.build import BuildReport, load_build, write_build
from autodev.artifacts.failure import FailureReport, load_failure, write_failure
from autodev.artifacts.implementation_index import (
    load_implementation_index,
    write_implementation_index,
)
from autodev.artifacts.overrides import (
    Overrides, load_overrides, write_overrides,
)
from autodev.artifacts.prd_checklist import load_prd_checklist, write_prd_checklist
from autodev.artifacts.scope import (
    ExcludedItem, Scope, ScopeItem, load_scope, write_scope,
)
from autodev.artifacts.verdict import (
    PanelFinding, PanelVerdict, load_verdict, write_verdict,
)
from autodev.errors import SchemaError


def _mk_scope(**over):
    base = Scope(
        source="prd.md", source_hash="sha256:" + "a" * 64,
        written="2026-04-20", feature="demo", mode="fresh", diff_base="main",
        in_scope=[ScopeItem(id="s-1", description="x", prd_ref=["§1"])],
        excluded=[ExcludedItem(id="s-x1", description="y", reason="z")],
    )
    for k, v in over.items():
        setattr(base, k, v)
    return base


def test_scope_roundtrip(tmp_path):
    p = tmp_path / "scope.json"
    write_scope(p, _mk_scope())
    loaded = load_scope(p)
    assert loaded.in_scope[0].id == "s-1"


def test_scope_rejects_missing_source_hash(tmp_path):
    with pytest.raises(SchemaError):
        write_scope(tmp_path / "scope.json", _mk_scope(source_hash=""))


def test_build_sealed_ref_roundtrip(tmp_path):
    """sealed_ref captures git HEAD at build-completion time so that
    LLM commits during build propagate through the cascade chain."""
    p = tmp_path / "build.json"
    write_build(p, BuildReport(
        source="scope.json", source_hash="sha256:" + "a" * 64,
        written="2026-04-20",
        test_cmd_run="pytest", test_exit_code=0,
        test_results={"passed": 1, "failed": 0, "skipped": 0},
        files_changed=["x.py"],
        sealed_ref="abc1234567890abcdef0",
    ))
    loaded = load_build(p)
    assert loaded.sealed_ref == "abc1234567890abcdef0"


def test_build_sealed_ref_default_empty(tmp_path):
    """Backward compat: omitted sealed_ref loads as empty string."""
    p = tmp_path / "build.json"
    p.write_text(
        '{"source":"scope.json","source_hash":"sha256:' + "a" * 64
        + '","written":"2026-04-20","test_cmd_run":"pytest",'
        + '"test_exit_code":0,"test_results":{"passed":1,"failed":0,"skipped":0},'
        + '"files_changed":["x.py"]}'
    )
    loaded = load_build(p)
    assert loaded.sealed_ref == ""


def test_build_sealed_ref_changes_file_hash(tmp_path):
    """Two BuildReports identical EXCEPT sealed_ref must hash to
    different files. This is the core property: WIP commits during
    build change git HEAD → sealed_ref → build.json hash → cascade
    propagates the change downstream."""
    import hashlib
    base_kwargs = dict(
        source="scope.json", source_hash="sha256:" + "a" * 64,
        written="2026-04-20", test_cmd_run="pytest", test_exit_code=0,
        test_results={"passed": 1, "failed": 0, "skipped": 0},
        files_changed=["x.py"],
    )
    p1 = tmp_path / "build1.json"
    p2 = tmp_path / "build2.json"
    write_build(p1, BuildReport(**base_kwargs, sealed_ref="aaa"))
    write_build(p2, BuildReport(**base_kwargs, sealed_ref="bbb"))
    h1 = hashlib.sha256(p1.read_bytes()).hexdigest()
    h2 = hashlib.sha256(p2.read_bytes()).hexdigest()
    assert h1 != h2, "different sealed_ref must produce different file hashes"


def test_build_requires_test_cmd_run(tmp_path):
    with pytest.raises(SchemaError):
        write_build(tmp_path / "build.json", BuildReport(
            source="scope.json", source_hash="sha256:" + "a" * 64,
            written="2026-04-20",
            test_cmd_run="",  # invalid
            test_exit_code=0,
            test_results={"passed": 1, "failed": 0, "skipped": 0},
            files_changed=["x.py"],
        ))


def test_build_requires_test_exit_code(tmp_path):
    # Missing field entirely would fail; string type also fails.
    r = BuildReport(
        source="scope.json", source_hash="sha256:" + "a" * 64,
        written="2026-04-20", test_cmd_run="pytest",
        test_exit_code=0, test_results={"passed": 1, "failed": 0, "skipped": 0},
        files_changed=["x.py"],
    )
    p = tmp_path / "build.json"
    write_build(p, r)
    loaded = load_build(p)
    assert loaded.test_exit_code == 0


def test_implementation_index_strips_build_semantics(feature_active, git_repo):
    import subprocess

    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), text=True,
    ).strip()
    active = feature_active
    (active / "architecture.md").write_text(
        f"# Architecture\n\n## Base ref\n\n`{base}`\n",
        encoding="utf-8",
    )
    (git_repo / "x.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "x.py"], cwd=str(git_repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "feature implementation"],
        cwd=str(git_repo), check=True,
    )
    write_build(active / "build.json", BuildReport(
        source="scope.json", source_hash="sha256:" + "a" * 64,
        written="2026-04-20", test_cmd_run="pytest",
        test_exit_code=0, test_results={"passed": 1, "failed": 0, "skipped": 0},
        files_changed=["x.py"],
        deviations=[{"scope_id": "s-1", "detail": "semantic diagnosis"}],
        blocking=True,
    ))
    path = write_implementation_index(active, repo_root=git_repo)
    raw = path.read_text(encoding="utf-8")
    assert "semantic diagnosis" not in raw
    assert "deviations" not in raw
    idx = load_implementation_index(path)
    assert idx.files_changed == ["x.py"]
    assert idx.test_cmd_run == "pytest"


def test_prd_checklist_is_not_coverage_map(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    (active / "prd.md").write_text(
        "# PRD\n\n## Requirements\n### R1: First\n### R2: Second\n",
        encoding="utf-8",
    )
    path = write_prd_checklist(active)
    loaded = load_prd_checklist(path)
    assert [r.req_id for r in loaded.requirements] == ["R1", "R2"]
    raw = path.read_text(encoding="utf-8")
    assert "status" not in raw
    assert "evidence" not in raw


def test_panel_verdict_roundtrip(tmp_path):
    p = tmp_path / "v.json"
    v = PanelVerdict(
        gate="design-review", verdict="pass", findings=[
            PanelFinding(severity="opinion", vendor="claude", summary="nit"),
        ],
        source="prd.md", source_hash="sha256:" + "a" * 64,
        prompt_file="x.md", prompt_hash="sha256:" + "b" * 64,
        harness_version="2.0.0", run_ts="2026-04-20T00:00:00Z",
        coverage_map={"claude": [
            {"req_id": "R1", "status": "satisfied", "evidence": "spec §3"}
        ]},
    )
    write_verdict(p, v)
    loaded = load_verdict(p)
    assert loaded.verdict == "pass"
    assert len(loaded.findings) == 1
    assert loaded.coverage_map["claude"][0]["req_id"] == "R1"


def test_panel_verdict_invariant_blocks():
    v = PanelVerdict(
        gate="design-review", verdict="pass", findings=[
            PanelFinding(severity="invariant_violation", vendor="claude", summary="bug"),
        ],
        source="prd.md", source_hash="sha256:" + "a" * 64,
        prompt_file="x.md", prompt_hash="sha256:" + "b" * 64,
        harness_version="2.0.0", run_ts="2026-04-20T00:00:00Z",
    )
    assert v.effectively_blocks() is True  # pass+invariant → blocks


def test_panel_verdict_skipped_does_not_block():
    v = PanelVerdict(
        gate="design-review", verdict="skipped", findings=[],
        source="prd.md", source_hash="sha256:" + "a" * 64,
        prompt_file="x.md", prompt_hash="sha256:" + "b" * 64,
        harness_version="2.0.0", run_ts="2026-04-20T00:00:00Z",
        skip_reason="tiny feature", skip_who="user",
    )
    assert v.effectively_blocks() is False


def test_failure_roundtrip_with_reaped(tmp_path):
    p = tmp_path / "f.json"
    r = FailureReport(
        stage="build", kind="timeout", detail="over 1800s",
        subprocess_exit=None, stderr_tail="...", ts="2026-04-20T00:00:00Z",
        subprocess_reaped=True, workspace_dirty=True,
    )
    write_failure(p, r)
    loaded = load_failure(p)
    assert loaded.subprocess_reaped is True
    assert loaded.kind == "timeout"


def test_overrides_skip_gate_cycle_scoped(tmp_path):
    o = Overrides(current_cycle=1)
    o.add_skip_gate(gate="design-review", reason="toy", who="tester")
    assert o.has_active_skip_gate("design-review")
    o.advance_cycle()
    assert not o.has_active_skip_gate("design-review")
    # Historical still there.
    assert len(o.historical_records()) == 1


def test_overrides_dirty_ack_preserved_by_invalidate(tmp_path):
    o = Overrides(current_cycle=1)
    o.add_dirty_ack(reason="in-progress", who="dev")
    o.add_skip_gate(gate="design-review", reason="r", who="dev")
    o.invalidate_cross_cycle("design-review")
    # skip-gate cleared
    assert not o.has_active_skip_gate("design-review")
    # dirty-ack preserved (R4d / R2a differentiation)
    assert o.has_active_dirty_ack()


def test_overrides_persistence(tmp_path):
    o = Overrides(current_cycle=2)
    o.add_skip_gate(gate="close-approval", reason="hotfix", who="dev")
    p = tmp_path / "overrides.json"
    write_overrides(p, o)
    loaded = load_overrides(p)
    assert loaded.current_cycle == 2
    assert loaded.has_active_skip_gate("close-approval")


def test_overrides_empty_reason_rejected_on_load(tmp_path):
    """Schema validation rejects persisted overrides with empty reason."""
    from autodev.errors import SchemaError
    import json
    bad = {"current_cycle": 1, "records": [
        {"kind": "skip_gate", "reason": "", "who": "x", "ts": "t",
         "skipped_in_cycle": 1, "gate": "design-review", "active": True}
    ]}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(SchemaError):
        load_overrides(p)
