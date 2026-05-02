"""Run a stage subprocess through the shared vendors adapter (R2, R2a, R2b).

Contract: harness tells the selected LLM where to write the artifact; the
LLM writes `<artifact>.tmp`; harness renames on exit 0. Model output is
logged for debugging, not parsed as the stage result.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from autodev.artifacts.failure import FailureReport, write_failure
from autodev.vendors.config import ProbeConfig, StageSpec
from autodev.vendors.shared_call import call_shared_vendor, split_common_vendor_flags

SIGKILL_GRACE_SEC = 30
STDERR_TAIL_BYTES = 4096
# Idle-timeout semantics: `timeout_sec` is interpreted as the maximum
# wall-clock interval between stdout/stderr updates. A subagent that
# keeps streaming (reading repo files, emitting tool calls, etc.) runs
# indefinitely; one that wedges for `timeout_sec` without any output is
# killed. Poll every IDLE_POLL_INTERVAL_SEC seconds.
IDLE_POLL_INTERVAL_SEC = 1


@dataclass
class StageRunResult:
    ok: bool
    artifact_path: Path | None
    stdout_path: Path
    stderr_path: Path
    exit_code: int | None
    failure_kind: str | None = None
    failure_detail: str = ""
    subprocess_reaped: bool = False
    elapsed_sec: float = 0.0


@dataclass
class SandboxReport:
    allowed_write_paths: list[Path] = field(default_factory=list)
    workspace_dirty_at_start: bool = False


def _exit_code_from_status(status: dict[str, str], fallback: int) -> int:
    try:
        return int(status.get("exit_code", str(fallback)))
    except ValueError:
        return fallback


def run_stage_subprocess(
    *,
    stage: str,
    stage_spec: StageSpec,
    prompt: str,
    artifact_target: Path,
    feature_active: Path,
    allowed_write_paths: list[Path],
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    log_emit: Callable[[dict], None] | None = None,
    extra_stdin: str = "",
    probe_config: ProbeConfig | None = None,
) -> StageRunResult:
    """Execute a single stage subprocess end-to-end.

    Writes artifact atomically via `<artifact_target>.tmp` → rename.
    On failure, writes `<stage>-failure.json` in `feature_active` and
    returns `ok=False` with taxonomy.
    """
    cwd = cwd or feature_active
    artifact_tmp = artifact_target.with_name(artifact_target.name + ".tmp")
    # Clean any stale tmp from a prior attempt
    if artifact_tmp.exists():
        try:
            artifact_tmp.unlink()
        except OSError:
            pass

    stdout_path = feature_active / f".{stage}.stdout.log"
    stderr_path = feature_active / f".{stage}.stderr.log"

    flags_effort, model_override, native_args = split_common_vendor_flags(stage_spec.flags)
    effort = stage_spec.effort or flags_effort
    model = model_override or stage_spec.model

    if log_emit:
        log_emit({"event": "subprocess-start", "stage": stage,
                  "vendor": stage_spec.vendor, "model": model,
                  "effort": effort or "<default>",
                  "timeout_sec": stage_spec.timeout_sec})

    start = time.monotonic()
    subprocess_reaped = False
    exit_code: int | None = None
    failure_kind: str | None = None
    failure_detail = ""

    try:
        result = call_shared_vendor(
            vendor=stage_spec.vendor,
            model=model,
            prompt=prompt + (extra_stdin or ""),
            output_id=stage,
            timeout_sec=stage_spec.timeout_sec,
            cwd=cwd,
            effort=effort,
            yolo=True,
            native_args=native_args,
            env_overrides=env_overrides,
        )
        exit_code = _exit_code_from_status(result.status, result.returncode)
        stdout_path.write_text(
            result.output
            + ("\n\n--- shared vendors summary ---\n" + result.summary_stdout
               if result.summary_stdout else ""),
            encoding="utf-8",
        )
        stderr_path.write_text(
            result.log
            + ("\n\n--- shared vendors stderr ---\n" + result.summary_stderr
               if result.summary_stderr else ""),
            encoding="utf-8",
        )
        if result.timed_out:
            failure_kind = "timeout"
            failure_detail = (
                f"vendor call timed out after {stage_spec.timeout_sec}s "
                f"through shared vendors"
            )
    except KeyboardInterrupt:
        failure_kind = "interrupted"
        failure_detail = "SIGINT during subprocess wait"
    except Exception as e:
        exit_code = None
        failure_kind = "exit_nonzero"
        failure_detail = f"shared vendors call failed: {e}"
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(str(e), encoding="utf-8")

    elapsed = time.monotonic() - start

    # Harness-side atomic rename (R2a artifact atomicity — vendor writes tmp,
    # harness renames).
    ok = False
    if failure_kind is None:
        if exit_code != 0:
            failure_kind = "exit_nonzero"
            failure_detail = f"shared vendor call exited {exit_code}"
        elif not artifact_tmp.exists() and not artifact_target.exists():
            failure_kind = "missing_artifact"
            failure_detail = f"neither {artifact_tmp.name} nor {artifact_target.name} after exit 0"
        elif artifact_tmp.exists():
            # Promote tmp → target.
            try:
                os.replace(artifact_tmp, artifact_target)
            except OSError as e:
                failure_kind = "missing_artifact"
                failure_detail = f"rename failed: {e}"
        # If artifact_target already exists (vendor wrote directly, skipping
        # our .tmp convention), we accept but flag a warning in log.
        if failure_kind is None:
            ok = True

    if not ok:
        # Tail stderr for the failure report.
        try:
            stderr_bytes = stderr_path.read_bytes()
            stderr_tail = stderr_bytes[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")
        except OSError:
            stderr_tail = ""
        report = FailureReport(
            stage=stage,
            kind=failure_kind or "exit_nonzero",
            detail=failure_detail,
            subprocess_exit=exit_code,
            stderr_tail=stderr_tail,
            ts=datetime.now(timezone.utc).isoformat(),
            subprocess_reaped=subprocess_reaped,
        )
        write_failure(feature_active / f"{stage}-failure.json", report)

    if log_emit:
        log_emit({"event": "subprocess-end", "stage": stage, "ok": ok,
                  "exit_code": exit_code, "failure_kind": failure_kind,
                  "elapsed_sec": elapsed, "reaped": subprocess_reaped})

    return StageRunResult(
        ok=ok,
        artifact_path=artifact_target if ok else None,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        exit_code=exit_code,
        failure_kind=failure_kind,
        failure_detail=failure_detail,
        subprocess_reaped=subprocess_reaped,
        elapsed_sec=elapsed,
    )
