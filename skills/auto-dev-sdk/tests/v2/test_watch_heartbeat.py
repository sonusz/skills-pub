"""Tests for the harness-owned watch heartbeat and terminal protocol."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import autodev.cli as cli
from autodev.watch import WatchSession, heartbeat_interval


def test_watch_session_emits_started_heartbeat_and_terminal(capsys):
    watch = WatchSession(feature="demo", verb="run", interval_sec=0.01)
    watch.start()
    time.sleep(0.035)
    watch.finish(2)

    lines = capsys.readouterr().out.splitlines()
    assert any("[autodev:watch] started" in line for line in lines)
    assert any("[autodev:watch] heartbeat" in line for line in lines)
    terminal = [line for line in lines if "[autodev:watch] terminal" in line]
    assert len(terminal) == 1
    assert "outcome=gate_pending" in terminal[0]
    assert "exit_code=2" in terminal[0]


@pytest.mark.parametrize(
    ("exit_code", "outcome"),
    [(0, "complete"), (1, "error"), (2, "gate_pending"),
     (3, "lock_conflict"), (9, "error")],
)
def test_terminal_outcome_mapping(capsys, exit_code, outcome):
    watch = WatchSession(feature="demo", verb="next", interval_sec=60)
    watch.start()
    watch.finish(exit_code)
    watch.finish(exit_code)

    terminal = [
        line for line in capsys.readouterr().out.splitlines()
        if "[autodev:watch] terminal" in line
    ]
    assert len(terminal) == 1
    assert f"outcome={outcome}" in terminal[0]


def test_heartbeat_interval_env(monkeypatch):
    monkeypatch.setenv("AUTODEV_WATCH_HEARTBEAT_SEC", "12.5")
    assert heartbeat_interval() == 12.5
    monkeypatch.setenv("AUTODEV_WATCH_HEARTBEAT_SEC", "invalid")
    assert heartbeat_interval() == 60.0
    monkeypatch.setenv("AUTODEV_WATCH_HEARTBEAT_SEC", "0")
    assert heartbeat_interval() == 60.0


def test_nonpositive_explicit_interval_falls_back(monkeypatch):
    monkeypatch.setenv("AUTODEV_WATCH_HEARTBEAT_SEC", "5")
    watch = WatchSession(feature="demo", verb="run", interval_sec=-1)
    assert watch.interval_sec == 5


def test_cli_watch_wraps_dispatch_with_terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_dispatch_orch", lambda args, verb: 2)
    args = SimpleNamespace(watch=True, feature="demo")

    assert cli._dispatch_with_watch(args, "run") == 2

    output = capsys.readouterr().out
    assert "[autodev:watch] started" in output
    assert "feature=demo" in output
    assert "[autodev:watch] terminal" in output
    assert "outcome=gate_pending" in output


def test_cli_without_watch_delegates_without_markers(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_dispatch_orch", lambda args, verb: 0)
    args = SimpleNamespace(watch=False, feature="demo")

    assert cli._dispatch_with_watch(args, "next") == 0
    assert "[autodev:watch]" not in capsys.readouterr().out
