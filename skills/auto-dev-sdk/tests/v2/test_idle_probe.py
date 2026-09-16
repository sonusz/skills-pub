"""Idle-timeout probe tests (Plan B).

The probe's prompt composition, model call, and verdict parsing live in
``shared/vendors/scripts/idle-probe.sh``; ``autodev.vendors.probe_agent``
is a thin adapter around it. Prompt and verdict behavior is therefore
tested against the script itself (``--compose-only`` and the
``VENDORS_IDLE_PROBE_FAKE`` hook); the adapter tests cover env-hook
mapping, vendor resolution, and the ``subprocess_runner`` wiring.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from autodev.vendors import probe_agent
from autodev.vendors.probe_agent import (
    FAKE_PROBE_ENV, FAKE_PROBE_VERDICT_ENV, IDLE_PROBE_SCRIPT, MAX_EXTEND_SEC,
    PROMPT_FILE, SHARED_FAKE_PROBE_ENV, SHARED_FAKE_PROBE_VERDICT_ENV,
    ProbeVerdict, parse_probe_output, run_idle_probe,
)
from autodev.vendors.config import ProbeConfig
from autodev.vendors import subprocess_runner

PROBE_CONFIG = ProbeConfig(vendor="claude", model="fake-probe")
_HOOK_ENVS = (
    FAKE_PROBE_ENV, FAKE_PROBE_VERDICT_ENV,
    SHARED_FAKE_PROBE_ENV, SHARED_FAKE_PROBE_VERDICT_ENV,
)


def _run_probe(**kwargs):
    kwargs.setdefault("probe_config", PROBE_CONFIG)
    return run_idle_probe(**kwargs)


def _fake_script(tmp_path: Path, body: str, name: str = "fake_probe.sh") -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + body + "\n")
    p.chmod(0o755)
    return p


def _script_args(tmp_path: Path, **overrides) -> list[str]:
    args = {
        "pid": "99999", "idle-sec": "500", "idle-cap-sec": "300", "label": "test",
        "stdout": str(tmp_path / "s.log"), "stderr": str(tmp_path / "e.log"),
    }
    args.update({k.replace("_", "-"): str(v) for k, v in overrides.items()})
    out: list[str] = []
    for key, value in args.items():
        out += [f"--{key}", value]
    return out


def _run_script(args: list[str], env: dict[str, str] | None = None, timeout: int = 60):
    """Run idle-probe.sh with the ambient test hooks stripped."""
    clean = {k: v for k, v in os.environ.items() if k not in _HOOK_ENVS}
    if env:
        clean.update(env)
    return subprocess.run(
        ["bash", str(IDLE_PROBE_SCRIPT), *args],
        capture_output=True, text=True, env=clean, timeout=timeout,
    )


def _verdict_via_fake(tmp_path: Path, answer: str) -> list[str]:
    """Feed a canned model answer through the script's fake-invoker hook."""
    answer_file = tmp_path / "answer.txt"
    answer_file.write_text(answer)
    script = _fake_script(tmp_path, f"cat > /dev/null; cat '{answer_file}'")
    proc = _run_script(_script_args(tmp_path), env={SHARED_FAKE_PROBE_ENV: str(script)})
    # Every verdict, including fail-closed kills, exits 0.
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def _compose(tmp_path: Path, stream: Path | None = None) -> str:
    args = ["--compose-only", *_script_args(
        tmp_path, pid=1234, idle_sec=120, idle_cap_sec=600, label="design",
    )]
    if stream is not None:
        args += ["--stream", str(stream)]
    proc = _run_script(args)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _tree_section(prompt: str) -> str:
    m = re.search(r"### Process tree\n\n```\n(.*?)\n```\n", prompt, re.S)
    assert m is not None, prompt
    return m.group(1)


# ---------- verdict parsing (idle-probe.sh) ----------

def test_script_verdict_extend_numeric(tmp_path):
    lines = _verdict_via_fake(tmp_path, "VERDICT: extend 600\nrationale line\n")
    assert lines[0] == "extend 600"
    assert lines[1] == "rationale: rationale line"


