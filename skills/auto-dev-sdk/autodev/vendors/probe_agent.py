"""Idle-timeout probe utility (Plan B).

When a caller wants a short-lived LLM judgment on a quiet process, the
probe asks: "is this subagent wedged, or working on a long task?" The
probe reads the process tree + log tails and replies
VERDICT: extend <N> | kill.

Kept deliberately thin:
- Read-only and routed through shared/vendors for real calls.
- Hard probe-level timeout so the probe itself can't wedge.
- Fake-invoker env var for tests.

Kept as a thin probe utility; defensive failures default to kill.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from autodev.errors import QuotaHalt
from autodev.vendors.config import ProbeConfig
from autodev.vendors.fallback import build_candidates, resolve_candidate
from autodev.vendors.shared_call import (
    SHARED_VENDORS_DIR,
    call_shared_vendor,
    split_common_vendor_flags,
)

PROMPT_FILENAME = "idle-probe.md"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

MAX_EXTEND_SEC = 1800
VERDICT_RE = re.compile(
    r"^\s*VERDICT:\s*(extend\s+(\d+)|kill)\s*$", re.MULTILINE,
)

# Test override. When set to a fake-invoker path, the probe routes
# through it instead of the real claude CLI. Same contract as the panel
# fake: receives prompt on stdin, writes verdict to stdout.
FAKE_PROBE_ENV = "AUTODEV_PROBE_FAKE"

# Test override. When set to "extend:N" or "kill", bypass entirely and
# return that verdict without any subprocess call. Useful for pure-unit
# tests that just want to exercise subprocess_runner's hook path.
FAKE_PROBE_VERDICT_ENV = "AUTODEV_PROBE_FAKE_VERDICT"


@dataclass
class ProbeVerdict:
    action: str          # "extend" or "kill"
    extend_sec: int = 0  # 0 unless action == "extend"
    rationale: str = ""  # free-form reason line (for operator log)
    raw_output: str = ""


# Shared, cross-platform process-tree helper. Primary path so every
# shared-vendors consumer gets one tested implementation, and so the
# portability logic lives next to the rest of the vendor process handling
# (call.sh's kill_tree). NEVER use GNU-only `ps --forest` / `cmd` here: they
# fail on macOS BSD ps, which starves the probe of input and biases it toward
# a false-positive kill.
PROCESS_TREE_SCRIPT = SHARED_VENDORS_DIR / "scripts" / "process-tree.sh"


def _process_tree(root_pid: int) -> str:
    """Return a process tree rooted at root_pid (plus its descendants).

    Primary path delegates to the shared ``process-tree.sh`` helper. Falls
    back to an inline portable ``ps`` (keywords common to BSD/macOS and
    GNU/Linux) if the helper is absent. Sentinel string if the process is
    gone."""
    if PROCESS_TREE_SCRIPT.exists():
        try:
            out = subprocess.run(
                ["bash", str(PROCESS_TREE_SCRIPT), str(root_pid)],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    # Inline fallback: portable `ps` form (works on BSD/macOS and GNU/Linux).
    # Root pid only; the shared helper above is what surfaces descendants.
    try:
        out = subprocess.run(
            ["ps", "-o", "pid,ppid,state,etime,command", "-p", str(root_pid)],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return f"(could not read process tree for pid {root_pid})"


def _tail(path: Path, max_lines: int = 200) -> str:
    if not path.exists():
        return "(file not present)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"(read error: {e})"
    return "\n".join(lines[-max_lines:])


def _compose_prompt(
    *, stage: str, pid: int, idle_sec: int, idle_cap_sec: int,
    stdout_path: Path, stderr_path: Path,
    stream_output_file: Path | None = None,
) -> str:
    base = (PROMPTS_DIR / PROMPT_FILENAME).read_text(encoding="utf-8")
    proc_tree = _process_tree(pid)
    stdout_tail = _tail(stdout_path, 200)
    stderr_tail = _tail(stderr_path, 200)
    stream_block = ""
    if stream_output_file is not None:
        if stream_output_file.exists():
            try:
                st = stream_output_file.stat()
                size_bytes = st.st_size
                seconds_since = max(0, int(time.time() - st.st_mtime))
            except OSError:
                size_bytes = 0
                seconds_since = idle_sec
        else:
            size_bytes = 0
            seconds_since = idle_sec
        stream_block = (
            f"- **Stream output file path**: `{stream_output_file}`\n"
            f"- **Stream output file size_bytes**: `{size_bytes}`\n"
            f"- **Stream output file seconds_since_modified**: `{seconds_since}s`\n"
        )
        stream_block += (
            "\n### Stream output tail (last 200 lines)\n\n```\n"
            + _tail(stream_output_file, 200)
            + "\n```\n"
        )
    return (
        base
        + "\n\n---\n\n## Probe inputs\n\n"
        + f"- **Stage**: `{stage}`\n"
        + f"- **Subagent pid**: `{pid}`\n"
        + f"- **Idle duration**: `{idle_sec}s`\n"
        + f"- **Configured idle cap**: `{idle_cap_sec}s`\n"
        + f"- **Stdout path**: `{stdout_path}`\n"
        + f"- **Stderr path**: `{stderr_path}`\n"
        + stream_block
        + "\n"
        + "### Process tree\n\n```\n" + proc_tree + "\n```\n\n"
        + "### Stdout tail (last 200 lines)\n\n```\n" + stdout_tail + "\n```\n\n"
        + "### Stderr tail (last 200 lines)\n\n```\n" + stderr_tail + "\n```\n"
    )


def _parse_verdict(raw: str) -> ProbeVerdict:
    m = VERDICT_RE.search(raw)
    if m is None:
        return ProbeVerdict(
            action="kill", rationale="probe did not emit parseable VERDICT line",
            raw_output=raw,
        )
    if m.group(1).startswith("kill"):
        return ProbeVerdict(action="kill", rationale="probe said kill",
                            raw_output=raw)
    n = int(m.group(2))
    # Clamp to the documented maximum.
    n = min(n, MAX_EXTEND_SEC)
    n = max(n, 1)
    # Collect rationale = everything after the VERDICT line.
    rationale_lines = []
    past_verdict = False
    for line in raw.splitlines():
        if past_verdict:
            rationale_lines.append(line)
            continue
        if VERDICT_RE.match(line):
            past_verdict = True
    return ProbeVerdict(
        action="extend", extend_sec=n,
        rationale="\n".join(rationale_lines).strip(),
        raw_output=raw,
    )


def run_idle_probe(
    *,
    stage: str,
    pid: int,
    idle_sec: int,
    idle_cap_sec: int,
    stdout_path: Path,
    stderr_path: Path,
    probe_timeout_sec: int | None = None,
    probe_config: ProbeConfig,
    vendor_binary: str | None = None,
    stream_output_file: Path | None = None,
    process_registry: Path | None = None,
) -> ProbeVerdict:
    """Ask a short-lived LLM probe whether to extend or kill.

    Defensive semantics: ANY failure (missing CLI, timeout, non-zero
    exit, unparseable output) returns ``kill`` — we default to the
    existing harness behavior rather than silently extending forever.
    """
    # Fast-path hard override (test convenience).
    forced = os.environ.get(FAKE_PROBE_VERDICT_ENV)
    if forced:
        if forced == "kill":
            return ProbeVerdict(action="kill", rationale="forced by env")
        if forced.startswith("extend:"):
            try:
                n = int(forced.split(":", 1)[1])
            except ValueError:
                return ProbeVerdict(action="kill", rationale="forced invalid")
            return ProbeVerdict(
                action="extend", extend_sec=min(max(n, 1), MAX_EXTEND_SEC),
                rationale="forced by env",
            )

    prompt = _compose_prompt(
        stage=stage, pid=pid, idle_sec=idle_sec, idle_cap_sec=idle_cap_sec,
        stdout_path=stdout_path, stderr_path=stderr_path,
        stream_output_file=stream_output_file,
    )

    fake = os.environ.get(FAKE_PROBE_ENV)
    cfg = probe_config
    timeout = probe_timeout_sec if probe_timeout_sec is not None else cfg.timeout_sec
    try:
        if fake:
            proc = subprocess.run(
                [fake], input=prompt, capture_output=True, text=True,
                timeout=timeout,
            )
        else:
            # Quota gate (FAIL-SOFT): the probe is a tiny read-only arbiter, so
            # never let its quota shortage halt the whole feature. If every probe
            # candidate is below its min, degrade to the normal probe-unavailable
            # default ("kill") instead of raising QuotaHalt.
            try:
                chosen = resolve_candidate(build_candidates(cfg), role="probe")
            except QuotaHalt as qh:
                return ProbeVerdict(
                    action="kill",
                    rationale=(
                        f"all {len(qh.diagnostics)} probe candidate(s) below min "
                        f"quota; defaulting to kill (no feature halt)"
                    ),
                )
            same_vendor = chosen.vendor == cfg.vendor
            if (
                same_vendor
                and vendor_binary is not None
                and not Path(vendor_binary).exists()
            ):
                return ProbeVerdict(
                    action="kill",
                    rationale=(
                        f"no {cfg.vendor} binary found for probe; defaulting to kill"
                    ),
                )
            flags_effort, model_override, native_args = split_common_vendor_flags(chosen.flags)
            effort = chosen.effort or flags_effort
            result = call_shared_vendor(
                vendor=chosen.vendor,
                model=model_override or chosen.model,
                prompt=prompt,
                output_id="probe",
                timeout_sec=timeout,
                effort=effort,
                native_args=native_args,
                binary_override=vendor_binary if same_vendor else None,
                process_registry=process_registry,
                process_label=f"probe:{stage}",
            )
            if result.returncode != 0:
                if result.timed_out:
                    return ProbeVerdict(
                        action="kill",
                        rationale=f"probe itself timed out after {timeout}s",
                        raw_output=result.output,
                    )
                return ProbeVerdict(
                    action="kill",
                    rationale=(
                        f"probe exited {result.returncode}; "
                        f"log: {(result.log or result.summary_stderr)[-200:]}"
                    ),
                    raw_output=result.output,
                )
            return _parse_verdict(result.output)
        if proc.returncode != 0:
            return ProbeVerdict(
                action="kill",
                rationale=(
                    f"probe exited {proc.returncode}; "
                    f"stderr: {proc.stderr[-200:]}"
                ),
                raw_output=proc.stdout,
            )
        return _parse_verdict(proc.stdout)
    except subprocess.TimeoutExpired:
        return ProbeVerdict(
            action="kill",
            rationale=f"probe itself timed out after {timeout}s",
        )
    except FileNotFoundError as e:
        return ProbeVerdict(action="kill", rationale=f"probe unavailable: {e}")
