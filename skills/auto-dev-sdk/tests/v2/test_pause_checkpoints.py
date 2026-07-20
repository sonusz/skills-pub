"""Regression: pause must take effect at the next safe checkpoint, not a
round later.

Reported symptom: setting `.pause` WHILE a panel is running let the design
agent run one full revision round before the pause was honored. Root cause:
the only pause checks sit at the run-loop top and `_advance_one` entry — both
BEFORE the panel runs. `_advance_gate` then chains straight from panel
completion into the producer rerun (`_advance_coding`) within the same call,
so a mid-panel pause was not observed until the next loop iteration.

These tests pin the two in-call checkpoints added to close that gap:
  1. design-review gate: halt after the panel verdict is on disk, before
     `handle_panel_verdict` bumps L[gate] and before the design rerun.
  2. build Ralph loop: halt at the top of each build round.
"""
from __future__ import annotations

import pytest

from autodev.artifacts.revision_state import load_state
from autodev.artifacts.verdict import PanelFinding, PanelVerdict
from autodev.errors import GatePending
from autodev.orchestrator import Orchestrator, OrchestratorConfig
from autodev.state.hashing import hash_file
from autodev.state.log import JsonlLog
from autodev.vendors.config import (
    PanelConfig,
    PanelReviewerSpec,
    PanelSynthesizerSpec,
    ProbeConfig,
    STAGES,
    StageSpec,
    VendorsConfig,
)


def _vendors(tmp_path) -> VendorsConfig:
    return VendorsConfig(
        path=tmp_path / "vendors.yml",
        stages={
            s: StageSpec(stage=s, vendor="claude", model="fake", probe_interval_sec=30)
            for s in STAGES
        },
        panel=PanelConfig(
            reviewers=(
                PanelReviewerSpec(vendor="claude", model="fake"),
                PanelReviewerSpec(vendor="agy", model="fake"),
            ),
            synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake"),
        ),
        probe=ProbeConfig(vendor="claude", model="fake"),
    )


def _orch(git_repo, tmp_path) -> Orchestrator:
    return Orchestrator(OrchestratorConfig(repo_root=git_repo, vendors=_vendors(tmp_path)))


def _blocking_verdict(primary) -> PanelVerdict:
    return PanelVerdict(
        gate="design-review",
        verdict="needs_revision",
        findings=[
            PanelFinding(
                severity="risk",
                vendor="claude",
                summary="design needs work",
                targets=["primary_pair.design.md"],
            )
        ],
        source=str(primary),
        source_hash=hash_file(primary),
        prompt_file="p",
        prompt_hash="sha256:" + "0" * 64,
        harness_version="test",
        run_ts="2026-05-28T00:00:00Z",
    )


def test_pause_during_panel_halts_before_design_revision(
    git_repo, feature_active, tmp_path, monkeypatch,
):
    """Pause set during the panel run halts the moment the panel finishes —
    before the design agent revises and before L[gate] is bumped."""
    orch = _orch(git_repo, tmp_path)
    primary = feature_active / "design-packet.json"
    primary.write_text("{}\n", encoding="utf-8")

    # No cached verdict, so the gate runs the panel.
    monkeypatch.setattr(
        "autodev.orchestrator.verdict_exists_and_valid", lambda **kw: None,
    )

    # Fake panel: simulate the operator running `autodev pause` mid-panel by
    # writing the sentinel, then return a blocking verdict (verdict is on disk
    # in production; here the orchestrator only consults effectively_blocks()).
    def fake_panel(**kwargs):
        (feature_active / ".pause").write_text("paused\n", encoding="utf-8")
        return _blocking_verdict(primary)

    monkeypatch.setattr("autodev.orchestrator.run_panel_gate", fake_panel)

    # The design revision must NOT be dispatched in this call.
    def boom(*a, **k):
        raise AssertionError("_advance_coding ran despite pause set during panel")

    monkeypatch.setattr(orch, "_advance_coding", boom)

    logger = JsonlLog(feature_active / "log.jsonl")
    with pytest.raises(GatePending) as ei:
        orch._advance_gate("demo", feature_active, "design-review", logger)

    assert ei.value.gate == "pause"
    # handle_panel_verdict never ran → revision budget untouched.
    assert load_state(feature_active).L.get("design-review", 0) == 0


def test_pause_halts_at_top_of_build_ralph_loop(
    git_repo, feature_active, tmp_path, monkeypatch,
):
    """A pause active when the build Ralph loop is entered halts before any
    build subprocess runs, rather than after the round completes."""
    orch = _orch(git_repo, tmp_path)
    (feature_active / ".pause").write_text("paused\n", encoding="utf-8")

    # If the loop reaches the build subprocess, the guard failed.
    def boom(*a, **k):
        raise AssertionError("build subprocess ran despite active pause")

    monkeypatch.setattr(orch, "_run_stage_subprocess_checked", boom)

    logger = JsonlLog(feature_active / "log.jsonl")
    with pytest.raises(GatePending) as ei:
        orch._advance_build_with_ralph_loop(
            feature="demo",
            active=feature_active,
            logger=logger,
            stage_spec=orch.cfg.vendors.resolve("build"),
            primary_target=feature_active / "build.json",
            allowed_write_paths=[feature_active, git_repo],
        )
    assert ei.value.gate == "pause"
