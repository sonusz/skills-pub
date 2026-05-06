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