def test_script_verdict_kill(tmp_path):
    lines = _verdict_via_fake(tmp_path, "VERDICT: kill\nbecause stuck\n")
    assert lines[0] == "kill"
    assert "because stuck" in lines[1]


def test_script_verdict_unparseable_defaults_to_kill(tmp_path):
    lines = _verdict_via_fake(tmp_path, "this is not a verdict\n")
    assert lines[0] == "kill"
    assert "parseable" in lines[1].lower()


def test_script_verdict_clamps_to_max(tmp_path):
    lines = _verdict_via_fake(tmp_path, "VERDICT: extend 999999\n")
    assert lines[0] == f"extend {MAX_EXTEND_SEC}"


def test_script_verdict_minimum_one_second(tmp_path):
    # 0 -> clamped up to 1 (avoids returning a no-op extend)
    lines = _verdict_via_fake(tmp_path, "VERDICT: extend 0\n")
    assert lines[0] == "extend 1"


def test_script_verdict_whitespace_tolerant(tmp_path):
    lines = _verdict_via_fake(tmp_path, "  VERDICT:   extend   120  \n")
    assert lines[0] == "extend 120"


def test_script_verdict_rationale_only_after_verdict_line(tmp_path):
    lines = _verdict_via_fake(tmp_path, "preamble\nVERDICT: extend 10\nrationale\n")
    assert lines[0] == "extend 10"
    assert lines[1] == "rationale: rationale"
    assert "preamble" not in lines[1]


def test_script_fake_nonzero_exit_defaults_to_kill(tmp_path):
    script = _fake_script(tmp_path, "cat > /dev/null; echo boom >&2; exit 3")
    proc = _run_script(_script_args(tmp_path), env={SHARED_FAKE_PROBE_ENV: str(script)})
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert lines[0] == "kill"
    assert "exited 3" in lines[1]


def test_script_no_probe_vendor_defaults_to_kill(tmp_path):
    proc = _run_script(_script_args(tmp_path))
    assert proc.returncode == 0
    assert proc.stdout.splitlines()[0] == "kill"


def test_script_forced_verdict_env(tmp_path):
    proc = _run_script(_script_args(tmp_path), env={SHARED_FAKE_PROBE_VERDICT_ENV: "extend:300"})
    assert proc.stdout.splitlines()[0] == "extend 300"
    proc = _run_script(_script_args(tmp_path), env={SHARED_FAKE_PROBE_VERDICT_ENV: "extend:nope"})
    assert proc.stdout.splitlines()[0] == "kill"


# ---------- parse_probe_output (adapter side) ----------

def test_parse_probe_output_extend_with_rationale():
    v = parse_probe_output("extend 600\nrationale: still compiling\n")
    assert v.action == "extend"
    assert v.extend_sec == 600
    assert v.rationale == "still compiling"


def test_parse_probe_output_kill():
    v = parse_probe_output("kill\nrationale: wedged\n")
    assert v.action == "kill"
    assert v.extend_sec == 0
    assert v.rationale == "wedged"


def test_parse_probe_output_garbage_defaults_to_kill():
    v = parse_probe_output("not a verdict")
    assert v.action == "kill"
    assert "parseable" in v.rationale.lower()


def test_parse_probe_output_clamps_defensively():
    # The script already clamps; the adapter must not trust that blindly.
    assert parse_probe_output("extend 999999\n").extend_sec == MAX_EXTEND_SEC
    assert parse_probe_output("extend 0\n").extend_sec == 1


# ---------- env-forced verdict (AUTODEV_* hooks mapped to the script) ----------

def test_forced_verdict_extend(monkeypatch, tmp_path):
    monkeypatch.setenv(FAKE_PROBE_VERDICT_ENV, "extend:300")
    v = _run_probe(
        stage="scope", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
    )
    assert v.action == "extend"
    assert v.extend_sec == 300


