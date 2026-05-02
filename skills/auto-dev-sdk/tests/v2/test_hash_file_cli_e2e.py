"""G17: end-to-end live-vendor run against the hash-file-cli reference.

Opt-in: runs real `claude` / `gemini` / `codex` CLIs. Costs wall-clock
time (5-10 min/run) and vendor-credit budget. Skipped by default.

Run with::

    python3 -m pytest -m live tests/v2/test_hash_file_cli_e2e.py -s

What this validates:
  - All 4 coding stages (design → build → spec → review) execute
    against live vendors
  - Both panel gates (design-review → close-approval) produce cleanly
    passing verdicts
    without skip-gate overrides
  - Design packet generation, design-review gating, and G20 (revision
    loop; via `autodev status` counters) are observed in
    passing runs with G=0 and no local revisions needed
  - `autodev close hash-file-cli complete --yes` succeeds

Phase-3 exit criterion: a clean run of this test means v2 can close
`hash-file-cli` without skip-gates, which is the main phase-3 claim.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REF_SRC = Path(__file__).resolve().parent.parent.parent / "examples" / "hash-file-cli"


def _cli_available(bin_name: str) -> bool:
    return shutil.which(bin_name) is not None


@pytest.fixture
def live_workspace(tmp_path):
    """Fresh git repo with hash-file-cli PRD staged in active/."""
    dest = tmp_path / "hash-file-cli-work"
    shutil.copytree(REF_SRC, dest)

    # Move planned/ → active/ (the harness's `run` verb expects active/).
    planned = dest / "docs" / "features" / "hash-file-cli" / "planned"
    active = dest / "docs" / "features" / "hash-file-cli" / "active"
    if planned.exists() and not active.exists():
        planned.rename(active)

    # Scope.json ships alongside the PRD in planned/; G17 runs the
    # scope stage fresh, so delete any pre-shipped scope artifact.
    stale = active / "scope.json"
    if stale.exists():
        stale.unlink()

    subprocess.run(["git", "init", "-q"], cwd=str(dest), check=True)
    subprocess.run(["git", "config", "user.email", "e2e@test"],
                   cwd=str(dest), check=True)
    subprocess.run(["git", "config", "user.name", "e2e"],
                   cwd=str(dest), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(dest), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed hash-file-cli"],
                   cwd=str(dest), check=True)
    return dest


@pytest.mark.live
def test_hash_file_cli_end_to_end(live_workspace, capsys):
    """Drive `autodev run hash-file-cli` through the full pipeline."""
    for bin_name in ("claude", "gemini", "codex"):
        if not _cli_available(bin_name):
            pytest.skip(f"{bin_name} CLI not on PATH")

    from autodev import exit_codes
    from autodev.cli import main

    # Surface all stdout/stderr from the harness via capsys.disabled() so
    # a human running this can watch progress across the 5-10 min run.
    with capsys.disabled():
        rc = main([
            "run", "hash-file-cli", "--repo-root", str(live_workspace),
        ])
    assert rc == exit_codes.OK, f"`autodev run` exited {rc}"

    active = live_workspace / "docs" / "features" / "hash-file-cli" / "active"

    # Every expected artifact present.
    for name in (
        "design.md", "scope.json", "trace.md", "test-plan.md", "build.json",
        "spec.md", "review.json",
        "panel-design-review.json", "panel-close-approval.json",
    ):
        assert (active / name).exists(), f"missing {name}"

    # Build stage tests passed.
    build = json.loads((active / "build.json").read_text())
    assert build["test_exit_code"] == 0, build
    assert build["test_results"]["failed"] == 0, build

    # No skip-gates needed (phase-3 exit criterion).
    overrides_path = active / "overrides.json"
    if overrides_path.exists():
        ov_raw = json.loads(overrides_path.read_text())
        active_records = [
            r for r in ov_raw.get("records", []) if r.get("active")
            and r.get("kind") == "skip_gate"
        ]
        assert not active_records, (
            f"skip-gate overrides used (forbidden in phase-3): "
            f"{active_records}"
        )

    # Every panel gate's verdict is non-blocking.
    from autodev.artifacts.verdict import load_verdict
    for gate in ("design-review", "close-approval"):
        v = load_verdict(active / f"panel-{gate}.json")
        assert not v.effectively_blocks(), (
            f"{gate} verdict blocks: {v.verdict} / findings={v.findings}"
        )

    # G20 revision-state: G should stay at 0 on a clean first run.
    rev_state = active / "revision-state.json"
    if rev_state.exists():
        s = json.loads(rev_state.read_text())
        assert s.get("G", 0) == 0, (
            f"revision loop fired during a run that was supposed to pass "
            f"cleanly: {s}"
        )

    # Close proceeds without skip-gate.
    rc = main([
        "close", "hash-file-cli", "complete", "--yes",
        "--repo-root", str(live_workspace),
    ])
    assert rc == exit_codes.OK
    complete = live_workspace / "docs" / "features" / "hash-file-cli" / "complete"
    assert complete.exists()
    # close cleared revision-state.
    assert not (complete / "revision-state.json").exists()


@pytest.mark.live
def test_live_required_clis_available():
    """Sanity check that a live run could plausibly succeed."""
    missing = [b for b in ("claude", "gemini", "codex") if not _cli_available(b)]
    assert not missing, f"missing vendor CLIs: {missing}"
