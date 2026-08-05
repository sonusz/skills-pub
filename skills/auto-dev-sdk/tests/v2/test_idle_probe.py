"""Idle-timeout probe agent tests (Plan B)."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from autodev.vendors.probe_agent import (
    FAKE_PROBE_VERDICT_ENV, MAX_EXTEND_SEC, PROCESS_TREE_SCRIPT, ProbeVerdict,
    _compose_prompt, _parse_verdict, _process_tree, run_idle_probe,
)
from autodev.vendors.config import ProbeConfig
from autodev.vendors import subprocess_runner

PROBE_CONFIG = ProbeConfig(vendor="claude", model="fake-probe")


def _run_probe(**kwargs):
    kwargs.setdefault("probe_config", PROBE_CONFIG)
    return run_idle_probe(**kwargs)


# ---------- _parse_verdict ----------

def test_parse_verdict_extend_numeric():
    v = _parse_verdict("VERDICT: extend 600\nrationale line")
    assert v.action == "extend"
    assert v.extend_sec == 600
    assert "rationale line" in v.rationale


def test_parse_verdict_kill():
    v = _parse_verdict("VERDICT: kill\nbecause stuck")
    assert v.action == "kill"
    assert v.extend_sec == 0


def test_parse_verdict_unparseable_defaults_to_kill():
    v = _parse_verdict("this is not a verdict")
    assert v.action == "kill"
    assert "parseable" in v.rationale.lower()


def test_parse_verdict_clamps_to_max():
    v = _parse_verdict("VERDICT: extend 999999\n")
    assert v.action == "extend"
    assert v.extend_sec == MAX_EXTEND_SEC


def test_parse_verdict_minimum_one_second():
    v = _parse_verdict("VERDICT: extend 0\n")
    # 0 → clamped up to 1 (avoids returning a no-op extend)
    assert v.extend_sec == 1


def test_parse_verdict_whitespace_tolerant():
    v = _parse_verdict("  VERDICT:   extend   120  \n")
    assert v.action == "extend"
    assert v.extend_sec == 120


def test_parse_verdict_rationale_only_after_verdict_line():
    text = "preamble\nVERDICT: extend 10\nrationale\n"
    v = _parse_verdict(text)
    assert "preamble" not in v.rationale
    assert "rationale" in v.rationale


# ---------- env-forced verdict ----------

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


# ---------- fake invoker (subprocess path) ----------

def _fake_script(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fake_probe.sh"
    p.write_text("#!/bin/bash\n" + body + "\n")
    p.chmod(0o755)
    return p


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


# ---------- _process_tree (macOS/BSD portability regression) ----------

def test_process_tree_returns_real_tree_with_descendants():
    # Regression guard for the macOS bug: the old `ps --forest -o ...,cmd`
    # form is GNU-only and failed on BSD ps ("illegal option -- -" /
    # "cmd: keyword not found"), so _process_tree returned the degraded
    # sentinel and starved the idle probe -> false-positive kill. A live
    # process with a child must now yield a real tree that includes the child.
    child = subprocess.Popen(["sleep", "30"])
    try:
        time.sleep(0.3)
        tree = _process_tree(os.getpid())
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert "could not read" not in tree
    assert str(os.getpid()) in tree
    assert str(child.pid) in tree or "sleep 30" in tree


def test_process_tree_dead_pid_is_graceful():
    # A pid that is not alive must not raise and must not surface that pid
    # as a process row (the probe should see an empty/headers-only tree).
    dead = 2_000_000_000
    tree = _process_tree(dead)
    assert isinstance(tree, str)
    assert str(dead) not in tree


def test_process_tree_primary_path_is_the_shared_helper():
    # The portable implementation must live in shared/vendors so every
    # shared-vendors consumer inherits one tested, cross-platform version.
    assert PROCESS_TREE_SCRIPT.name == "process-tree.sh"
    assert "shared" in PROCESS_TREE_SCRIPT.parts
    assert "vendors" in PROCESS_TREE_SCRIPT.parts


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
    # FileNotFoundError path → kill.
    assert v.action == "kill"


# ---------- ProbeVerdict dataclass ----------

def test_probe_verdict_defaults():
    v = ProbeVerdict(action="kill")
    assert v.extend_sec == 0
    assert v.rationale == ""
    assert v.raw_output == ""


# ---------- compose prompt with stream output file ----------

def test_compose_prompt_omits_stream_block_when_no_file(tmp_path):
    p = _compose_prompt(
        stage="design", pid=1234, idle_sec=120, idle_cap_sec=600,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
        stream_output_file=None,
    )
    assert "Stream output file path" not in p
    assert "Stream output file size_bytes" not in p
    assert "Stream output file seconds_since_modified" not in p


def test_compose_prompt_includes_stream_block_for_existing_file(tmp_path):
    stream = tmp_path / "out"
    stream.write_text("a" * 100, encoding="utf-8")
    p = _compose_prompt(
        stage="design", pid=1234, idle_sec=120, idle_cap_sec=600,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
        stream_output_file=stream,
    )
    assert f"Stream output file path**: `{stream}`" in p
    assert "Stream output file size_bytes**: `100`" in p
    assert "Stream output file seconds_since_modified" in p


def test_compose_prompt_includes_stream_block_for_missing_file(tmp_path):
    # Caller may pass a path that does not yet exist (vendor CLI has
    # not written its first chunk). The probe should still see the
    # block so it can reason about "stream never produced anything".
    stream = tmp_path / "no-such-out"
    p = _compose_prompt(
        stage="design", pid=1234, idle_sec=180, idle_cap_sec=600,
        stdout_path=tmp_path / "s.log", stderr_path=tmp_path / "e.log",
        stream_output_file=stream,
    )
    assert f"Stream output file path**: `{stream}`" in p
    assert "Stream output file size_bytes**: `0`" in p


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

    # First call: probe says extend 120s → callback returns continue.
    a1 = cb(stream_file=tmp_path / "out", idle_sec=above, elapsed_sec=400.0, pid=1)
    assert a1 == "continue"
    assert call_count["n"] == 1

    # 60s later: still inside the granted 120s window → no probe call.
    fake_now["t"] += 60
    a2 = cb(stream_file=tmp_path / "out", idle_sec=above, elapsed_sec=460.0, pid=1)
    assert a2 == "continue"
    assert call_count["n"] == 1

    # 121s after extend granted: window elapsed → probe re-invoked, kill.
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
