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
from autodev.state.process_registry import registry_path
from autodev.vendors.config import ProbeConfig, StageSpec
from autodev.vendors.fallback import build_candidates, resolve_candidate
from autodev.vendors.probe_agent import ProbeVerdict, run_idle_probe
from autodev.vendors.shared_call import (
    IdleAction,
    call_shared_vendor,
    split_common_vendor_flags,
)

SIGKILL_GRACE_SEC = 30
STDERR_TAIL_BYTES = 4096
# Idle-timeout semantics: each StageSpec carries `probe_interval_sec`
# (the per-stage idle threshold for probe consultation). When the
# vendor's stream output file has been silent for that many seconds,
# the harness invokes the idle probe LLM, which decides extend vs
# kill. Polling cadence is set by IDLE_POLL_INTERVAL_SEC; the bash
# watchdog and Python wait deadline are derived as a generous
# multiple of probe_interval_sec (HARD_BACKSTOP_MULTIPLIER) with an
# absolute floor (HARD_BACKSTOP_FLOOR_SEC) so the probe path is the
# operative timeout mechanism and the bash side is just a last-resort
# safety net.
IDLE_POLL_INTERVAL_SEC = 5
HARD_BACKSTOP_MULTIPLIER = 5
HARD_BACKSTOP_FLOOR_SEC = 28800  # 8 hours


def _hard_backstop_sec(probe_interval_sec: int) -> int:
    return max(probe_interval_sec * HARD_BACKSTOP_MULTIPLIER, HARD_BACKSTOP_FLOOR_SEC)


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
    preseed: bool = False,
    resume_prompt: str | None = None,
) -> StageRunResult:
    """Execute a single stage subprocess end-to-end.

    Writes artifact atomically via `<artifact_target>.tmp` → rename.
    On failure, writes `<stage>-failure.json` in `feature_active` and
    returns `ok=False` with taxonomy.

    When ``preseed`` is set and a landed ``artifact_target`` already
    exists (i.e. this is a rerun), the ``.tmp`` is pre-filled with the
    current artifact so the subagent can revise it in place (Edit)
    instead of regenerating the whole file from scratch. This is purely
    an output-token optimization — the atomic-rename contract is
    unchanged: on failure the (possibly half-edited) ``.tmp`` is
    discarded and the landed artifact stays untouched.
    """
    cwd = cwd or feature_active

    # Quota gate (before any subprocess): pick the primary or the first fallback
    # whose vendor has enough remaining quota. Raises QuotaHalt (propagated to the
    # orchestrator to pause + schedule) when no candidate qualifies. Kept OUTSIDE
    # the try/except below so QuotaHalt is never swallowed as a stage failure.
    chosen = resolve_candidate(
        build_candidates(stage_spec),
        role=stage,
        logger=(lambda m: log_emit({"event": "quota", "stage": stage, "msg": m}))
        if log_emit
        else None,
    )

    artifact_tmp = artifact_target.with_name(artifact_target.name + ".tmp")
    # Clean any stale tmp from a prior attempt, then optionally pre-seed.
    if artifact_tmp.exists():
        try:
            artifact_tmp.unlink()
        except OSError:
            pass
    if preseed and artifact_target.exists():
        import shutil
        try:
            shutil.copyfile(artifact_target, artifact_tmp)
        except OSError:
            pass

    stdout_path = feature_active / f".{stage}.stdout.log"
    stderr_path = feature_active / f".{stage}.stderr.log"

    flags_effort, model_override, native_args = split_common_vendor_flags(chosen.flags)
    effort = chosen.effort or flags_effort
    model = model_override or chosen.model
    vendor = chosen.vendor
    session_key: str | None = None
    session_max_turns: int | None = None
    if stage in {"design", "build", "ralph-review"}:
        from autodev.vendors.session_keys import (
            feature_session_key,
            session_max_turns_for_role,
        )

        session_key = feature_session_key(feature_active, stage)
        session_max_turns = session_max_turns_for_role(stage)

    probe_enabled = probe_config is not None
    hard_backstop_sec = (
        _hard_backstop_sec(stage_spec.probe_interval_sec)
        if probe_enabled
        else stage_spec.probe_interval_sec
    )

    if log_emit:
        log_emit({"event": "subprocess-start", "stage": stage,
                  "vendor": vendor, "model": model,
                  "effort": effort or "<default>",
                  "probe_interval_sec": stage_spec.probe_interval_sec,
                  "hard_backstop_sec": hard_backstop_sec})

    start = time.monotonic()
    subprocess_reaped = False
    exit_code: int | None = None
    failure_kind: str | None = None
    failure_detail = ""
    session_mode: str | None = None
    session_turn: int | None = None
    session_auto_reset = False

    idle_callback = (
        _build_idle_callback(
            stage=stage,
            stage_probe_interval_sec=stage_spec.probe_interval_sec,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            probe_config=probe_config,
            log_emit=log_emit,
            process_registry=registry_path(feature_active),
        )
        if probe_enabled
        else None
    )
    try:
        result = call_shared_vendor(
            vendor=vendor,
            model=model,
            prompt=prompt + (extra_stdin or ""),
            output_id=stage,
            timeout_sec=hard_backstop_sec,
            cwd=cwd,
            effort=effort,
            yolo=True,
            native_args=native_args,
            env_overrides=env_overrides,
            idle_callback=idle_callback,
            idle_check_interval_sec=IDLE_POLL_INTERVAL_SEC,
            process_registry=registry_path(feature_active),
            process_label=stage,
            session_key=session_key,
            session_max_turns=session_max_turns,
            resume_prompt=resume_prompt if session_key is not None else None,
        )
        session_mode = result.session_mode
        session_turn = result.session_turn
        session_auto_reset = result.session_auto_reset
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
                f"vendor call killed (probe-driven idle threshold "
                f"{stage_spec.probe_interval_sec}s, hard backstop "
                f"{hard_backstop_sec}s) through shared vendors"
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
                  "elapsed_sec": elapsed, "reaped": subprocess_reaped,
                  "session_mode": session_mode,
                  "session_turn": session_turn,
                  "session_auto_reset": session_auto_reset})

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


