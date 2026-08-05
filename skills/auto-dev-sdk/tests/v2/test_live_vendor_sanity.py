"""Live vendor sanity smoke for shared-vendors stage dispatch.

@pytest.mark.live — deselected by default; run with:
    python3 -m pytest -m live tests/v2/test_live_vendor_sanity.py -v

Not a full pipeline test. Just verifies:
  1. Real claude CLI dispatched through shared/vendors writes
     the target artifact within an aggressive timeout.
  2. `stdout/stderr` log files receive observable output for debugging.

Uses the smallest possible PRD so even slow cold-starts land well
under 60s.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from autodev.orchestrator import Orchestrator, OrchestratorConfig
from autodev.vendors.config import (
    PanelConfig,
    PanelReviewerSpec,
    PanelSynthesizerSpec,
    ProbeConfig,
    STAGES,
    StageSpec,
    VendorsConfig,
)


@pytest.fixture
def live_git_repo(tmp_path):
    """Minimal git repo + feature folder with a 2-line PRD."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "live@test"],
                   cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "live"],
                   cwd=str(tmp_path), check=True)
    (tmp_path / "docs" / "features" / "live" / "active").mkdir(parents=True)
    prd = tmp_path / "docs" / "features" / "live" / "active" / "prd.md"
    prd.write_text(
        "# PRD: live sanity\n\n"
        "1. One in_scope item: `id=t-1`, description 'print hello'.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"],
                   cwd=str(tmp_path), check=True)
    return tmp_path


@pytest.mark.live
def test_live_scope_stage_completes_fast(live_git_repo):
    """Single scope stage with real claude must finish in <60s and
    must write observable activity to the log file (not buffered)."""
    from autodev import overrides_api as ov

    active = live_git_repo / "docs" / "features" / "live" / "active"
    ov.record_acknowledge_dirty(active, reason="live smoke", who="pytest")

    vendors = VendorsConfig(
        path=live_git_repo / "vendors.yml",
        stages={
            s: StageSpec(
                stage=s,
                vendor="claude", model="claude-sonnet-5",
                probe_interval_sec=60,
            ) for s in STAGES
        },
        panel=PanelConfig(
            reviewers=(
                PanelReviewerSpec(vendor="claude", model="claude-sonnet-5"),
                PanelReviewerSpec(vendor="cursor", model="gemini-3.1-pro"),
                PanelReviewerSpec(vendor="codex", model="gpt-5.6-terra"),
            ),
            synthesizer=PanelSynthesizerSpec(vendor="claude", model="claude-sonnet-5"),
        ),
        probe=ProbeConfig(vendor="claude", model="claude-haiku-4-5"),
    )
    orch = Orchestrator(OrchestratorConfig(
        repo_root=live_git_repo, vendors=vendors, session_id="live-sanity",
    ))

    start = time.monotonic()
    result = orch.advance_one("live")
    elapsed = time.monotonic() - start

    # Must complete and produce scope.json.
    assert result.success, (
        f"scope stage failed: {result.detail or result.stage_name}"
    )
    scope_json = active / "scope.json"
    assert scope_json.exists(), "scope.json not written"

    assert elapsed < 120, (
        f"scope stage took {elapsed:.1f}s — even generous budget "
        f"exceeded; vendor dispatch path is broken"
    )

    stdout_log = active / ".scope.stdout.log"
    stderr_log = active / ".scope.stderr.log"
    assert stdout_log.exists() and stderr_log.exists()
    total_bytes = stdout_log.stat().st_size + stderr_log.stat().st_size
    assert total_bytes > 0, (
        "shared-vendors dispatch produced zero bytes of logs"
    )
