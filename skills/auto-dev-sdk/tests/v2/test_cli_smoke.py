"""v2-14/15/16: CLI verb smoke + pause sentinel + git prerequisite."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
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


def test_cli_explain_uses_human_status_mode(git_repo, feature_active, capsys):
    code = main(["explain", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    out = capsys.readouterr().out
    assert "feature: demo" in out
    assert "status:  active" in out


def test_cli_grant_rerun_records_one_auditable_credit(
    git_repo, feature_active, capsys,
):
    from autodev.artifacts.revision_state import (
        L_MAX, RevisionState, load_state, write_state,
    )
    from autodev.artifacts.verdict import (
        PanelFinding, PanelVerdict, write_verdict,
    )

    state = RevisionState()
    state.L["design-review"] = L_MAX
    write_state(feature_active, state)
    write_verdict(
        feature_active / "panel-design-review.json",
        PanelVerdict(
            gate="design-review", verdict="needs_revision",
            findings=[PanelFinding(
                severity="risk", vendor="codex", summary="fix design",
                targets=["primary_pair.design.md"],
            )],
            source="design.md", source_hash="sha256:" + "0" * 64,
            prompt_file="p", prompt_hash="sha256:" + "0" * 64,
            harness_version="test", run_ts="2026-08-13T00:00:00Z",
        ),
    )

    code = main([
        "grant-rerun", "demo", "design-review",
        "--reason", "one narrow correction", "--who", "operator",
        "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK
    saved = load_state(feature_active)
    assert saved.manual_rerun_credits["design-review"] == 1
    assert saved.manual_rerun_grants[-1]["reason"] == "one narrow correction"
    assert saved.manual_rerun_grants[-1]["who"] == "operator"
    assert saved.manual_rerun_grants[-1]["consumed_at"] is None
    assert "gate still must pass" in capsys.readouterr().out

    duplicate = main([
        "grant-rerun", "demo", "design-review",
        "--reason", "do not stack", "--who", "operator",
        "--repo-root", str(git_repo),
    ])
    assert duplicate == exit_codes.ERROR
    assert "already pending" in capsys.readouterr().err


def test_cli_grant_rerun_accepts_trace_only_design_blocker(
    git_repo, feature_active, capsys,
):
    from autodev.artifacts.revision_state import (
        L_MAX, RevisionState, load_state, write_state,
    )
    from autodev.artifacts.verdict import (
        PanelFinding, PanelVerdict, ReviewDecision, write_verdict,
    )

    state = RevisionState()
    state.L["design-review"] = L_MAX
    write_state(feature_active, state)
    common = {
        "source": "design-packet.json",
        "source_hash": "sha256:" + "1" * 64,
        "prompt_file": "p", "prompt_hash": "sha256:" + "0" * 64,
        "harness_version": "test", "run_ts": "2026-08-13T00:00:00Z",
    }
    write_verdict(
        feature_active / "panel-design-review.json",
        PanelVerdict(
            gate="design-review", verdict="pass", findings=[],
            decision=ReviewDecision(
                node="design_review", outcome="pass", blocking=False,
                severity="opinion", summary="design passes",
            ),
            **common,
        ),
    )
    write_verdict(
        feature_active / "panel-trace-review.json",
        PanelVerdict(
            gate="trace-review", verdict="needs_revision",
            findings=[PanelFinding(
                severity="risk", vendor="codex", summary="trace blocks",
                targets=["primary_pair.trace.md"],
            )],
            **common,
        ),
    )

    code = main([
        "grant-rerun", "demo", "design-review",
        "--reason", "trace correction", "--who", "operator",
        "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK
    assert load_state(feature_active).manual_rerun_credits["design-review"] == 1


def test_cli_grant_rerun_rejects_human_only_verdict(
    git_repo, feature_active, capsys,
):
    from autodev.artifacts.revision_state import (
        L_MAX, RevisionState, load_state, write_state,
    )
    from autodev.artifacts.verdict import (
        PanelFinding, PanelVerdict, ReviewDecision, write_verdict,
    )

    state = RevisionState()
    state.L["design-review"] = L_MAX
    write_state(feature_active, state)
    write_verdict(
        feature_active / "panel-design-review.json",
        PanelVerdict(
            gate="design-review", verdict="needs_revision",
            findings=[PanelFinding(
                severity="risk", vendor="codex", summary="human decision",
                targets=["primary_pair.prd.md"],
            )],
            source="design-packet.json", source_hash="sha256:" + "2" * 64,
            prompt_file="p", prompt_hash="sha256:" + "0" * 64,
            harness_version="test", run_ts="2026-08-13T00:00:00Z",
            decision=ReviewDecision(
                node="design_review", outcome="halt_for_human", blocking=True,
                severity="risk", summary="requires human",
            ),
        ),
    )

    code = main([
        "grant-rerun", "demo", "design-review",
        "--reason", "must reject", "--who", "operator",
        "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    assert "not a producer-rerunnable" in capsys.readouterr().err
    assert load_state(feature_active).manual_rerun_credits["design-review"] == 0


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


def test_cli_reset_session_requires_pause_and_delegates(
    git_repo, feature_active, capsys, monkeypatch,
):
    import autodev.vendors.session_control as session_control

    calls = []
    monkeypatch.setattr(
        session_control,
        "reset_feature_session",
        lambda active, role: calls.append((active, role)) or 2,
    )
    code = main([
        "reset-session", "demo", "design", "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    assert "must be paused" in capsys.readouterr().err
    assert calls == []

    (feature_active / ".pause").touch()
    code = main([
        "reset-session", "demo", "design", "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK
    assert calls == [(feature_active, "design")]
    assert "reset 2 persistent session mapping" in capsys.readouterr().out


def test_cli_restore_design_requires_pause_and_delegates(
    git_repo, feature_active, capsys, monkeypatch,
):
    import autodev.artifacts.design_package_history as history

    calls = []
    snapshot = feature_active / "design-package-history" / "package-007"
    monkeypatch.setattr(
        history,
        "restore_design_package",
        lambda active, package: calls.append((active, package)) or (snapshot, ["design.md"]),
    )
    code = main([
        "restore-design", "demo", "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.ERROR
    assert "must be paused" in capsys.readouterr().err
    assert calls == []

    (feature_active / ".pause").touch()
    code = main([
        "restore-design", "demo", "--package", "package-007",
        "--repo-root", str(git_repo),
    ])
    assert code == exit_codes.OK
    assert calls == [(feature_active, "package-007")]
    assert "restored package-007" in capsys.readouterr().out


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


def test_cli_abort_reaps_registered_new_session_child(
    git_repo, feature_active, capsys,
):
    """Regression: panel children must not survive after their owner is killed."""
    marker = feature_active / ".abort-test-child-pid"
    helper_code = textwrap.dedent(
        """\
        import subprocess
        import sys
        import time
        from pathlib import Path

        from autodev.state.lock import Lock
        from autodev.state.process_registry import register_process, registry_path

        active = Path(sys.argv[1])
        marker = Path(sys.argv[2])
        lock = Lock(active, session_id="abort-test", verb="run")
        lock.acquire()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        register_process(
            registry_path(active),
            pid=child.pid,
            label="panel-reviewer:test",
        )
        marker.write_text(str(child.pid), encoding="utf-8")
        time.sleep(60)
        """
    )
    helper = subprocess.Popen(
        [sys.executable, "-c", helper_code, str(feature_active), str(marker)],
        cwd=Path(__file__).resolve().parents[2],
    )
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.02)
        assert marker.exists()
        child_pid = int(marker.read_text(encoding="utf-8"))

        code = main(["abort", "demo", "--repo-root", str(git_repo)])
        assert code == exit_codes.OK
        helper.wait(timeout=7)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"registered child pid {child_pid} survived abort")

        assert not (feature_active / ".running-pids.json").exists()
        failure = json.loads(
            (feature_active / "abort-failure.json").read_text(encoding="utf-8")
        )
        assert child_pid in json.loads(
            failure["detail"].split("registered_pids=", 1)[1].split(
                ", registered_pgids=", 1
            )[0]
        )
        assert "stopped 1 registered process group" in capsys.readouterr().out
    finally:
        if child_pid is not None:
            try:
                os.killpg(child_pid, 9)
            except ProcessLookupError:
                pass
        if helper.poll() is None:
            helper.kill()
        helper.wait(timeout=2)


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


def test_cli_invalidate_design_clears_whole_active_package_but_keeps_history(
    git_repo, feature_active,
):
    from autodev import overrides_api as ov

    ov.record_skip_gate(feature_active, gate="design-review", reason="r", who="t")
    active_outputs = (
        "design.md",
        "scope.json",
        "trace.md",
        "test-plan.md",
        "design-changelog.json",
        "design-packet.json",
        "panel-design-review.json",
        "panel-trace-review.json",
        "accepted-design.json",
        "panel-coverage-map.json",
        "panel-design-review.docs.json",
        "panel-design-review.reviewers.json",
        "panel-trace-review.docs.json",
        "panel-trace-review.reviewers.json",
        "diagnosis.json",
        "rework-mode.json",
    )
    for name in active_outputs:
        (feature_active / name).write_text("old\n", encoding="utf-8")
    for name in active_outputs[:5]:
        (feature_active / f"{name}.tmp").write_text("partial\n", encoding="utf-8")
    history = feature_active / "design-package-history" / "package-001"
    history.mkdir(parents=True)
    (history / "design.md").write_text("recoverable\n", encoding="utf-8")
    (feature_active / "prd.md").write_text("binding\n", encoding="utf-8")

    code = main([
        "invalidate", "demo", "design", "--repo-root", str(git_repo),
    ])

    assert code == exit_codes.OK
    assert not any((feature_active / name).exists() for name in active_outputs)
    assert not any(
        (feature_active / f"{name}.tmp").exists()
        for name in active_outputs[:5]
    )
    assert (history / "design.md").read_text(encoding="utf-8") == "recoverable\n"
    assert (feature_active / "prd.md").read_text(encoding="utf-8") == "binding\n"
    assert not ov.load(feature_active).has_active_skip_gate("design-review")


def test_cli_status_surfaces_overrides(git_repo, feature_active, capsys):
    from autodev import overrides_api as ov
    ov.record_skip_gate(feature_active, gate="design-review", reason="hotfix", who="dev")
    code = main(["status", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    out = capsys.readouterr().out
    assert "hotfix" in out
    assert "skip_gate" in out or "design-review" in out


def test_cli_run_until_design_threads_stop_before(git_repo, feature_active, monkeypatch):
    """`run --until design` must reach the orchestrator as
    stop_before='build' (stop the loop before the build phase)."""
    import autodev.cli as cli

    captured = {}

    class FakeOrch:
        def run(self, feature, *, stop_before=None):
            captured["feature"] = feature
            captured["stop_before"] = stop_before

    monkeypatch.setattr(cli, "_orch", lambda args: FakeOrch())
    code = main(["run", "demo", "--until", "design", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    assert captured == {"feature": "demo", "stop_before": "build"}


def test_cli_run_without_until_runs_to_completion(git_repo, feature_active, monkeypatch):
    import autodev.cli as cli

    captured = {}

    class FakeOrch:
        def run(self, feature, *, stop_before=None):
            captured["stop_before"] = stop_before

    monkeypatch.setattr(cli, "_orch", lambda args: FakeOrch())
    code = main(["run", "demo", "--repo-root", str(git_repo)])
    assert code == exit_codes.OK
    assert captured["stop_before"] is None


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
