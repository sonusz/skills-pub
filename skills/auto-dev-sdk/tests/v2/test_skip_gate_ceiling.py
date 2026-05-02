"""G16: skip-gate ceiling + severity classification.

Rules:
  - severity=low records are not counted toward the ceiling
  - severity=normal records count as 1 each
  - severity=high records count as 2 each
  - weight ≥ 2 → `autodev close` emits WARNING (refuses without --yes)
  - weight ≥ 3 → `autodev close` REFUSES regardless of --yes
  - cycle advance via `autodev update` clears all active overrides → weight resets
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from autodev import overrides_api as ov
from autodev.artifacts.overrides import (
    CEILING_REFUSE_AT, CEILING_WARNING_AT, Overrides, OverrideRecord,
    SEVERITY_WEIGHT, load_overrides,
)


@pytest.fixture
def feature_with_prd(git_repo):
    """Build a feature directory with a PRD + scope + passing close panel."""
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    (active / "prd.md").write_text("# PRD\n\n## Overview\ndemo\n")
    # Force a passing close-approval verdict so close can proceed past gate
    # check if ceiling allows.
    from autodev.artifacts.verdict import PanelVerdict, PanelFinding, write_verdict
    v = PanelVerdict(
        gate="close-approval", verdict="pass", findings=[],
        source=str(active / "prd.md"),
        source_hash="sha256:" + "0" * 64,
        prompt_file="/dev/null", prompt_hash="sha256:" + "0" * 64,
        harness_version="test", run_ts="2026-04-20T00:00:00Z",
    )
    write_verdict(active / "panel-close-approval.json", v)
    return active


def test_weight_empty_is_zero(feature_with_prd):
    o = ov.load(feature_with_prd)
    assert o.active_skip_gate_weight() == 0


def test_weight_low_is_zero(feature_with_prd):
    ov.record_skip_gate(
        feature_with_prd, gate="design-review", reason="parser artifact",
        severity="low", who="tester",
    )
    o = ov.load(feature_with_prd)
    assert o.active_skip_gate_weight() == 0


def test_weight_normal_is_one(feature_with_prd):
    ov.record_skip_gate(
        feature_with_prd, gate="design-review", reason="normal override",
        severity="normal", who="tester",
    )
    assert ov.load(feature_with_prd).active_skip_gate_weight() == 1


def test_weight_high_is_two(feature_with_prd):
    ov.record_skip_gate(
        feature_with_prd, gate="design-review", reason="critical bypass",
        severity="high", who="tester",
    )
    assert ov.load(feature_with_prd).active_skip_gate_weight() == 2


def test_severity_mixed(feature_with_prd):
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="low note", severity="low", who="t")
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="normal note", severity="normal", who="t")
    ov.record_skip_gate(feature_with_prd, gate="close-approval",
                        reason="high note", severity="high", who="t")
    # low(0) + normal(1) + high(2) = 3
    assert ov.load(feature_with_prd).active_skip_gate_weight() == 3


def test_severity_back_compat_load(feature_with_prd):
    """Old overrides.json records without `severity` load as severity=normal."""
    legacy = {
        "current_cycle": 1,
        "records": [{
            "kind": "skip_gate", "reason": "legacy", "who": "old-tester",
            "ts": "2026-04-01T00:00:00Z", "skipped_in_cycle": 1,
            "gate": "design-review", "active": True,
            # NOTE: no 'severity' field
        }],
    }
    (feature_with_prd / "overrides.json").write_text(json.dumps(legacy))
    o = load_overrides(feature_with_prd / "overrides.json")
    assert o.records[0].severity == "normal"
    assert o.active_skip_gate_weight() == 1


def test_cycle_advance_clears_weight(feature_with_prd):
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="cycle-1 skip", severity="normal", who="t")
    assert ov.load(feature_with_prd).active_skip_gate_weight() == 1
    ov.advance_cycle(feature_with_prd)
    assert ov.load(feature_with_prd).active_skip_gate_weight() == 0


import shutil as _shutil

_SDK_ROOT = Path(__file__).resolve().parents[2]


def _autodev_cmd() -> list[str]:
    override = os.environ.get("AUTODEV_CLI")
    if override:
        return [override]
    installed = _shutil.which("autodev")
    if installed:
        return [installed]
    return [sys.executable, str(_SDK_ROOT / "autodev.py")]


def _run_cli(*args: str, cwd: Path, repo_root: Path) -> subprocess.CompletedProcess:
    """Invoke autodev CLI in-process via subprocess."""
    env = os.environ.copy()
    env["AUTODEV_WHO"] = "tester"
    return subprocess.run(
        [*_autodev_cmd(), *args, "--repo-root", str(repo_root)],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )


def test_close_refuses_at_refuse_ceiling(git_repo, feature_with_prd):
    # Three normal skips = weight 3 = refuse.
    for g in ("design-review", "design-review", "close-approval"):
        ov.record_skip_gate(feature_with_prd, gate=g, reason=f"skip {g}",
                            severity="normal", who="t")
    proc = _run_cli(
        "close", "demo", "complete", "--yes",
        cwd=git_repo, repo_root=git_repo,
    )
    assert proc.returncode != 0
    assert "refused" in proc.stderr.lower()
    assert "ceiling" in proc.stderr.lower()
    # Feature should still be at active/ (not moved to complete/)
    assert (git_repo / "docs" / "features" / "demo" / "active").exists()
    assert not (git_repo / "docs" / "features" / "demo" / "complete").exists()


def test_close_refuses_at_refuse_ceiling_single_high_plus_normal(
    git_repo, feature_with_prd,
):
    # high(2) + normal(1) = 3 = refuse
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="high", severity="high", who="t")
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="normal", severity="normal", who="t")
    proc = _run_cli("close", "demo", "complete", "--yes",
                    cwd=git_repo, repo_root=git_repo)
    assert proc.returncode != 0
    assert "ceiling" in proc.stderr.lower()


def test_close_warns_at_warning_ceiling_without_yes(git_repo, feature_with_prd):
    # Two normal = weight 2 = warn; without --yes, close should refuse.
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="s1", severity="normal", who="t")
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="s2", severity="normal", who="t")
    # Without --yes (send 'n' to prompt) — close should refuse.
    proc = _run_cli("close", "demo", "complete",
                    cwd=git_repo, repo_root=git_repo)
    assert proc.returncode != 0
    assert "warning" in proc.stderr.lower()


def test_close_proceeds_at_warning_with_yes(git_repo, feature_with_prd):
    # Weight 2 (warn) with --yes → should still close (warning only).
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="s1", severity="normal", who="t")
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="s2", severity="normal", who="t")
    proc = _run_cli("close", "demo", "complete", "--yes",
                    cwd=git_repo, repo_root=git_repo)
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    assert (git_repo / "docs" / "features" / "demo" / "complete").exists()


def test_close_clean_at_weight_zero(git_repo, feature_with_prd):
    # No skips → close proceeds cleanly.
    proc = _run_cli("close", "demo", "complete", "--yes",
                    cwd=git_repo, repo_root=git_repo)
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    assert (git_repo / "docs" / "features" / "demo" / "complete").exists()


def test_skip_gate_cli_accepts_severity(git_repo, feature_with_prd):
    proc = _run_cli(
        "skip-gate", "demo", "design-review",
        "--reason", "parser artifact", "--severity", "low",
        cwd=git_repo, repo_root=git_repo,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    o = ov.load(feature_with_prd)
    assert o.records[0].severity == "low"
    assert o.active_skip_gate_weight() == 0


def test_skip_gate_default_severity_is_normal(git_repo, feature_with_prd):
    proc = _run_cli(
        "skip-gate", "demo", "design-review", "--reason", "x",
        cwd=git_repo, repo_root=git_repo,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    o = ov.load(feature_with_prd)
    assert o.records[0].severity == "normal"


def test_skip_gate_warns_near_ceiling(git_repo, feature_with_prd):
    # Put one normal first, then skip-gate another normal → weight goes to 2.
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="s1", severity="normal", who="t")
    proc = _run_cli(
        "skip-gate", "demo", "design-review",
        "--reason", "s2", cwd=git_repo, repo_root=git_repo,
    )
    assert proc.returncode == 0
    assert "warning" in proc.stderr.lower()
    assert "block" in proc.stderr.lower()


def test_escalate_writes_snapshot(git_repo, feature_with_prd):
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="s1", severity="high", who="t")
    ov.record_skip_gate(feature_with_prd, gate="design-review",
                        reason="s2", severity="normal", who="t")
    proc = _run_cli("escalate", "demo",
                    cwd=git_repo, repo_root=git_repo)
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    esc = feature_with_prd / "escalation.json"
    assert esc.exists()
    snapshot = json.loads(esc.read_text())
    assert snapshot["feature"] == "demo"
    assert snapshot["active_skip_gate_weight"] == 3
    assert len(snapshot["active_skip_gates"]) == 2
    # Guidance message printed to stderr
    assert "amend the prd" in proc.stderr.lower() or "autodev update" in proc.stderr.lower()
