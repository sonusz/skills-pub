"""Idle-timeout probe agent tests (Plan B)."""
from __future__ import annotations

from pathlib import Path

import pytest

from autodev.vendors.probe_agent import (
    FAKE_PROBE_VERDICT_ENV, MAX_EXTEND_SEC, ProbeVerdict, _parse_verdict,
    run_idle_probe,
)
from autodev.vendors.config import ProbeConfig

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
        probe_config=ProbeConfig(vendor="codex", model="gpt-5.4"),
    )
    assert v.action == "kill"
    args = args_file.read_text()
    assert "exec" in args
    assert "gpt-5.4" in args
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
