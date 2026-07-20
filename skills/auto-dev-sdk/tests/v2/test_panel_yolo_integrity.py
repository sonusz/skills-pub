"""Panel reviewer yolo + write-integrity guard.

Reviewers run with the sandbox bypassed (yolo) so they can read cross-repo
material and reach the network; the write-integrity guard backstops any stray
mutation of the artifacts under review.
"""
from __future__ import annotations

import subprocess

import pytest

from autodev.errors import GatePending
from autodev.panel import integrity, run_panel_gate
from autodev.panel import runner
from autodev.panel.runner import _compose_reviewer_prompt, _invoke_reviewer
from autodev.vendors.config import PanelReviewerSpec


def _init_git_repo(path):
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=path, check=True)


# --------------------------------------------------------------------------
# Prompt: no inlining; reviewers self-read.

def test_reviewer_prompt_self_read_no_inlining(tmp_path):
    art = tmp_path / "design-packet.json"
    art.write_text("{}", encoding="utf-8")
    prompt = _compose_reviewer_prompt(
        gate="design-review", artifact_path=art, consulted_docs=[],
    )
    assert "Read them yourself" in prompt
    assert "size_bytes" in prompt
    # old inlining contract must be gone
    assert "inlined sections" not in prompt
    assert "attached context" not in prompt


# --------------------------------------------------------------------------
# Reviewer runs yolo, with no read-only / context-file args.

def test_reviewer_invoked_with_yolo_no_readonly_no_contextfiles(monkeypatch, tmp_path):
    captured: dict = {}

    class _Result:
        returncode = 0
        timed_out = False
        output = "## Review\nVerdict: pass"
        log = ""
        summary_stderr = ""
        status = {"exit_code": "0"}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return _Result()

    monkeypatch.delenv(runner.FAKE_INVOKER_ENV, raising=False)
    monkeypatch.setattr(runner, "call_shared_vendor", fake_call)

    res = _invoke_reviewer(
        PanelReviewerSpec(vendor="claude", model="fake"), "prompt", 10,
        cwd=tmp_path,
    )
    assert res.ok
    assert captured.get("yolo") is True
    assert "native_args" not in captured   # read-only hints removed
    assert "context_files" not in captured  # inlining removed


# --------------------------------------------------------------------------
# Integrity guard.

def test_guard_detects_canonical_modification_git(tmp_path):
    _init_git_repo(tmp_path)
    art = tmp_path / "design.md"
    art.write_text("v1", encoding="utf-8")
    state = integrity.snapshot_before(tmp_path, "design-review", [art])
    assert state.git_ref is not None
    assert integrity.detect_after(state, [art]) == []        # clean
    art.write_text("v2-mutated", encoding="utf-8")
    changes = integrity.detect_after(state, [art])
    assert any("modified during review" in c for c in changes)


def test_guard_clean_pass_discards_ref(tmp_path):
    _init_git_repo(tmp_path)
    art = tmp_path / "design.md"
    art.write_text("v1", encoding="utf-8")
    state = integrity.snapshot_before(tmp_path, "design-review", [art])
    assert subprocess.run(
        ["git", "show-ref", "--verify", state.git_ref], cwd=tmp_path
    ).returncode == 0
    assert integrity.detect_after(state, [art]) == []
    integrity.discard(state)
    assert subprocess.run(
        ["git", "show-ref", "--verify", state.git_ref],
        cwd=tmp_path, capture_output=True,
    ).returncode != 0


def test_guard_works_without_git(tmp_path):
    art = tmp_path / "design.md"
    art.write_text("v1", encoding="utf-8")
    state = integrity.snapshot_before(tmp_path, "design-review", [art])
    assert state.git_ref is None  # not a git repo
    assert integrity.detect_after(state, [art]) == []
    art.write_text("v2", encoding="utf-8")
    assert any("modified" in c for c in integrity.detect_after(state, [art]))


def test_guard_detects_tracked_source_modification(tmp_path):
    _init_git_repo(tmp_path)
    src = tmp_path / "src.py"
    src.write_text("orig", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    art = tmp_path / "design.md"
    art.write_text("d", encoding="utf-8")
    state = integrity.snapshot_before(tmp_path, "close-approval", [art])
    src.write_text("MUTATED-BY-REVIEWER", encoding="utf-8")  # tracked source edit
    changes = integrity.detect_after(state, [art])
    assert any("src.py" in c and "tracked file modified" in c for c in changes)


def test_guard_ignores_tracked_outputs_owned_by_current_panel(tmp_path):
    _init_git_repo(tmp_path)
    active = tmp_path / "docs" / "features" / "x" / "active"
    active.mkdir(parents=True)
    art = active / "design-packet.json"
    art.write_text("{}", encoding="utf-8")
    outputs = [
        active / "panel-design-review.json",
        active / "panel-design-review.reviewers.json",
        active / "panel-trace-review.json",
        active / "panel-trace-review.reviewers.json",
    ]
    for output in outputs:
        output.write_text('{"run":"old"}', encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "initial panel"], cwd=tmp_path, check=True,
    )

    state = integrity.snapshot_before(tmp_path, "design-review", [art])
    for output in outputs:
        output.write_text('{"run":"new"}', encoding="utf-8")

    assert integrity.detect_after(state, [art]) == []


def test_report_includes_git_rollback_command(tmp_path):
    _init_git_repo(tmp_path)
    art = tmp_path / "design.md"
    art.write_text("v1", encoding="utf-8")
    state = integrity.snapshot_before(tmp_path, "design-review", [art])
    report = integrity.format_report(state, [f"{art} (modified during review)"])
    assert "git" in report and "restore" in report
    assert state.git_ref in report
    assert "NOT auto-reverted" in report


# --------------------------------------------------------------------------
# Wrapper: mutation during the round -> discard verdict + pause, no revert.

def test_panel_wrapper_pauses_on_surface_mutation(tmp_path, monkeypatch):
    repo = tmp_path
    _init_git_repo(repo)
    fa = repo / "docs" / "features" / "x" / "active"
    fa.mkdir(parents=True)
    packet = fa / "design-packet.json"
    packet.write_text(
        '{"artifacts": [], "input": {}, "context_refs": []}', encoding="utf-8"
    )
    # leave a stale verdict to prove it gets invalidated
    (fa / "panel-design-review.json").write_text("{}", encoding="utf-8")

    def fake_internal(**kwargs):
        packet.write_text('{"MUTATED": true}', encoding="utf-8")  # reviewer mutates
        return "verdict"

    monkeypatch.setattr("autodev.panel.run_panel_gate_internal", fake_internal)
    monkeypatch.delenv("AUTODEV_PANEL_REVIEW_BIN", raising=False)

    with pytest.raises(GatePending):
        run_panel_gate(
            gate="design-review", feature_active=fa, repo_root=repo,
            feature="x", primary_artifact=packet, panel_config=object(),
        )

    assert (fa / ".pause").exists()
    assert "restore" in (fa / ".pause").read_text(encoding="utf-8")
    assert not (fa / "panel-design-review.json").exists()  # round invalidated