def test_forced_verdict_kill(monkeypatch, tmp_path):
    monkeypatch.setenv(FAKE_PROBE_VERDICT_ENV, "kill")
    v = _run_probe(
        stage="scope", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
    )
    assert v.action == "kill"


def test_forced_verdict_malformed_defaults_to_kill(monkeypatch, tmp_path):
    monkeypatch.setenv(FAKE_PROBE_VERDICT_ENV, "extend:not-a-number")
    v = _run_probe(
        stage="scope", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
    )
    assert v.action == "kill"


def test_forced_verdict_extend_clamped(monkeypatch, tmp_path):
    monkeypatch.setenv(FAKE_PROBE_VERDICT_ENV, "extend:10000")
    v = _run_probe(
        stage="scope", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
    )
    assert v.extend_sec == MAX_EXTEND_SEC


# ---------- fake invoker (AUTODEV_PROBE_FAKE -> VENDORS_IDLE_PROBE_FAKE) ----------

def test_fake_invoker_extend(monkeypatch, tmp_path):
    script = _fake_script(tmp_path, "cat > /dev/null; echo 'VERDICT: extend 450'")
    monkeypatch.setenv("AUTODEV_PROBE_FAKE", str(script))
    v = _run_probe(
        stage="plan", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
    )
    assert v.action == "extend"
    assert v.extend_sec == 450


def test_fake_invoker_kill(monkeypatch, tmp_path):
    script = _fake_script(tmp_path, "cat > /dev/null; echo 'VERDICT: kill'")
    monkeypatch.setenv("AUTODEV_PROBE_FAKE", str(script))
    v = _run_probe(
        stage="plan", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
    )
    assert v.action == "kill"


def test_fake_invoker_nonzero_exit_defaults_to_kill(monkeypatch, tmp_path):
    script = _fake_script(tmp_path, "cat > /dev/null; exit 1")
    monkeypatch.setenv("AUTODEV_PROBE_FAKE", str(script))
    v = _run_probe(
        stage="plan", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
    )
    assert v.action == "kill"
    assert "exit" in v.rationale.lower()


def test_fake_invoker_empty_output_defaults_to_kill(monkeypatch, tmp_path):
    script = _fake_script(tmp_path, "cat > /dev/null")  # no stdout
    monkeypatch.setenv("AUTODEV_PROBE_FAKE", str(script))
    v = _run_probe(
        stage="plan", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
    )
    assert v.action == "kill"


def test_fake_invoker_receives_prompt_on_stdin(monkeypatch, tmp_path):
    """Sanity: the probe prompt includes process tree + log tail sections."""
    received = tmp_path / "received_prompt.txt"
    script = _fake_script(tmp_path, f"cat > {received}; echo 'VERDICT: kill'")
    monkeypatch.setenv("AUTODEV_PROBE_FAKE", str(script))
    stdout_log = tmp_path / "s.log"
    stdout_log.write_text("Running tests...\n" * 10)
    _run_probe(
        stage="build", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=stdout_log, stderr_path=tmp_path / "e.log",
    )
    body = received.read_text()
    assert "Process tree" in body
    assert "Stdout tail" in body
    assert "Stderr tail" in body
    assert "Running tests" in body


def test_adapter_forwards_label_stream_and_idle_numbers(monkeypatch, tmp_path):
    received = tmp_path / "received_prompt.txt"
    script = _fake_script(tmp_path, f"cat > {received}; echo 'VERDICT: kill'")
    monkeypatch.setenv("AUTODEV_PROBE_FAKE", str(script))
    stream = tmp_path / "out"
    stream.write_text("x" * 42)
    _run_probe(
        stage="build", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
        stream_output_file=stream,
    )
    body = received.read_text()
    assert "**Stage**: `build`" in body
    assert "**Idle duration**: `500s`" in body
    assert "**Configured idle cap**: `300s`" in body
    assert f"Stream output file path**: `{stream}`" in body
    assert "Stream output file size_bytes**: `42`" in body


