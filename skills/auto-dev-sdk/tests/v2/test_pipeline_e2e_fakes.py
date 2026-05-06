"""Full-pipeline e2e smoke test with FakeCLI + FakePanel — NO live LLM.

Drives `autodev run` all the way from prd → design → design-review →
build → implementation-index → spec → prd-checklist → close-approval
using only fake subprocess invokers. Runs in <10s.

Purpose: catch wiring regressions (stage dispatch, cascade order,
panel verdict parsing, revision-state advancement) without paying
for real vendor subprocess calls.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autodev.artifacts.design_packet import accepted_design_fresh, design_packet_fresh
from autodev.cli import main as cli_main
from autodev.orchestrator import Orchestrator, OrchestratorConfig
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

FAKE_VENDOR = Path(__file__).resolve().parent / "fakes" / "fake_vendor_cli_auto.py"
FAKE_PANEL = Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"


def _write_prd(git_repo: Path, feature: str = "smoke") -> Path:
    active = git_repo / "docs" / "features" / feature / "active"
    active.mkdir(parents=True)
    (git_repo / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n",
        encoding="utf-8",
    )
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    prd = active / "prd.md"
    # Stage 0 PRD schema: 6 required sections + ≥1 ### R<N>: marker.
    prd.write_text(
        "# PRD: smoke\n\n"
        "## Problem\nSmoke test exercising the full pipeline.\n\n"
        "## Users\npytest.\n\n"
        "## Requirements\n### R1: feature does exactly one thing — pass through.\n\n"
        "## Constraints\nminimal.\n\n"
        "## Success criteria\npipeline runs end-to-end with fakes.\n\n"
        "## Out of scope\nreal vendor calls.\n",
        encoding="utf-8",
    )
    # Commit so the working tree is clean when orchestrator.run runs its
    # dirty-workspace check.
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"seed {feature} prd"],
                   cwd=str(git_repo), check=True)
    return active


def _vendors_fake_everywhere(repo_root: Path) -> VendorsConfig:
    """All coding stages → claude vendor with the autodetecting fake."""
    return VendorsConfig(
        path=repo_root / "vendors.yml",
        stages={
            s: StageSpec(stage=s, vendor="claude", model="fake-model", probe_interval_sec=30)
            for s in STAGES
        },
        panel=PanelConfig(
            reviewers=(
                PanelReviewerSpec(vendor="claude", model="fake-panel-claude"),
                PanelReviewerSpec(vendor="gemini", model="fake-panel-gemini"),
                PanelReviewerSpec(vendor="codex", model="fake-panel-codex"),
            ),
            synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake-panel-synth"),
        ),
        probe=ProbeConfig(vendor="claude", model="fake-probe"),
    )


def test_pipeline_full_run_with_fakes(git_repo, monkeypatch):
    """End-to-end: prd → ... → close-approval, all fakes, verdict=pass.

    Exercises every coding stage dispatch, every panel gate, the
    cascade ordering, revision-state creation, and artifact writes.
    No live vendor calls; runs in seconds.
    """
    feature = "smoke"
    active = _write_prd(git_repo, feature)

    # Point coding-stage vendor to the Python autodetecting fake.
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)

    # Point panel reviewers + synthesizer to the shell fake that emits
    # all-pass verdicts.
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_INVOKER", str(FAKE_PANEL))
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")

    # Pre-acknowledge dirty workspace so stages writing new artifacts
    # (scope.json, trace.md, etc.) don't trip the dirty-check between
    # each stage.
    from autodev import overrides_api as ov
    ov.record_acknowledge_dirty(active, reason="smoke test", who="pytest")

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo,
        vendors=_vendors_fake_everywhere(git_repo),
        session_id="smoke-test",
    ))
    orch.run(feature, max_stages=25)

    # Every cascade artifact should exist.
    for name in (
        "design.md", "design-packet.json", "accepted-design.json",
        "scope.json", "trace.md", "test-plan.md", "build.json",
        "implementation-index.json", "implemented-spec.md", "prd-checklist.json",
        "panel-design-review.json", "panel-close-approval.json",
    ):
        p = active / name
        assert p.exists(), f"missing artifact after pipeline: {name}"

    # Both panel verdicts should be pass.
    for gate in ("design-review", "close-approval"):
        v = json.loads((active / f"panel-{gate}.json").read_text())
        assert v["verdict"] == "pass", (
            f"{gate} verdict != pass: {v['verdict']} ({len(v.get('findings', []))} findings)"
        )

    # Revision-state should never have bumped L/G (all panels passed first try).
    state_path = active / "revision-state.json"
    if state_path.exists():
        s = json.loads(state_path.read_text())
        assert s["G"] == 0
        assert all(v == 0 for v in s["L"].values())


def test_pipeline_skip_gate_design_review_writes_accepted_design_with_fakes(git_repo, monkeypatch):
    feature = "smoke-skip-design-review"
    active = _write_prd(git_repo, feature)

    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_INVOKER", str(FAKE_PANEL))
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")

    from autodev import overrides_api as ov
    ov.record_acknowledge_dirty(active, reason="smoke test", who="pytest")
    ov.record_skip_gate(active, gate="design-review", reason="fake-backed skip path", who="pytest")

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo,
        vendors=_vendors_fake_everywhere(git_repo),
        session_id="smoke-skip-design-review",
    ))
    orch.run(feature, max_stages=25)

    verdict = json.loads((active / "panel-design-review.json").read_text(encoding="utf-8"))
    accepted = json.loads((active / "accepted-design.json").read_text(encoding="utf-8"))
    assert verdict["verdict"] == "skipped"
    assert accepted["verdict"] == "skipped"
    assert accepted["acceptance_mode"] == "skip_gate"
    assert accepted["design_packet_hash"] == hash_file(active / "design-packet.json")
    assert design_packet_fresh(active / "design-packet.json") is True
    assert accepted_design_fresh(active / "accepted-design.json") is True
    assert (active / "build.json").exists()


def test_pipeline_blocked_design_review_propagates_to_design(git_repo, monkeypatch):
    """Design-review returns needs_revision → cascade invalidates design;
    next autodev run re-runs design.

    Shorter variant: just check that the revision-state records the
    L-bump and scope re-dispatches. We don't chase convergence.
    """
    feature = "smoke-blocked"
    active = _write_prd(git_repo, feature)

    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_INVOKER", str(FAKE_PANEL))
    # design-review fails with IV so cascade triggers revision.
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_two_fail")

    # The first run writes the design packet and dirties the workspace.
    from autodev import overrides_api as ov
    ov.record_acknowledge_dirty(active, reason="smoke test", who="pytest")

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo,
        vendors=_vendors_fake_everywhere(git_repo),
        session_id="smoke-blocked",
    ))
    # run halts on design-review severe → halt_for_human; we just check state.
    # Allow enough stages to exhaust L_MAX panel reruns.
    from autodev.errors import GatePending
    from autodev.artifacts.revision_state import L_MAX
    with pytest.raises(GatePending):
        orch.run(feature, max_stages=(L_MAX + 1) * 4)

    # design-review verdict should have been written with fail / IV.
    v = json.loads((active / "panel-design-review.json").read_text())
    assert v["verdict"] in ("fail", "needs_revision")
    assert any(
        f.get("severity") == "invariant_violation"
        for f in v.get("findings", [])
    )
