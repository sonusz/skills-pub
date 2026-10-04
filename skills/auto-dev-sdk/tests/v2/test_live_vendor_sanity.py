"""Live vendor sanity smoke for shared-vendors stage dispatch.

@pytest.mark.live — deselected by default; run with:
    python3 -m pytest -m live tests/v2/test_live_vendor_sanity.py -v

Not a full pipeline test. Just verifies, for the first pipeline step
(the arch-design -> arch-review loop):
  1. Real claude CLI dispatched through shared/vendors writes each
     stage's artifact within an aggressive timeout.
  2. Each stage's `stdout/stderr` log files receive observable output.

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
def test_live_first_step_completes_fast(live_git_repo):
    """The first step (arch-design then arch-review) with real claude must
    finish quickly and write observable activity to each stage's logs."""
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

    # A fresh feature starts with the architecture loop: arch-design writes
    # arch-design.md, then arch-review judges it. Observed ~60s for both.
    assert result.success, (
        f"first step failed at {result.stage_name}: {result.detail}"
    )
    assert result.stage_name == "arch_review", result.stage_name
    for artifact in ("arch-design.md", "arch-review.json"):
        assert (active / artifact).exists(), f"{artifact} not written"

    assert elapsed < 240, (
        f"first step took {elapsed:.1f}s — even generous budget "
        f"exceeded; vendor dispatch path is broken"
    )

    for stage in ("arch-design", "arch-review"):
        stdout_log = active / f".{stage}.stdout.log"
        stderr_log = active / f".{stage}.stderr.log"
        assert stdout_log.exists() and stderr_log.exists(), stage
        total_bytes = stdout_log.stat().st_size + stderr_log.stat().st_size
        assert total_bytes > 0, (
            f"shared-vendors dispatch for {stage} produced zero bytes of logs"
        )
