"""Coding-stage E2E: orchestrator drives vendor subprocesses."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from autodev.artifacts.common import write_markdown_with_hash
from autodev.orchestrator import Orchestrator, OrchestratorConfig
from autodev.state.atomic import atomic_write_json
from autodev.state.hashing import hash_file
from autodev.vendors.config import (
    PanelConfig,
    PanelReviewerSpec,
    PanelSynthesizerSpec,
    ProbeConfig,
    STAGES,
    StageSpec,
    VendorsConfig,
)

FAKE_CLI = Path(__file__).resolve().parent / "fakes" / "fake_vendor_cli.sh"


def _vendors_for_test(tmp_path) -> VendorsConfig:
    return VendorsConfig(
        path=tmp_path / "vendors.yml",
        stages={
            s: StageSpec(stage=s, vendor="claude", model="fake-model", probe_interval_sec=30)
            for s in STAGES
        },
        panel=PanelConfig(
            reviewers=(
                PanelReviewerSpec(vendor="claude", model="fake-panel-claude"),
                PanelReviewerSpec(vendor="agy", model="fake-panel-agy"),
                PanelReviewerSpec(vendor="codex", model="fake-panel-codex"),
            ),
            synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake-panel-synth"),
        ),
        probe=ProbeConfig(vendor="claude", model="fake-probe"),
    )


def _prd(git_repo, feature="toy"):
    planned = git_repo / "docs" / "features" / feature / "active"
    planned.mkdir(parents=True)
    prd = planned / "prd.md"
    prd.write_text(
        "# PRD: toy\n## 1. Problem\n## 2. Users\n## 3. Requirements\n"
        "## 4. Constraints\n## 5. Success\n## 6. Out of scope\n"
    )
    prd_hash = hash_file(prd)
    # core R4: design's canonical upstream is arch-design.md, not prd.md
    # directly. Seed a passed initial design + arch-review so cascade
    # skips straight to the "design" stage these tests exercise.
    arch_design = planned / "arch-design.md"
    write_markdown_with_hash(
        arch_design,
        "## 1. Goal\ntoy\n## 5. PRD coverage\n| R1 | toy |\n",
        source=str(prd), source_hash=prd_hash,
    )
    atomic_write_json(planned / "arch-review.json", {
        "kind": "arch-review",
        "source": str(arch_design),
        "source_hash": hash_file(arch_design),
        "prd_hash": prd_hash,
        "written": "2026-04-21T00:00:00Z",
        "verdict": "pass",
        "findings": [],
    })
    # Commit so git status shows clean baseline
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init prd"], cwd=str(git_repo), check=True)
    return planned, prd, arch_design


def _bypass_panel_gate(feature_active, gate, *, git_repo=None):
    """Write a synthetic 'pass' panel verdict so orchestrator advances.

    Also commits the verdict so workspace stays clean (pipeline-produced
    files trigger dirty-workspace block; tests need to keep baseline clean).
    """
    from autodev.artifacts.verdict import PanelVerdict, write_verdict
    from autodev.state.hashing import hash_file
    from datetime import datetime, timezone

    # Determine upstream artifact for this gate
    if gate == "design-review":
        primary = feature_active / "design.md"
    else:  # close-approval
        primary = feature_active / "implemented-spec.md"

    if not primary.exists():
        return
    v = PanelVerdict(
        gate=gate, verdict="pass", findings=[],
        source=str(primary), source_hash=hash_file(primary),
        prompt_file="test", prompt_hash="sha256:" + "0" * 64,
        harness_version="test", run_ts=datetime.now(timezone.utc).isoformat(),
    )
    write_verdict(feature_active / f"panel-{gate}.json", v)
    # Commit to keep workspace clean
    if git_repo is not None:
        subprocess.run(["git", "add", "-A"], cwd=str(git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty",
                        "-m", f"test: bypass {gate}"],
                       cwd=str(git_repo), check=True)


def _fake_env(target_artifact, source_path, source_hash, feature="toy", behavior=None):
    return {
        "AUTODEV_VENDOR_BIN_CLAUDE": str(FAKE_CLI),
        "AUTODEV_FAKE_BEHAVIOR": behavior or "success_design",
        "AUTODEV_FAKE_TARGET_ARTIFACT": str(target_artifact),
        "AUTODEV_FAKE_SOURCE_PATH": str(source_path),
        "AUTODEV_FAKE_SOURCE_HASH": source_hash,
        "AUTODEV_FAKE_FEATURE": feature,
    }


def test_coding_design_stage_success(git_repo, monkeypatch):
    planned, prd, arch_design = _prd(git_repo)
    arch_design_hash = hash_file(arch_design)

    env = _fake_env(
        target_artifact=planned / "design.md",
        source_path=arch_design,
        source_hash=arch_design_hash,
        behavior="success_design",
    )
    env["AUTODEV_FAKE_TARGET_SCOPE"] = str(planned / "scope.json")
    env["AUTODEV_FAKE_TARGET_TRACE"] = str(planned / "trace.md")
    env["AUTODEV_FAKE_TARGET_TEST_PLAN"] = str(planned / "test-plan.md")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_for_test(git_repo),
        session_id="test",
    ))
    result = orch.advance_one("toy")
    assert result.success
    assert (planned / "design.md").exists()
    assert (planned / "scope.json").exists()
    assert (planned / "trace.md").exists()
    assert (planned / "test-plan.md").exists()
    scope = json.loads((planned / "scope.json").read_text())
    assert scope["feature"] == "toy"
    assert scope["in_scope"][0]["id"] == "t-1"


def test_coding_stage_exit_nonzero_produces_failure_json(git_repo, monkeypatch):
    planned, prd, arch_design = _prd(git_repo)
    from autodev.state.hashing import hash_file

    env = _fake_env(
        target_artifact=planned / "design.md",
        source_path=arch_design, source_hash=hash_file(arch_design),
        behavior="exit_nonzero",
    )
    env["AUTODEV_FAKE_TARGET_SCOPE"] = str(planned / "scope.json")
    env["AUTODEV_FAKE_TARGET_TRACE"] = str(planned / "trace.md")
    env["AUTODEV_FAKE_TARGET_TEST_PLAN"] = str(planned / "test-plan.md")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_for_test(git_repo), session_id="t",
    ))
    with pytest.raises(Exception) as exc:
        orch.advance_one("toy")
    assert (planned / "design-failure.json").exists()
    failure = json.loads((planned / "design-failure.json").read_text())
    assert failure["kind"] == "exit_nonzero"
    assert failure["subprocess_exit"] == 2


def test_coding_stage_malformed_provenance_md(git_repo, monkeypatch):
    """A markdown primary without parseable `<!-- source_hash: ... -->`
    header would otherwise loop forever (cascade always sees stale).
    Orchestrator must fail loud with provenance_malformed instead."""
    planned, prd, arch_design = _prd(git_repo)
    from autodev.state.hashing import hash_file

    env = _fake_env(
        target_artifact=planned / "design.md",
        source_path=arch_design, source_hash=hash_file(arch_design),
        behavior="malformed_provenance",
    )
    env["AUTODEV_FAKE_TARGET_SCOPE"] = str(planned / "scope.json")
    env["AUTODEV_FAKE_TARGET_TRACE"] = str(planned / "trace.md")
    env["AUTODEV_FAKE_TARGET_TEST_PLAN"] = str(planned / "test-plan.md")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_for_test(git_repo), session_id="t",
    ))
    with pytest.raises(Exception):
        orch.advance_one("toy")
    assert (planned / "design-failure.json").exists()
    f = json.loads((planned / "design-failure.json").read_text())
    assert f["kind"] == "malformed_artifact"
    assert "source_hash" in f["detail"]


def test_coding_stage_missing_artifact(git_repo, monkeypatch):
    planned, prd, arch_design = _prd(git_repo)
    from autodev.state.hashing import hash_file

    env = _fake_env(
        target_artifact=planned / "design.md",
        source_path=arch_design, source_hash=hash_file(arch_design),
        behavior="missing_artifact",
    )
    env["AUTODEV_FAKE_TARGET_SCOPE"] = str(planned / "scope.json")
    env["AUTODEV_FAKE_TARGET_TRACE"] = str(planned / "trace.md")
    env["AUTODEV_FAKE_TARGET_TEST_PLAN"] = str(planned / "test-plan.md")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_for_test(git_repo), session_id="t",
    ))
    with pytest.raises(Exception):
        orch.advance_one("toy")
    assert (planned / "design-failure.json").exists()
    f = json.loads((planned / "design-failure.json").read_text())
    assert f["kind"] == "missing_artifact"


def test_coding_stage_malformed_json_classified_as_malformed(git_repo, monkeypatch):
    """Schema validation on loaded artifact after rename → malformed_artifact."""
    planned, prd, arch_design = _prd(git_repo)
    from autodev.state.hashing import hash_file

    env = _fake_env(
        target_artifact=planned / "design.md",
        source_path=arch_design, source_hash=hash_file(arch_design),
        behavior="malformed_json",
    )
    env["AUTODEV_FAKE_TARGET_SCOPE"] = str(planned / "scope.json")
    env["AUTODEV_FAKE_TARGET_TRACE"] = str(planned / "trace.md")
    env["AUTODEV_FAKE_TARGET_TEST_PLAN"] = str(planned / "test-plan.md")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_for_test(git_repo), session_id="t",
    ))
    with pytest.raises(Exception):
        orch.advance_one("toy")  # may succeed from runner's POV
    assert (planned / "design-failure.json").exists()


def test_coding_stage_out_of_scope_write_detected(git_repo, monkeypatch, tmp_path):
    """G3: a subagent that writes outside feature-root is caught post-stage."""
    planned, prd, arch_design = _prd(git_repo)
    from autodev.state.hashing import hash_file

    escape_file = git_repo / "escape.txt"  # outside feature-root but inside repo

    env = _fake_env(
        target_artifact=planned / "design.md",
        source_path=arch_design, source_hash=hash_file(arch_design),
        behavior="out_of_scope_write",
    )
    env["AUTODEV_FAKE_TARGET_SCOPE"] = str(planned / "scope.json")
    env["AUTODEV_FAKE_TARGET_TRACE"] = str(planned / "trace.md")
    env["AUTODEV_FAKE_TARGET_TEST_PLAN"] = str(planned / "test-plan.md")
    env["AUTODEV_FAKE_ESCAPE_PATH"] = str(escape_file)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_for_test(git_repo), session_id="t",
    ))
    with pytest.raises(Exception):
        orch.advance_one("toy")
    fpath = planned / "design-failure.json"
    assert fpath.exists()
    failure = json.loads(fpath.read_text())
    assert failure["kind"] == "detected_out_of_scope_write"


def test_design_stage_rejects_write_to_prd_inside_feature_root(
    git_repo, monkeypatch,
):
    """The old feature-root allowlist silently allowed design to edit PRD."""
    planned, prd, arch_design = _prd(git_repo)
    from autodev.state.hashing import hash_file

    env = _fake_env(
        target_artifact=planned / "design.md",
        source_path=arch_design,
        source_hash=hash_file(arch_design),
        behavior="out_of_scope_write",
    )
    env["AUTODEV_FAKE_TARGET_SCOPE"] = str(planned / "scope.json")
    env["AUTODEV_FAKE_TARGET_TRACE"] = str(planned / "trace.md")
    env["AUTODEV_FAKE_TARGET_TEST_PLAN"] = str(planned / "test-plan.md")
    env["AUTODEV_FAKE_ESCAPE_PATH"] = str(prd)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_for_test(git_repo), session_id="t",
    ))
    with pytest.raises(Exception):
        orch.advance_one("toy")

    failure = json.loads((planned / "design-failure.json").read_text())
    assert failure["kind"] == "detected_out_of_scope_write"
    assert "prd.md" in failure["detail"]


def test_coding_stage_deficient_output_retries_then_succeeds(git_repo, monkeypatch):
    """A stage that exits 0 but omits a required artifact must NOT halt
    the run on the first slip. The harness re-dispatches the same agent
    with a <stage>-output-rejection.json + the prior artifacts, and the
    amended retry completes the deliverable."""
    planned, prd, arch_design = _prd(git_repo)
    from autodev.state.hashing import hash_file

    env = _fake_env(
        target_artifact=planned / "design.md",
        source_path=arch_design, source_hash=hash_file(arch_design),
        behavior="missing_then_success_design",
    )
    env["AUTODEV_FAKE_TARGET_SCOPE"] = str(planned / "scope.json")
    env["AUTODEV_FAKE_TARGET_TRACE"] = str(planned / "trace.md")
    env["AUTODEV_FAKE_TARGET_TEST_PLAN"] = str(planned / "test-plan.md")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_for_test(git_repo), session_id="t",
    ))
    result = orch.advance_one("toy")

    assert result.success
    # Two attempts: the first omitted scope/trace/test-plan, the second
    # (amend pass) produced the complete set.
    assert int((planned / "scratch" / ".fake.design.attempt").read_text()) == 2
    assert (planned / "design.md").exists()
    assert (planned / "scope.json").exists()
    assert (planned / "trace.md").exists()
    assert (planned / "test-plan.md").exists()
    # Rejection note is cleared once the stage succeeds.
    assert not (planned / "design-output-rejection.json").exists()
