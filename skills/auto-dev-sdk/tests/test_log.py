"""ad-1: JSONL log."""
from __future__ import annotations

import json

from auto_dev.state.log import JsonlLog


def test_emit_creates_file(tmp_path):
    log = JsonlLog(tmp_path / "log.jsonl")
    log.emit(stage="build", event="done", feature="foo", detail={"n": 1})
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["stage"] == "build"
    assert ev["feature"] == "foo"
    assert ev["detail"] == {"n": 1}
    assert ev["schema"] == 1


def test_tail_returns_last_n(tmp_path):
    log = JsonlLog(tmp_path / "log.jsonl")
    for i in range(5):
        log.emit(stage="x", event=f"e{i}", feature="foo")
    assert [e["event"] for e in log.tail(3)] == ["e2", "e3", "e4"]


def test_tail_on_missing_file(tmp_path):
    log = JsonlLog(tmp_path / "missing.jsonl")
    assert log.tail() == []
