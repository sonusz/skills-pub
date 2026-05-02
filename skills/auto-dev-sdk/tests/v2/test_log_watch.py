"""Tests for --watch / AUTODEV_WATCH stdout alert markers in JsonlLog."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.state.log import JsonlLog


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "log.jsonl"


def test_watch_off_no_stdout(monkeypatch, capsys, log_path):
    monkeypatch.delenv("AUTODEV_WATCH", raising=False)
    JsonlLog(log_path).emit(
        stage="gate", event="panel-done", feature="f",
        detail={"verdict": "pass"},
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    # JSONL still written
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line]
    assert len(rows) == 1


def test_watch_on_alerts_on_panel_done(monkeypatch, capsys, log_path):
    monkeypatch.setenv("AUTODEV_WATCH", "1")
    JsonlLog(log_path).emit(
        stage="gate", event="panel-done", feature="f",
        detail={"verdict": "pass", "gate": "design-review"},
    )
    out = capsys.readouterr().out
    assert "[autodev:gate]" in out
    assert "panel-done" in out
    assert "verdict=pass" in out
    assert "gate=design-review" in out


def test_watch_on_skips_routine_events(monkeypatch, capsys, log_path):
    monkeypatch.setenv("AUTODEV_WATCH", "1")
    log = JsonlLog(log_path)
    # These are intentionally NOT in the alert whitelist.
    log.emit(stage="design", event="subprocess-dispatch", feature="f",
             detail={"vendor": "claude"})
    log.emit(stage="design-packet", event="artifact-written", feature="f")
    log.emit(stage="ralph-review", event="iteration-recorded", feature="f")
    assert capsys.readouterr().out == ""


def test_watch_on_alerts_on_subprocess_failed_any_stage(monkeypatch, capsys, log_path):
    monkeypatch.setenv("AUTODEV_WATCH", "1")
    JsonlLog(log_path).emit(
        stage="ralph-review", event="subprocess-failed", feature="f",
        detail={"kind": "missing_artifact"},
    )
    out = capsys.readouterr().out
    assert "subprocess-failed" in out
    assert "kind=missing_artifact" in out


def test_watch_on_alerts_on_revision_loop_halt(monkeypatch, capsys, log_path):
    monkeypatch.setenv("AUTODEV_WATCH", "1")
    JsonlLog(log_path).emit(
        stage="gate", event="revision-loop-halt", feature="f",
        detail={"gate": "design-review", "reason": "L_MAX reached"},
    )
    out = capsys.readouterr().out
    assert "revision-loop-halt" in out
    assert "gate=design-review" in out
    assert "reason=L_MAX reached" in out


def test_watch_on_truncates_long_reason(monkeypatch, capsys, log_path):
    monkeypatch.setenv("AUTODEV_WATCH", "1")
    long_reason = "x" * 200
    JsonlLog(log_path).emit(
        stage="gate", event="revision-loop-halt", feature="f",
        detail={"reason": long_reason},
    )
    out = capsys.readouterr().out
    # Truncated to 80 chars (77 + "...")
    assert "x" * 77 + "..." in out
    assert "x" * 100 not in out


def test_watch_value_other_than_1_is_off(monkeypatch, capsys, log_path):
    monkeypatch.setenv("AUTODEV_WATCH", "true")  # not the literal "1"
    JsonlLog(log_path).emit(
        stage="gate", event="panel-done", feature="f", detail={"verdict": "pass"},
    )
    assert capsys.readouterr().out == ""