def test_adapter_missing_script_defaults_to_kill(monkeypatch, tmp_path):
    monkeypatch.setattr(probe_agent, "IDLE_PROBE_SCRIPT", tmp_path / "no-such-idle-probe.sh")
    v = _run_probe(
        stage="plan", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
    )
    assert v.action == "kill"
    assert "unavailable" in v.rationale


# ---------- process tree (macOS/BSD portability regression) ----------

def test_compose_only_process_tree_includes_descendants(tmp_path):
    # Regression guard for the macOS bug: the old `ps --forest -o ...,cmd`
    # form is GNU-only and failed on BSD ps ("illegal option -- -" /
    # "cmd: keyword not found"), so the tree degraded to a sentinel and
    # starved the idle probe -> false-positive kill. A live process with a
    # child must yield a real tree that includes the child.
    child = subprocess.Popen(["sleep", "30"])
    try:
        time.sleep(0.3)
        proc = _run_script(["--compose-only", *_script_args(tmp_path, pid=os.getpid())])
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert proc.returncode == 0, proc.stderr
    tree = _tree_section(proc.stdout)
    assert "could not read" not in tree
    assert str(os.getpid()) in tree
    assert str(child.pid) in tree or "sleep 30" in tree


def test_compose_only_dead_pid_is_graceful(tmp_path):
    # A pid that is not alive must not fail composition and must not surface
    # that pid as a process row (the probe sees a headers-only tree).
    dead = 2_000_000_000
    proc = _run_script(["--compose-only", *_script_args(tmp_path, pid=dead)])
    assert proc.returncode == 0, proc.stderr
    assert str(dead) not in _tree_section(proc.stdout)


def test_probe_implementation_lives_in_shared_vendors():
    # The portable implementation must live in shared/vendors so every
    # shared-vendors consumer inherits one tested, cross-platform version.
    assert IDLE_PROBE_SCRIPT.name == "idle-probe.sh"
    assert "shared" in IDLE_PROBE_SCRIPT.parts
    assert "vendors" in IDLE_PROBE_SCRIPT.parts
    assert IDLE_PROBE_SCRIPT.exists()
    assert PROMPT_FILE.exists()
    text = IDLE_PROBE_SCRIPT.read_text(encoding="utf-8")
    code_lines = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    assert any("process-tree.sh" in l for l in code_lines)
    assert any("ps -o pid,ppid,state,etime,command -p" in l for l in code_lines)
    # GNU-only ps forms must never come back (they fail on BSD/macOS ps).
    assert not any("--forest" in l or "etimes" in l for l in code_lines)


