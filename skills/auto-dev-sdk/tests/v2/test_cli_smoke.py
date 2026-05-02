"""v2-14/15/16: CLI verb smoke + pause sentinel + git prerequisite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from autodev import exit_codes
from autodev.cli import DEFAULT_VENDORS_YML, VENDORS_YML_ENV, main


def test_cli_status_nonexistent_returns_ok(git_repo, capsys, monkeypatch):
    monkeypatch.chdir(git_repo)
    code = main(["status", "ghost", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    out = capsys.readouterr().out
    assert "not found" in out


def test_cli_prd_from_file(git_repo, capsys, tmp_path):
    src = tmp_path / "draft.md"
    # PRD must pass Stage 0 schema (six sections + ≥1 `### R<N>:` marker).
    src.write_text(
        "# PRD\n"
        "## Problem\np\n## Users\nu\n"
        "## Requirements\n### R1: do a thing\n"
        "## Constraints\nc\n"
        "## Success criteria\ns\n"
        "## Out of scope\no\n"
    )
    code = main(["prd", "demo", "--from-file", str(src), "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    assert (git_repo / "docs" / "features" / "demo" / "planned" / "prd.md").exists()


def test_cli_prd_in_non_git_refused(tmp_path, capsys):
    (tmp_path / "docs" / "features").mkdir(parents=True)
    src = tmp_path / "p.md"
    src.write_text("# PRD\n")
    code = main(["prd", "demo", "--from-file", str(src), "--repo-root", str(tmp_path)])
    assert code == exit_codes.ERROR
    assert "preflight" in capsys.readouterr().err.lower()


def test_cli_pause_and_resume(git_repo, feature_active):
    code = main(["pause", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    assert (feature_active / ".pause").exists()
    code = main(["resume", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    assert not (feature_active / ".pause").exists()


def test_cli_abort_writes_pause_sentinel(git_repo, feature_active, capsys):
    """abort must hard-stop the run, not just kill the in-flight vendor.
    Writing the .pause sentinel ensures the orchestrator's for-loop
    cannot dispatch the next stage on the next iteration.
    Regression test for the bug where abort-killed vendor was treated as
    a normal stage failure → revision-loop redispatched the next vendor."""
    # No running pid file — abort should still complete cleanly and
    # leave the sentinel.
    assert not (feature_active / ".pause").exists()
    code = main(["abort", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    assert (feature_active / ".pause").exists(), \
        "abort must write .pause so orchestrator stops at next stage boundary"
    out = capsys.readouterr().out
    assert "resume" in out  # operator instruction present


def test_cli_skip_gate_requires_reason(git_repo, feature_active, capsys):
    # argparse-level: --reason is required; omit → SystemExit
    with pytest.raises(SystemExit):
        main(["skip-gate", "demo", "design-review", "--repo-root", str(git_repo)])


def test_cli_skip_gate_records_override(git_repo, feature_active):
    code = main([
        "skip-gate", "demo", "design-review",
        "--reason", "toy feature",
        "--who", "tester",
        "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK
    from autodev import overrides_api as ov
    loaded = ov.load(feature_active)
    assert loaded.has_active_skip_gate("design-review")
    assert loaded.active_records()[0].reason == "toy feature"


def test_cli_acknowledge_dirty_records(git_repo, feature_active):
    code = main([
        "acknowledge-dirty", "demo",
        "--reason", "mid-refactor",
        "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK
    from autodev import overrides_api as ov
    assert ov.load(feature_active).has_active_dirty_ack()


def test_cli_invalidate_clears_same_cycle_skip_gate(git_repo, feature_active):
    from autodev import overrides_api as ov
    ov.record_skip_gate(feature_active, gate="design-review", reason="r", who="t")
    # fake a panel_design_review.json to invalidate
    (feature_active / "panel-design-review.json").write_text('{}')
    code = main([
        "invalidate", "demo", "panel_design_review",
        "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK
    loaded = ov.load(feature_active)
    assert not loaded.has_active_skip_gate("design-review")


def test_cli_status_surfaces_overrides(git_repo, feature_active, capsys):
    from autodev import overrides_api as ov
    ov.record_skip_gate(feature_active, gate="design-review", reason="hotfix", who="dev")
    code = main(["status", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    out = capsys.readouterr().out
    assert "hotfix" in out
    assert "skip_gate" in out or "design-review" in out


def test_cli_run_uses_sdk_root_vendors_by_default(git_repo, feature_active, monkeypatch):
    import autodev.cli as cli

    calls = []

    def fake_load(path):
        calls.append(Path(path))
        return object()

    monkeypatch.delenv(VENDORS_YML_ENV, raising=False)
    monkeypatch.setattr(cli, "load_vendors_config", fake_load)

    cfg = cli._load_vendors(None, repo_root=git_repo)

    assert cfg is not None
    assert calls == [DEFAULT_VENDORS_YML]


def test_cli_run_honors_vendor_env_before_sdk_default(git_repo, tmp_path, monkeypatch):
    import autodev.cli as cli

    env_cfg = tmp_path / "global-vendors.yml"
    env_cfg.write_text("stages: {}\n", encoding="utf-8")
    calls = []

    def fake_load(path):
        calls.append(Path(path))
        return object()

    monkeypatch.setenv(VENDORS_YML_ENV, str(env_cfg))
    monkeypatch.setattr(cli, "load_vendors_config", fake_load)

    cfg = cli._load_vendors(None, repo_root=git_repo)

    assert cfg is not None
    assert calls == [env_cfg]
