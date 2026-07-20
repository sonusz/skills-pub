"""g-23: orchestrator halts on build.json.blocking=true.

The stage-implement prompt contracts that a subagent sets top-level
`blocking: true` on build.json when a scope item is irreducibly
blocked (e.g. a trace row has no PRD backing). Prior to g-23 the
orchestrator silently ignored the field and advanced to spec anyway,
breaking the prompt contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev import exit_codes
from autodev.artifacts.build import BuildReport, write_build
from autodev.cli import main
from autodev.errors import GatePending
from autodev.orchestrator import Orchestrator, OrchestratorConfig
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


def _orch(repo_root: Path) -> Orchestrator:
    vendors = VendorsConfig(
        path=repo_root / "vendors.yml",
        stages={
            s: StageSpec(stage=s, vendor="claude", model="fake", probe_interval_sec=30)
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
    return Orchestrator(OrchestratorConfig(
        repo_root=repo_root, vendors=vendors, session_id="test",
    ))


def _write_build(active: Path, *, blocking: bool, deviations: list[dict]) -> None:
    write_build(active / "build.json", BuildReport(
        source=str(active / "scope.json"),
        source_hash="sha256:" + "0" * 64,
        written="2026-04-20",
        test_cmd_run="pytest",
        test_exit_code=0,
        test_results={"passed": 3, "failed": 0, "skipped": 0},
        files_changed=["src/a.py"],
        deviations=deviations,
        blocking=blocking,
    ))


def test_build_blocking_false_does_not_halt(git_repo, feature_active):
    """Baseline: blocking=false passes through _enforce_build_blocking."""
    _write_build(feature_active, blocking=False, deviations=[
        {"scope_id": "t-1", "severity": "minor", "blocking": False,
         "detail": "used stdlib json over orjson"},
    ])
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")
    # Should not raise.
    orch._enforce_build_blocking(feature_active, logger, "demo")


def test_build_blocking_true_halts_with_scope_ids(git_repo, feature_active):
    """blocking=true → GatePending('build_blocking', ...) listing scope_ids."""
    _write_build(feature_active, blocking=True, deviations=[
        {"scope_id": "t-3", "severity": "blocking", "blocking": True,
         "detail": "no PRD backing for auth flow"},
        {"scope_id": "t-7", "severity": "blocking", "blocking": True,
         "detail": "trace row contradicts scope"},
        {"scope_id": "t-9", "severity": "minor", "blocking": False,
         "detail": "unrelated minor choice"},
    ])
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    with pytest.raises(GatePending) as exc_info:
        orch._enforce_build_blocking(feature_active, logger, "demo")

    err = exc_info.value
    assert err.gate == "build_blocking"
    # Only blocking-flagged deviations surface as scope_ids.
    assert "t-3" in err.detail
    assert "t-7" in err.detail
    assert "t-9" not in err.detail
    assert "PRD amendment" in err.detail

    # Audit event emitted.
    events = [json.loads(line) for line in
              (feature_active / "log.jsonl").read_text().splitlines()]
    halt_events = [e for e in events if e.get("event") == "build-blocking-halt"]
    assert len(halt_events) == 1
    assert halt_events[0]["detail"]["scope_ids"] == ["t-3", "t-7"]


def test_status_surfaces_build_blocking(git_repo, feature_active, capsys,
                                        monkeypatch):
    """`autodev status` prints a 'build blocking: ...' line when set."""
    _write_build(feature_active, blocking=True, deviations=[
        {"scope_id": "t-5", "severity": "blocking", "blocking": True,
         "detail": "needs PRD amendment"},
    ])
    monkeypatch.chdir(git_repo)
    code = main(["status", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK

    out = capsys.readouterr().out
    assert "build blocking" in out
    assert "t-5" in out
    assert "PRD amendment required" in out