def _build_idle_callback(
    *,
    stage: str,
    stage_probe_interval_sec: int,
    stdout_path: Path,
    stderr_path: Path,
    probe_config: ProbeConfig,
    log_emit: Callable[[dict], None] | None,
    process_registry: Path | None = None,
) -> Callable[..., IdleAction]:
    """Construct the idle-watch callback `call_shared_vendor` will poll.

    Behavior:
    - When the stream output file has been silent
      < `stage_probe_interval_sec`, return "continue" without spending
      a probe call.
    - When the threshold is crossed, ask `run_idle_probe`. A `kill`
      verdict returns "kill" (call_shared_vendor unwinds the proc);
      an `extend` verdict returns "continue" and we wait at least
      `extend_sec` before invoking the probe again, so we honor the
      probe's grace grant without hammering it.

    The probe verdict object is logged with full raw output (truncated
    to 800 chars) so a false-positive kill leaves enough trail to
    diagnose without re-running.
    """
    state: dict[str, float] = {"next_probe_at": 0.0}

    def cb(*, stream_file: Path, idle_sec: float, elapsed_sec: float, pid: int) -> IdleAction:
        if idle_sec < stage_probe_interval_sec:
            return "continue"
        now = time.monotonic()
        if now < state["next_probe_at"]:
            # Probe previously granted an extension that has not
            # elapsed yet; keep waiting silently.
            return "continue"
        try:
            verdict: ProbeVerdict = run_idle_probe(
                stage=stage,
                pid=pid,
                idle_sec=int(idle_sec),
                idle_cap_sec=stage_probe_interval_sec,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                probe_config=probe_config,
                stream_output_file=stream_file,
                process_registry=process_registry,
            )
        except Exception:
            # Defensive: if the probe itself errors (transient OS
            # failure reading process tree, exec error, etc.), we keep
            # the run alive. The hard wall-clock backstop in
            # call_shared_vendor still bounds total runtime.
            return "continue"
        if log_emit is not None:
            log_emit({
                "event": "idle-probe",
                "stage": stage,
                "action": verdict.action,
                "extend_sec": verdict.extend_sec,
                "idle_sec": int(idle_sec),
                "elapsed_sec": int(elapsed_sec),
                "rationale": verdict.rationale[:200],
                "raw_output": verdict.raw_output[:800],
            })
        if verdict.action == "kill":
            return "kill"
        # extend: wait at least the granted seconds before the next probe.
        state["next_probe_at"] = now + max(verdict.extend_sec or 1, 1)
        return "continue"

    return cb