def test_real_probe_vendor_path_receives_prompt_on_stdin(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTODEV_PROBE_FAKE", raising=False)
    monkeypatch.delenv("AUTODEV_PROBE_FAKE_VERDICT", raising=False)
    received = tmp_path / "received_prompt.txt"
    args_file = tmp_path / "args.txt"
    script = _fake_script(
        tmp_path,
        f"printf '%s\\n' \"$@\" > {args_file}; cat > {received}; echo 'VERDICT: kill'",
    )
    stdout_log = tmp_path / "s.log"
    stdout_log.write_text("Working...\n")
    v = _run_probe(
        stage="build", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=stdout_log, stderr_path=tmp_path / "e.log",
        vendor_binary=str(script),
    )
    assert v.action == "kill"
    assert "Probe inputs" in received.read_text()
    assert "Working" in received.read_text()
    assert "Probe inputs" not in args_file.read_text()


def test_probe_can_use_codex_config(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTODEV_PROBE_FAKE", raising=False)
    monkeypatch.delenv("AUTODEV_PROBE_FAKE_VERDICT", raising=False)
    received = tmp_path / "received_prompt.txt"
    args_file = tmp_path / "args.txt"
    script = _fake_script(
        tmp_path,
        f"printf '%s\\n' \"$@\" > {args_file}; cat > {received}; echo 'VERDICT: kill'",
    )
    stdout_log = tmp_path / "s.log"
    stdout_log.write_text("Working...\n")
    v = _run_probe(
        stage="build", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=stdout_log, stderr_path=tmp_path / "e.log",
        vendor_binary=str(script),
        probe_config=ProbeConfig(vendor="codex", model="gpt-5.6-luna"),
    )
    assert v.action == "kill"
    args = args_file.read_text()
    assert "exec" in args
    assert "gpt-5.6-luna" in args
    assert "Probe inputs" in received.read_text()


# ---------- no vendor binary available ----------

def test_no_vendor_binary_defaults_to_kill(monkeypatch, tmp_path):
    # Clear fake-invoker + fake-verdict overrides.
    monkeypatch.delenv("AUTODEV_PROBE_FAKE", raising=False)
    monkeypatch.delenv("AUTODEV_PROBE_FAKE_VERDICT", raising=False)
    v = _run_probe(
        stage="scope", pid=99999, idle_sec=500, idle_cap_sec=300,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
        vendor_binary="/does/not/exist/claude",
    )
    # Missing binary path -> kill.
    assert v.action == "kill"


# ---------- ProbeVerdict dataclass ----------

def test_probe_verdict_defaults():
    v = ProbeVerdict(action="kill")
    assert v.extend_sec == 0
    assert v.rationale == ""
    assert v.raw_output == ""


# ---------- compose prompt (idle-probe.sh --compose-only) ----------

def test_compose_prompt_has_base_prompt_and_inputs(tmp_path):
    (tmp_path / "s.log").write_text("Running tests...\n")
    (tmp_path / "e.log").write_text("warn: slow\n")
    p = _compose(tmp_path)
    assert p.startswith(PROMPT_FILE.read_text(encoding="utf-8"))
    assert "## Probe inputs" in p
    assert "**Stage**: `design`" in p
    assert "**Subagent pid**: `1234`" in p
    assert "**Idle duration**: `120s`" in p
    assert "**Configured idle cap**: `600s`" in p
    assert "### Stdout tail (last 200 lines)\n\n```\nRunning tests...\n```" in p
    assert "### Stderr tail (last 200 lines)\n\n```\nwarn: slow\n```" in p


def test_compose_prompt_missing_logs_use_sentinel(tmp_path):
    p = _compose(tmp_path)
    assert p.count("(file not present)") == 2


def test_compose_prompt_omits_stream_block_when_no_file(tmp_path):
    p = _compose(tmp_path, stream=None)
    assert "Stream output file path" not in p
    assert "Stream output file size_bytes" not in p
    assert "Stream output file seconds_since_modified" not in p


def test_compose_prompt_includes_stream_block_for_existing_file(tmp_path):
    stream = tmp_path / "out"
    stream.write_text("a" * 100, encoding="utf-8")
    p = _compose(tmp_path, stream=stream)
    assert f"Stream output file path**: `{stream}`" in p
    assert "Stream output file size_bytes**: `100`" in p
    assert "Stream output file seconds_since_modified" in p
    assert "### Stream output tail (last 200 lines)\n\n```\n" + "a" * 100 + "\n```" in p


def test_compose_prompt_includes_stream_block_for_missing_file(tmp_path):
    # Caller may pass a path that does not yet exist (vendor CLI has
    # not written its first chunk). The probe should still see the
    # block so it can reason about "stream never produced anything".
    stream = tmp_path / "no-such-out"
    p = _compose(tmp_path, stream=stream)
    assert f"Stream output file path**: `{stream}`" in p
    assert "Stream output file size_bytes**: `0`" in p
    # Without an mtime the idle duration stands in for staleness.
    assert "Stream output file seconds_since_modified**: `120s`" in p


# ---------- _build_idle_callback wiring ----------

_TEST_PROBE_INTERVAL_SEC = 600


def _build_cb(monkeypatch, **overrides):
    """Construct an idle-watch callback with reasonable test defaults."""
    defaults = dict(
        stage="design",
        stage_probe_interval_sec=_TEST_PROBE_INTERVAL_SEC,
        stdout_path=Path("/tmp/.design.stdout.log"),
        stderr_path=Path("/tmp/.design.stderr.log"),
        probe_config=PROBE_CONFIG,
        log_emit=None,
    )
    defaults.update(overrides)
    return subprocess_runner._build_idle_callback(**defaults)


def test_idle_callback_below_threshold_skips_probe(monkeypatch, tmp_path):
    # The callback short-circuits when idle_sec < the per-stage
    # probe_interval_sec; it must NOT spend a probe call in that case.
    called = {"n": 0}

    def stub(**kwargs):
        called["n"] += 1
        return ProbeVerdict(action="kill", rationale="should not be called")

    monkeypatch.setattr(subprocess_runner, "run_idle_probe", stub)
    cb = _build_cb(monkeypatch)
    action = cb(
        stream_file=tmp_path / "out",
        idle_sec=_TEST_PROBE_INTERVAL_SEC - 1,
        elapsed_sec=200.0,
        pid=4321,
    )
    assert action == "continue"
    assert called["n"] == 0


def test_idle_callback_kill_verdict_propagates(monkeypatch, tmp_path):
    seen_kwargs: dict = {}

    def stub(**kwargs):
        seen_kwargs.update(kwargs)
        return ProbeVerdict(action="kill", rationale="wedged")

    monkeypatch.setattr(subprocess_runner, "run_idle_probe", stub)
    cb = _build_cb(monkeypatch)
    stream = tmp_path / "out"
    action = cb(
        stream_file=stream,
        idle_sec=_TEST_PROBE_INTERVAL_SEC + 5,
        elapsed_sec=400.0,
        pid=4321,
    )
    assert action == "kill"
    assert seen_kwargs["stage"] == "design"
    assert seen_kwargs["stream_output_file"] == stream
    assert seen_kwargs["idle_cap_sec"] == _TEST_PROBE_INTERVAL_SEC


def test_idle_callback_extend_holds_grace_then_reprobes(monkeypatch, tmp_path):
    verdicts = [
        ProbeVerdict(action="extend", extend_sec=120, rationale="working"),
        ProbeVerdict(action="kill", rationale="now wedged"),
    ]
    call_count = {"n": 0}

    def stub(**kwargs):
        call_count["n"] += 1
        return verdicts[call_count["n"] - 1]

    monkeypatch.setattr(subprocess_runner, "run_idle_probe", stub)

    # Pin time.monotonic so we can step through the extend window.
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(
        subprocess_runner.time, "monotonic", lambda: fake_now["t"]
    )

    cb = _build_cb(monkeypatch)
    above = _TEST_PROBE_INTERVAL_SEC + 5

    # First call: probe says extend 120s -> callback returns continue.
    a1 = cb(stream_file=tmp_path / "out", idle_sec=above, elapsed_sec=400.0, pid=1)
    assert a1 == "continue"
    assert call_count["n"] == 1

    # 60s later: still inside the granted 120s window -> no probe call.
    fake_now["t"] += 60
    a2 = cb(stream_file=tmp_path / "out", idle_sec=above, elapsed_sec=460.0, pid=1)
    assert a2 == "continue"
    assert call_count["n"] == 1

    # 121s after extend granted: window elapsed -> probe re-invoked, kill.
    fake_now["t"] += 61
    a3 = cb(stream_file=tmp_path / "out", idle_sec=above, elapsed_sec=521.0, pid=1)
    assert a3 == "kill"
    assert call_count["n"] == 2


def test_idle_callback_swallows_probe_exception(monkeypatch, tmp_path):
    # If the probe machinery itself raises (e.g. a transient OS error
    # while reading the process tree), we must NOT abort the run.
    def bad(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(subprocess_runner, "run_idle_probe", bad)
    cb = _build_cb(monkeypatch)
    action = cb(
        stream_file=tmp_path / "out",
        idle_sec=_TEST_PROBE_INTERVAL_SEC + 5,
        elapsed_sec=400.0,
        pid=1,
    )
    assert action == "continue"
