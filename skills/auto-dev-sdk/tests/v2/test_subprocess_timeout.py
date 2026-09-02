"""G11 cover: timeout branch in subprocess_runner."""
from __future__ import annotations

from pathlib import Path

import pytest

from autodev.vendors.config import StageSpec
from autodev.vendors.shared_call import SharedVendorResult, call_shared_vendor
import autodev.vendors.subprocess_runner as subprocess_runner
from autodev.vendors.subprocess_runner import run_stage_subprocess

FAKE_CLI = Path(__file__).resolve().parent / "fakes" / "fake_vendor_cli.sh"


def test_subprocess_timeout_produces_failure_json(git_repo, feature_active, monkeypatch):
    """FakeCLI 'timeout' behavior sleeps forever; harness times out after 2s."""
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_CLI))
    monkeypatch.setenv("AUTODEV_FAKE_BEHAVIOR", "timeout")
    monkeypatch.setenv("AUTODEV_FAKE_TARGET_ARTIFACT", str(feature_active / "scope.json"))
    monkeypatch.setenv("AUTODEV_FAKE_SOURCE_HASH", "sha256:" + "a" * 64)
    monkeypatch.setenv("AUTODEV_FAKE_SOURCE_PATH", "prd.md")
    monkeypatch.setenv("AUTODEV_PROBE_ENABLED", "0")

    spec = StageSpec(
        stage="scope", vendor="claude", model="fake",
        probe_interval_sec=2,  # very short for test
    )

    result = run_stage_subprocess(
        stage="scope", stage_spec=spec,
        prompt="test timeout", artifact_target=feature_active / "scope.json",
        feature_active=feature_active,
        allowed_write_paths=[feature_active],
        cwd=git_repo,
    )
    assert not result.ok
    assert result.failure_kind == "timeout"
    failure_path = feature_active / "scope-failure.json"
    assert failure_path.exists()
    import json
    f = json.loads(failure_path.read_text())
    assert f["kind"] == "timeout"


def test_shared_vendor_timeout_records_timeout_status(
    git_repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_CLI))
    monkeypatch.setenv("AUTODEV_FAKE_BEHAVIOR", "timeout")

    result = call_shared_vendor(
        vendor="claude",
        model="fake",
        prompt="test timeout",
        output_id="timeout-probe",
        timeout_sec=1,
        cwd=git_repo,
        output_dir=tmp_path / "vendor-out",
    )

    assert result.returncode != 0
    assert result.timed_out
    assert result.status["exit_code"] == "124"
    assert result.status["reason"] == "timeout"


def test_shared_vendor_exposes_standard_live_stream_for_codex(
    git_repo, tmp_path, monkeypatch
):
    fake_codex = tmp_path / "fake-codex.sh"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "out=''\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --output-last-message) out=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "prompt=$(cat)\n"
        "printf 'live transcript for %s\\n' \"$prompt\"\n"
        "printf 'final answer for %s\\n' \"$prompt\" > \"$out\"\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CODEX", str(fake_codex))

    out_dir = tmp_path / "vendor-out"
    result = call_shared_vendor(
        vendor="codex",
        model="fake",
        prompt="stream sentinel",
        output_id="codex-stream",
        timeout_sec=5,
        cwd=git_repo,
        output_dir=out_dir,
    )

    stream = out_dir / "codex-stream" / "stream"
    assert result.returncode == 0
    assert stream.exists()
    assert "live transcript for stream sentinel" in stream.read_text()
    assert result.status["stream"] == str(stream)
    assert "final answer for stream sentinel" in result.output


def test_stage_effort_field_passed_to_shared_vendor(
    git_repo, feature_active, tmp_path, monkeypatch
):
    captured = {}
    artifact = feature_active / "build.json"

    def fake_call_shared_vendor(**kwargs):
        captured["effort"] = kwargs["effort"]
        artifact.with_name(artifact.name + ".tmp").write_text("{}", encoding="utf-8")
        return SharedVendorResult(
            vendor=kwargs["vendor"],
            output_id=kwargs["output_id"],
            returncode=0,
            output="ok",
            log="",
            status={"exit_code": "0"},
            summary_stdout="",
            summary_stderr="",
            elapsed_sec=0.01,
            output_dir=tmp_path,
        )

    monkeypatch.setattr(subprocess_runner, "call_shared_vendor", fake_call_shared_vendor)
    spec = StageSpec(
        stage="build", vendor="claude", model="fake", probe_interval_sec=30, effort="xhigh"
    )

    result = run_stage_subprocess(
        stage="build",
        stage_spec=spec,
        prompt="test effort",
        artifact_target=artifact,
        feature_active=feature_active,
        allowed_write_paths=[feature_active],
        cwd=git_repo,
    )

    assert result.ok
    assert captured["effort"] == "xhigh"


def test_existing_unchanged_artifact_does_not_satisfy_a_new_stage_turn(
    git_repo, feature_active, tmp_path, monkeypatch,
):
    artifact = feature_active / "build.json"
    artifact.write_text('{"old": true}\n', encoding="utf-8")

    def fake_call_shared_vendor(**kwargs):
        return SharedVendorResult(
            vendor=kwargs["vendor"],
            output_id=kwargs["output_id"],
            returncode=0,
            output="exited without writing this turn's output",
            log="",
            status={"exit_code": "0"},
            summary_stdout="",
            summary_stderr="",
            elapsed_sec=0.01,
            output_dir=tmp_path,
        )

    monkeypatch.setattr(subprocess_runner, "call_shared_vendor", fake_call_shared_vendor)
    spec = StageSpec(
        stage="build", vendor="claude", model="fake", probe_interval_sec=30,
    )

    result = run_stage_subprocess(
        stage="build",
        stage_spec=spec,
        prompt="test stale output",
        artifact_target=artifact,
        feature_active=feature_active,
        allowed_write_paths=[feature_active],
        cwd=git_repo,
    )

    assert not result.ok
    assert result.failure_kind == "stale_artifact"
    assert "pre-existing build.json was unchanged" in result.failure_detail
    assert artifact.read_text(encoding="utf-8") == '{"old": true}\n'


