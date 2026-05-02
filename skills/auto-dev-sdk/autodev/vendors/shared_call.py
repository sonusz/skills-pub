"""Adapter for the packaged shared vendors module.

auto-dev-sdk keeps its v2 `vendors.yml` vocabulary (`codex` for the
OpenAI/Codex CLI), while the shared module exposes the stable
`openai|claude|gemini` interface and the `<id>/out,status,log` contract.
This file is the only place that should know how to bridge those details.
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


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


def normalize_shared_vendor(vendor: str) -> str:
    raw = vendor.strip().lower()
    if raw in {"openai", "codex", "gpt"}:
        return "openai"
    if raw in {"claude", "anthropic"}:
        return "claude"
    if raw in {"gemini", "google"}:
        return "gemini"
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
        if schema_file is not None:
            cmd.extend(["--schema-file", str(schema_file)])
        for context_file in context_files:
            cmd.extend(["--context-file", str(context_file)])
        for arg in native_args:
            cmd.extend(["--native-arg", arg])

        call_dir = actual_output_dir / output_id
        start = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
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
        )
