"""Idle-timeout probe adapter (Plan B).

When a caller wants a short-lived LLM judgment on a quiet process, the
probe asks: "is this subagent wedged, or working on a long task?" and
replies ``extend <N>`` or ``kill``.

The evidence prompt (process tree + log tails + stream-file staleness),
the model call, and the verdict parsing all live in the shared vendors
module (``shared/vendors/scripts/idle-probe.sh``) so every
``call.sh`` consumer gets the same probe. This module is the thin
auto-dev adapter around it:

- resolves the probe vendor/model/effort (quota-aware candidate
  fallback, ``ProbeConfig`` flags, binary overrides);
- maps the ``AUTODEV_PROBE_FAKE*`` test hooks onto the shared script's
  ``VENDORS_IDLE_PROBE_FAKE*`` hooks;
- registers the probe process like any other vendor call and parses
  the script's one-line verdict into :class:`ProbeVerdict`.

Defensive failures default to ``kill`` (never extend forever).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from autodev.errors import QuotaHalt
from autodev.vendors.config import ProbeConfig
from autodev.vendors.fallback import build_candidates, resolve_candidate
from autodev.vendors.shared_call import (
    SHARED_VENDORS_DIR,
    _normalize_effort,
    _prepend_cli_override_to_path,
    _tracked_process,
    normalize_shared_vendor,
    split_common_vendor_flags,
)

# Shared implementation: composes the prompt from prompts/idle-probe.md, runs
# call.sh, and prints `extend <N>` | `kill` (+ optional `rationale: ...`).
IDLE_PROBE_SCRIPT = SHARED_VENDORS_DIR / "scripts" / "idle-probe.sh"
PROMPT_FILE = SHARED_VENDORS_DIR / "prompts" / "idle-probe.md"

MAX_EXTEND_SEC = 1800
_VERDICT_LINE_RE = re.compile(r"^(extend\s+(\d+)|kill)$")

# Test override. When set to a fake-invoker path, the probe routes
# through it instead of the real vendor CLI. Same contract as the panel
# fake: receives prompt on stdin, writes verdict to stdout.
FAKE_PROBE_ENV = "AUTODEV_PROBE_FAKE"

# Test override. When set to "extend:N" or "kill", bypass entirely and
# return that verdict without any model call. Useful for pure-unit
# tests that just want to exercise subprocess_runner's hook path.
FAKE_PROBE_VERDICT_ENV = "AUTODEV_PROBE_FAKE_VERDICT"

# The shared script's equivalents of the two hooks above.
SHARED_FAKE_PROBE_ENV = "VENDORS_IDLE_PROBE_FAKE"
SHARED_FAKE_PROBE_VERDICT_ENV = "VENDORS_IDLE_PROBE_FAKE_VERDICT"


@dataclass
class ProbeVerdict:
    action: str          # "extend" or "kill"
    extend_sec: int = 0  # 0 unless action == "extend"
    rationale: str = ""  # free-form reason line (for operator log)
    raw_output: str = ""


def parse_probe_output(raw: str) -> ProbeVerdict:
    """Parse idle-probe.sh's stdout: one verdict line, optional rationale."""
    lines = raw.splitlines()
    first = lines[0].strip() if lines else ""
    rationale = ""
    for line in lines[1:]:
        if line.startswith("rationale:"):
            rationale = line[len("rationale:"):].strip()
            break
    m = _VERDICT_LINE_RE.match(first)
    if m is None:
        return ProbeVerdict(
            action="kill",
            rationale="idle-probe.sh did not emit a parseable verdict",
            raw_output=raw,
        )
    if m.group(1) == "kill":
        return ProbeVerdict(
            action="kill", rationale=rationale or "probe said kill",
            raw_output=raw,
        )
    n = min(max(int(m.group(2)), 1), MAX_EXTEND_SEC)
    return ProbeVerdict(
        action="extend", extend_sec=n, rationale=rationale, raw_output=raw,
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
    """Ask the shared idle probe whether to extend or kill.

    Defensive semantics: ANY failure (missing script or CLI, quota halt,
    timeout, non-zero exit, unparseable output) returns ``kill`` — we
    default to the existing harness behavior rather than silently
    extending forever.
    """
    if not IDLE_PROBE_SCRIPT.exists():
        return ProbeVerdict(
            action="kill",
            rationale=f"probe unavailable: {IDLE_PROBE_SCRIPT} missing",
        )

    cfg = probe_config
    timeout = probe_timeout_sec if probe_timeout_sec is not None else cfg.timeout_sec
    # A watchdog arbiter must itself be bounded: 0/negative would turn the
    # outer wait below (and the script's own call.sh --timeout) into "forever".
    timeout = max(int(timeout), 1)
    env = os.environ.copy()
    cmd = [
        "bash", str(IDLE_PROBE_SCRIPT),
        "--pid", str(pid),
        "--idle-sec", str(max(int(idle_sec), 0)),
        "--idle-cap-sec", str(max(int(idle_cap_sec), 0)),
        "--label", stage,
        "--stdout", str(stdout_path),
        "--stderr", str(stderr_path),
        "--probe-timeout", str(timeout),
    ]
    if stream_output_file is not None:
        cmd.extend(["--stream", str(stream_output_file)])

    forced = os.environ.get(FAKE_PROBE_VERDICT_ENV)
    fake = os.environ.get(FAKE_PROBE_ENV)
    chosen = None
    same_vendor = True
    if forced:
        env[SHARED_FAKE_PROBE_VERDICT_ENV] = forced
    elif fake:
        env[SHARED_FAKE_PROBE_ENV] = fake
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
        cmd.extend(["--probe-vendor", normalize_shared_vendor(chosen.vendor)])
        model = model_override or chosen.model
        if model:
            cmd.extend(["--probe-model", model])
        if effort:
            cmd.extend(["--probe-effort", _normalize_effort(effort)])
        for arg in native_args:
            cmd.extend(["--probe-native-arg", arg])

    with tempfile.TemporaryDirectory(prefix="autodev-idle-probe.") as tmp:
        work_dir = Path(tmp)
        if chosen is not None:
            # Same PATH-shim mechanism call_shared_vendor uses, so an explicit
            # binary or an AUTODEV_VENDOR_BIN_* override reaches the probe's
            # call.sh unchanged.
            _prepend_cli_override_to_path(
                vendor=chosen.vendor,
                explicit_binary=vendor_binary if same_vendor else None,
                work_dir=work_dir,
                env=env,
            )
        cmd.extend(["--output-dir", str(work_dir / "out")])
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
        except OSError as e:
            return ProbeVerdict(action="kill", rationale=f"probe unavailable: {e}")
        with _tracked_process(
            proc,
            process_registry=process_registry,
            label=f"probe:{stage}",
        ):
            try:
                # The script bounds the model call itself; this outer cap only
                # guards against the wrapper wedging.
                out, err = proc.communicate(timeout=timeout + 30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    out, err = proc.communicate()
                return ProbeVerdict(
                    action="kill",
                    rationale=f"probe itself timed out after {timeout}s",
                    raw_output=out or "",
                )
        if proc.returncode != 0:
            return ProbeVerdict(
                action="kill",
                rationale=(
                    f"idle-probe.sh exited {proc.returncode}; "
                    f"stderr: {(err or '')[-200:]}"
                ),
                raw_output=out or "",
            )
        return parse_probe_output(out or "")