def test_changed_direct_target_remains_a_supported_stage_output(
    git_repo, feature_active, tmp_path, monkeypatch,
):
    artifact = feature_active / "build.json"
    artifact.write_text('{"old": true}\n', encoding="utf-8")

    def fake_call_shared_vendor(**kwargs):
        artifact.write_text('{"fresh": true}\n', encoding="utf-8")
        return SharedVendorResult(
            vendor=kwargs["vendor"],
            output_id=kwargs["output_id"],
            returncode=0,
            output="wrote target directly",
            log="",
            status={"exit_code": "0"},
            summary_stdout="",
            summary_stderr="",
            elapsed_sec=0.01,
            output_dir=tmp_path,
        )

    monkeypatch.setattr(subprocess_runner, "call_shared_vendor", fake_call_shared_vendor)
    spec = StageSpec(
        stage="build", vendor="claude", model="fake", probe_interval_sec=30,
    )

    result = run_stage_subprocess(
        stage="build",
        stage_spec=spec,
        prompt="test direct output",
        artifact_target=artifact,
        feature_active=feature_active,
        allowed_write_paths=[feature_active],
        cwd=git_repo,
    )

    assert result.ok
    assert artifact.read_text(encoding="utf-8") == '{"fresh": true}\n'


def test_claude_529_retries_in_place_and_keeps_draft(
    git_repo, feature_active, tmp_path, monkeypatch,
):
    from autodev.vendors.shared_call import SharedVendorResult

    artifact = feature_active / "design.md"
    artifact.write_text("old\n", encoding="utf-8")
    artifact_tmp = feature_active / "design.md.tmp"
    calls = 0

    def fake_call_shared_vendor(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            artifact_tmp.write_text("useful partial draft\n", encoding="utf-8")
            return SharedVendorResult(
                vendor="claude", output_id="design", returncode=1,
                output="API Error: 529 Overloaded.", log="",
                status={"exit_code": "1", "session_mode": "resume"},
                summary_stdout="", summary_stderr="", elapsed_sec=0.01,
                output_dir=tmp_path,
            )
        assert artifact_tmp.read_text(encoding="utf-8") == "useful partial draft\n"
        artifact_tmp.write_text("completed draft\n", encoding="utf-8")
        return SharedVendorResult(
            vendor="claude", output_id="design", returncode=0,
            output="done", log="",
            status={"exit_code": "0", "session_mode": "resume"},
            summary_stdout="", summary_stderr="", elapsed_sec=0.01,
            output_dir=tmp_path,
        )

    monkeypatch.setattr(subprocess_runner, "call_shared_vendor", fake_call_shared_vendor)
    monkeypatch.setattr(subprocess_runner.time, "sleep", lambda _seconds: None)
    spec = StageSpec(
        stage="design", vendor="claude", model="fake", probe_interval_sec=30,
    )

    result = run_stage_subprocess(
        stage="design", stage_spec=spec, prompt="continue",
        artifact_target=artifact, feature_active=feature_active,
        allowed_write_paths=[feature_active], cwd=git_repo, preseed=True,
    )
    assert result.ok
    assert calls == 2
    assert artifact.read_text(encoding="utf-8") == "completed draft\n"


def test_retry_after_claude_529_resumes_prior_tmp_across_invocations(
    git_repo, feature_active, tmp_path, monkeypatch,
):
    from autodev.vendors.shared_call import SharedVendorResult

    artifact = feature_active / "design.md"
    artifact.write_text("old landed design\n", encoding="utf-8")
    artifact_tmp = feature_active / "design.md.tmp"
    artifact_tmp.write_text("partial design worth keeping\n", encoding="utf-8")
    (feature_active / "design-failure.json").write_text(
        '{"kind":"exit_nonzero","detail":"transient provider overload '
        'after 3 attempt(s)"}\n',
        encoding="utf-8",
    )

    def fake_call_shared_vendor(**kwargs):
        assert artifact_tmp.read_text(encoding="utf-8") == (
            "partial design worth keeping\n"
        )
        artifact_tmp.write_text("resumed and completed\n", encoding="utf-8")
        return SharedVendorResult(
            vendor="claude", output_id="design", returncode=0,
            output="done", log="", status={"exit_code": "0"},
            summary_stdout="", summary_stderr="", elapsed_sec=0.01,
            output_dir=tmp_path,
        )

    monkeypatch.setattr(subprocess_runner, "call_shared_vendor", fake_call_shared_vendor)
    spec = StageSpec(
        stage="design", vendor="claude", model="fake", probe_interval_sec=30,
    )
    result = run_stage_subprocess(
        stage="design", stage_spec=spec, prompt="resume",
        artifact_target=artifact, feature_active=feature_active,
        allowed_write_paths=[feature_active], cwd=git_repo, preseed=True,
    )
    assert result.ok
    assert artifact.read_text(encoding="utf-8") == "resumed and completed\n"
