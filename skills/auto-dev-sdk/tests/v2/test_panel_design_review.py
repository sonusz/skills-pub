"""Unified design-review panel gate behavior."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.artifacts.common import write_markdown_with_hash
from autodev.artifacts.design_packet import write_design_packet
from autodev.artifacts.verdict import load_verdict
from autodev.paths import find_repo_root
from autodev.panel import prompt_path, run_panel_gate
from autodev.panel.runner import FAKE_INVOKER_ENV
from autodev.state.cascade import ARTIFACTS, StalenessCascade
from autodev.state.hashing import hash_file
from autodev.vendors.config import (
    PanelConfig, PanelReviewerSpec, PanelSynthesizerSpec,
)

FAKE_SCRIPT = Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"


@pytest.fixture
def fake_invoker(monkeypatch):
    assert FAKE_SCRIPT.exists()
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(FAKE_SCRIPT))
    yield


@pytest.fixture
def panel_config():
    return PanelConfig(
        reviewers=(
            PanelReviewerSpec(vendor="claude", model="fake"),
            PanelReviewerSpec(vendor="gemini", model="fake"),
            PanelReviewerSpec(vendor="codex", model="fake"),
        ),
        synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake"),
        reviewer_timeout_sec=10,
        synthesizer_timeout_sec=10,
    )


def _seed(feature_active: Path, *, packet: bool = False) -> Path:
    feature_active.mkdir(parents=True, exist_ok=True)
    repo_root = find_repo_root(feature_active)
    (repo_root / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n",
        encoding="utf-8",
    )
    (feature_active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    prd = feature_active / "prd.md"
    prd.write_text(
        "# PRD: demo\n## 3. Requirements\n### R1: the thing must hash.\n",
        encoding="utf-8",
    )
    prd_hash = hash_file(prd)

    design = feature_active / "design.md"
    write_markdown_with_hash(
        design,
        "# Design\n\n## 2. Primitives & commitments\n"
        "Validation commands: [\"pytest -q\"]\n\n"
        "## Flow\nHandle R1.\n",
        source=str(prd),
        source_hash=prd_hash,
    )

    scope = feature_active / "scope.json"
    scope.write_text(
        json.dumps({
            "source": "prd.md",
            "source_hash": prd_hash,
            "written": "2026-04-20",
            "feature": "demo",
            "mode": "fresh",
            "diff_base": "main",
            "in_scope": [
                {"id": "d-1", "description": "hash a file",
                 "prd_ref": ["R1"], "design_ref": ["R1"], "status": "active"},
            ],
            "excluded": [],
        }) + "\n",
        encoding="utf-8",
    )

    write_markdown_with_hash(
        feature_active / "trace.md",
        "# trace\n\n"
        "| # | Req ID | Scope ID | Requirement | Source |\n"
        "| - | ------ | -------- | ----------- | ------ |\n"
        "| 1 | d-1.r1 | d-1 | hash file | Source: prd:R1 |\n",
        source=str(prd), source_hash=prd_hash,
    )
    write_markdown_with_hash(
        feature_active / "test-plan.md",
        "# test-plan\n\n"
        "| Scope ID | Description | Source |\n"
        "|----------|-------------|--------|\n"
        "| d-1 | hash empty file | Source: scope:d-1 |\n",
        source=str(prd), source_hash=prd_hash,
    )
    if packet:
        return write_design_packet(feature_active)
    return design


def test_prompt_path_resolves_for_design_review():
    p = prompt_path("design-review")
    assert p.exists()
    assert "design-review" in p.name


def test_cascade_has_design_review_between_design_and_build():
    names = [a.name for a in ARTIFACTS]
    i_design = names.index("design")
    i_packet = names.index("design_packet")
    i_gate = names.index("panel_design_review")
    i_accept = names.index("accepted_design")
    i_build = names.index("build")
    assert i_design < i_packet < i_gate < i_accept < i_build, f"order wrong: {names}"
    gate = next(a for a in ARTIFACTS if a.name == "panel_design_review")
    assert gate.upstream == ("design_packet", "prd")


def test_cascade_next_stage_is_design_packet_after_design_artifacts(tmp_path):
    _seed(tmp_path)
    cascade = StalenessCascade(tmp_path)
    assert cascade.next_stage() == "design_packet"


def test_cascade_next_stage_is_design_review_after_design_packet(tmp_path):
    _seed(tmp_path, packet=True)
    cascade = StalenessCascade(tmp_path)
    assert cascade.next_stage() == "panel_design_review"


def test_design_review_passes(
    git_repo, feature_active, fake_invoker, panel_config, monkeypatch
):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    packet = _seed(feature_active, packet=True)
    v = run_panel_gate(
        gate="design-review",
        feature_active=feature_active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=packet,
        panel_config=panel_config,
    )
    assert v.gate == "design-review"
    assert v.verdict == "pass"
    out = feature_active / "panel-design-review.json"
    assert out.exists()
    loaded = load_verdict(out)
    assert loaded.gate == "design-review"


def test_design_review_records_consulted_docs(
    git_repo, feature_active, fake_invoker, panel_config, monkeypatch
):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    packet = _seed(feature_active, packet=True)
    run_panel_gate(
        gate="design-review",
        feature_active=feature_active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=packet,
        panel_config=panel_config,
    )
    docs_path = feature_active / "panel-design-review.docs.json"
    assert docs_path.exists()
    docs = json.loads(docs_path.read_text())
    names = sorted(Path(d["path"]).name for d in docs)
    assert names == [
        "architecture-proposal.md",
        "design.md",
        "prd.md",
        "scope.json",
        "test-plan.md",
        "trace.md",
    ]
    for d in docs:
        assert d["hash"].startswith("sha256:")


def test_design_review_blocks_on_fail(
    git_repo, feature_active, fake_invoker, panel_config, monkeypatch
):
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_two_fail")
    packet = _seed(feature_active, packet=True)
    v = run_panel_gate(
        gate="design-review",
        feature_active=feature_active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=packet,
        panel_config=panel_config,
    )
    assert v.verdict == "needs_revision"
    assert v.effectively_blocks()
    assert v.has_invariant_violation()


def test_verdict_validator_accepts_design_review_gate(tmp_path):
    from autodev.artifacts.verdict import PanelVerdict, write_verdict
    from datetime import datetime, timezone
    design = tmp_path / "design.md"
    design.write_text("# design\n")
    v = PanelVerdict(
        gate="design-review", verdict="pass", findings=[],
        source=str(design), source_hash=hash_file(design),
        prompt_file="x", prompt_hash="sha256:" + "0" * 64,
        harness_version="test", run_ts=datetime.now(timezone.utc).isoformat(),
    )
    out = tmp_path / "panel-design-review.json"
    write_verdict(out, v)
    assert out.exists()
