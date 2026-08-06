"""Adapter for the packaged shared vendors module.

auto-dev-sdk keeps its v2 `vendors.yml` vocabulary (`codex` for the
OpenAI/Codex CLI), while the shared module exposes the stable
`openai|claude|agy|cursor|grok` interface and the
`<id>/out,status,log` contract.
This file is the only place that should know how to bridge those details.
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Literal

from autodev.state.process_registry import (
    register_process,
    terminate_process_group,
    unregister_process,
)


# Action returned by an idle_callback to call_shared_vendor.
# - "continue": keep waiting; we'll poll again next interval.
# - "kill": SIGTERM the subprocess group now and unwind.
IdleAction = Literal["continue", "kill"]


SDK_ROOT = Path(__file__).resolve().parents[2]
SHARED_VENDORS_DIR = SDK_ROOT / "shared" / "vendors"
SHARED_CALL_SCRIPT = SHARED_VENDORS_DIR / "scripts" / "call.sh"


@dataclass(frozen=True)
class SharedVendorResult:
    vendor: str
    output_id: str
    returncode: int
    output: str
    log: str
    status: dict[str, str]
    summary_stdout: str
    summary_stderr: str
    elapsed_sec: float
    output_dir: Path
    timed_out: bool = False
    session_id: str | None = None
    session_mode: str | None = None
    session_key_hash: str | None = None


@contextmanager
def _tracked_process(
    proc: subprocess.Popen,
    *,
    process_registry: Path | None,
    label: str,
) -> Iterator[None]:
    """Register a new-session process for the duration of its vendor call."""
    if process_registry is not None:
        register_process(process_registry, pid=proc.pid, label=label)
    try:
        yield
    finally:
        # The shared shell runner historically killed its timer subshell but
        # left the timer's long-lived `sleep` reparented to PID 1. A vendor
        # call owns its entire new session, so no process in that group should
        # survive once the group leader returns.
        terminate_process_group(proc.pid)
        if process_registry is not None:
            unregister_process(process_registry, pid=proc.pid)


def normalize_shared_vendor(vendor: str) -> str:
    raw = vendor.strip().lower()
    if raw in {"openai", "codex", "gpt"}:
        return "openai"
    if raw in {"claude", "anthropic"}:
        return "claude"
    if raw in {"agy", "antigravity"}:
        return "agy"
    if raw in {"cursor", "cursor-agent", "anysphere"}:
        return "cursor"
    if raw in {"grok", "xai"}:
        return "grok"
    raise ValueError(f"unknown vendor: {vendor!r}")


def cli_name_for_vendor(vendor: str) -> str:
    normalized = normalize_shared_vendor(vendor)
    if normalized == "openai":
        return "codex"
    return normalized


def _status_file_to_dict(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _live_stream_file(call_dir: Path, normalized_vendor: str) -> Path:
    """Return the shared-vendors live stream file for idle probing.

    Newer shared/vendors exposes `<id>/stream` for every provider. Keep a
    fallback for older checkouts so the SDK remains diagnosable when the
    bundled shell scripts are out of sync.
    """
    stream = call_dir / "stream"
    if stream.exists():
        return stream
    if normalized_vendor == "openai":
        transcript = call_dir / "codex-transcript.txt"
        if transcript.exists():
            return transcript
    return call_dir / "out"


def _candidate_binary_env_keys(vendor: str) -> list[str]:
    raw = vendor.strip().upper()
    cli = cli_name_for_vendor(vendor).upper()
    keys = [f"AUTODEV_VENDOR_BIN_{raw}", f"AUTODEV_VENDOR_BIN_{cli}"]
    if cli == "CODEX":
        keys.append("AUTODEV_VENDOR_BIN_OPENAI")
    # Preserve order while dropping duplicates.
    return list(dict.fromkeys(keys))


def _resolve_binary_override(vendor: str, explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    for key in _candidate_binary_env_keys(vendor):
        value = os.environ.get(key)
        if value:
            p = Path(value)
            if p.exists():
                return p
    return None


def _prepend_cli_override_to_path(
    *, vendor: str, explicit_binary: str | None, work_dir: Path, env: dict[str, str],
) -> None:
    binary = _resolve_binary_override(vendor, explicit_binary)
    if binary is None:
        return
    bin_dir = work_dir / "bin"
    bin_dir.mkdir(exist_ok=True)
    link = bin_dir / cli_name_for_vendor(vendor)
    if not link.exists():
        link.symlink_to(binary)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"


def split_common_vendor_flags(flags: tuple[str, ...] | list[str]) -> tuple[str, str | None, list[str]]:
    """Extract shared-module options from legacy per-vendor native flags.

    Returns `(effort, model_override, native_args)`. Unsupported or
    vendor-specific flags remain native args and still pass through the
    allowlist in `config.py`.
    """
    effort = ""
    model_override: str | None = None
    native: list[str] = []
    items = list(flags)
    i = 0
    while i < len(items):
        entry = items[i]
        if entry == "--effort" and i + 1 < len(items):
            effort = _normalize_effort(items[i + 1])
            i += 2
            continue
        if entry.startswith("--effort="):
            effort = _normalize_effort(entry.split("=", 1)[1])
            i += 1
            continue
        if entry == "--model" and i + 1 < len(items):
            model_override = items[i + 1]
            i += 2
            continue
        if entry.startswith("--model="):
            model_override = entry.split("=", 1)[1]
            i += 1
            continue
        native.append(entry)
        i += 1
    return effort, model_override, native


def _normalize_effort(value: str) -> str:
    raw = value.strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "minimal": "min",
        "minimum": "min",
        "med": "medium",
        "mid": "medium",
        "hi": "high",
        "x-high": "xhigh",
        "extra-high": "xhigh",
        "extrahigh": "xhigh",
        "maximum": "max",
    }
    return aliases.get(raw, raw)


def call_shared_vendor(
    *,
    vendor: str,
    model: str | None,
    prompt: str,
    output_id: str,
    timeout_sec: int,
    cwd: Path | None = None,
    effort: str = "",
    yolo: bool = False,
    context_files: list[Path] | tuple[Path, ...] = (),
    native_args: list[str] | tuple[str, ...] = (),
    env_overrides: dict[str, str] | None = None,
    binary_override: str | None = None,
    output_dir: Path | None = None,
    schema_json: str | None = None,
    idle_callback: Callable[..., IdleAction] | None = None,
    idle_check_interval_sec: int = 10,
    process_registry: Path | None = None,
    process_label: str | None = None,
    session_key: str | None = None,
    resume_prompt: str | None = None,
) -> SharedVendorResult:
    if not SHARED_CALL_SCRIPT.exists():
        raise FileNotFoundError(
            f"shared vendors module not found at {SHARED_CALL_SCRIPT}; "
            "install or package auto-dev-sdk with shared/vendors"
        )

    normalized = normalize_shared_vendor(vendor)
    with tempfile.TemporaryDirectory(prefix="autodev-vendors.") as tmp:
        work_dir = Path(tmp)
        prompt_file = work_dir / "prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        resume_prompt_file: Path | None = None
        if resume_prompt is not None:
            resume_prompt_file = work_dir / "resume-prompt.txt"
            resume_prompt_file.write_text(resume_prompt, encoding="utf-8")
        actual_output_dir = output_dir or (work_dir / "out")
        schema_file: Path | None = None
        if schema_json is not None:
            schema_file = work_dir / "schema.json"
            schema_file.write_text(schema_json, encoding="utf-8")

        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        _prepend_cli_override_to_path(
            vendor=vendor,
            explicit_binary=binary_override,
            work_dir=work_dir,
            env=env,
        )

        cmd = [
            str(SHARED_CALL_SCRIPT),
            "--vendor", normalized,
            "--id", output_id,
            "--prompt-file", str(prompt_file),
            "--output-dir", str(actual_output_dir),
            "--min-success", "1",
            "--timeout", str(max(timeout_sec, 0)),
        ]
        if model:
            cmd.extend(["--model", model])
        if effort:
            cmd.extend(["--effort", _normalize_effort(effort)])
        if yolo:
            cmd.append("--yolo")
        if cwd is not None:
            cmd.extend(["--cwd", str(cwd)])
        if session_key is not None:
            cmd.extend(["--session-key", session_key])
        if resume_prompt_file is not None:
            cmd.extend(["--resume-prompt-file", str(resume_prompt_file)])
        if schema_file is not None:
            cmd.extend(["--schema-file", str(schema_file)])
        for context_file in context_files:
            cmd.extend(["--context-file", str(context_file)])
        for arg in native_args:
            cmd.extend(["--native-arg", arg])

        call_dir = actual_output_dir / output_id
        start = time.monotonic()
        wallclock_start = time.time()

        if idle_callback is None:
            # Original blocking-communicate path. Single hard wall-clock
            # cap, no probe interaction.
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
            with _tracked_process(
                proc,
                process_registry=process_registry,
                label=process_label or output_id,
            ):
                try:
                    summary_stdout, summary_stderr = proc.communicate(
                        timeout=max(timeout_sec, 0) + 30 if timeout_sec else None,
                    )
                    elapsed = time.monotonic() - start
                    returncode = proc.returncode
                    outer_timed_out = False
                except subprocess.TimeoutExpired as e:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        summary_stdout, summary_stderr = proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        summary_stdout, summary_stderr = proc.communicate()
                    elapsed = time.monotonic() - start
                    returncode = -1
                    summary_stdout = summary_stdout or e.stdout or ""
                    summary_stderr = summary_stderr or e.stderr or ""
                    outer_timed_out = True
        else:
            # Idle-callback path. Redirect summary stdout/stderr to
            # files (so we can call proc.wait() repeatedly without
            # blocking on a PIPE buffer) and poll the stream output
            # file's mtime each interval, asking the callback whether
            # to keep waiting or kill the subprocess group.
            summary_out_path = work_dir / "summary.out"
            summary_err_path = work_dir / "summary.err"
            outer_timed_out = False
            with open(summary_out_path, "w") as so_f, open(summary_err_path, "w") as se_f:
                proc = subprocess.Popen(
                    cmd,
                    stdout=so_f,
                    stderr=se_f,
                    env=env,
                    start_new_session=True,
                )
                with _tracked_process(
                    proc,
                    process_registry=process_registry,
                    label=process_label or output_id,
                ):
                    hard_deadline = (
                        start + (timeout_sec + 30)
                        if timeout_sec and timeout_sec > 0
                        else None
                    )
                    while True:
                        try:
                            proc.wait(timeout=max(idle_check_interval_sec, 1))
                            break  # natural exit
                        except subprocess.TimeoutExpired:
                            # Still running: gather stream-file state and ask callback.
                            stream_file = _live_stream_file(call_dir, normalized)
                            if stream_file.exists():
                                try:
                                    last_mtime = stream_file.stat().st_mtime
                                except OSError:
                                    last_mtime = wallclock_start
                            else:
                                last_mtime = wallclock_start
                            idle_sec = max(0.0, time.time() - last_mtime)
                            elapsed_now = time.monotonic() - start
                            try:
                                action: IdleAction = idle_callback(
                                    stream_file=stream_file,
                                    idle_sec=idle_sec,
                                    elapsed_sec=elapsed_now,
                                    pid=proc.pid,
                                )
                            except Exception:
                                # Defensive: if the callback explodes, treat
                                # as "continue" so we don't lose the run for
                                # an observability bug; the hard deadline
                                # still backstops us.
                                action = "continue"
                            if action == "kill":
                                try:
                                    os.killpg(proc.pid, signal.SIGTERM)
                                except ProcessLookupError:
                                    pass
                                try:
                                    proc.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    try:
                                        os.killpg(proc.pid, signal.SIGKILL)
                                    except ProcessLookupError:
                                        pass
                                    proc.wait()
                                outer_timed_out = True
                                break
                            if hard_deadline is not None and time.monotonic() >= hard_deadline:
                                try:
                                    os.killpg(proc.pid, signal.SIGTERM)
                                except ProcessLookupError:
                                    pass
                                try:
                                    proc.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    try:
                                        os.killpg(proc.pid, signal.SIGKILL)
                                    except ProcessLookupError:
                                        pass
                                    proc.wait()
                                outer_timed_out = True
                                break
                    elapsed = time.monotonic() - start
                    returncode = proc.returncode if proc.returncode is not None else -1
            summary_stdout = (
                summary_out_path.read_text(encoding="utf-8")
                if summary_out_path.exists()
                else ""
            )
            summary_stderr = (
                summary_err_path.read_text(encoding="utf-8")
                if summary_err_path.exists()
                else ""
            )

        status = _status_file_to_dict(call_dir / "status")
        output = _read_text(call_dir / "out")
        log = _read_text(call_dir / "log")
        status_code = status.get("exit_code")
        timed_out = (
            outer_timed_out
            or returncode != 0
            and timeout_sec > 0
            and elapsed >= max(timeout_sec - 0.5, 0)
            and status_code in {"124", "137", "143", "-9", "-15"}
        )
        # DEBUG capture (behavior-preserving; active only when env var set):
        # persist the whole work dir (call_dir out/status/log + summary stderr +
        # any vendor transcript) before the TemporaryDirectory is cleaned, so a
        # failing vendor's REAL stderr can be inspected after the run.
        _keep_dir = os.environ.get("AUTODEV_KEEP_VENDOR_TMP")
        if _keep_dir:
            import shutil
            try:
                _dest = Path(_keep_dir) / f"{output_id}-rc{returncode}-{Path(tmp).name}"
                shutil.copytree(work_dir, _dest, dirs_exist_ok=True)
            except OSError:
                pass
        return SharedVendorResult(
            vendor=normalized,
            output_id=output_id,
            returncode=returncode,
            output=output,
            log=log,
            status=status,
            summary_stdout=summary_stdout,
            summary_stderr=summary_stderr,
            elapsed_sec=elapsed,
            output_dir=actual_output_dir,
            timed_out=timed_out,
            session_id=status.get("session_id") or None,
            session_mode=status.get("session_mode") or None,
            session_key_hash=status.get("session_key_hash") or None,
        )
