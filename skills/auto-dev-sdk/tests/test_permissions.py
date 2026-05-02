"""ad-6: permission rules + tool executor."""
from __future__ import annotations

import pytest

from auto_dev.errors import PermissionDenied
from auto_dev.executor.permissions import PermissionRules
from auto_dev.executor.tools import ToolExecutor


def test_allow_glob_permits_matching_bash():
    rules = PermissionRules(allow_cmd=["pytest *"])
    assert rules.check_cmd("pytest tests/").allowed
    assert not rules.check_cmd("rm -rf /").allowed


def test_deny_overrides_allow():
    rules = PermissionRules(allow_cmd=["*"], deny_cmd=["rm *"])
    assert not rules.check_cmd("rm -rf /").allowed
    assert rules.check_cmd("ls").allowed


def test_regex_rule():
    rules = PermissionRules(allow_cmd=[r"re:^pytest( .*)?$"])
    assert rules.check_cmd("pytest").allowed
    assert rules.check_cmd("pytest tests/x.py").allowed
    assert not rules.check_cmd("pytestfoo").allowed  # regex anchored


def test_unmatched_command_denied_by_default():
    rules = PermissionRules()
    assert not rules.check_cmd("ls").allowed


def test_executor_denies_and_raises(tmp_path):
    rules = PermissionRules(allow_cmd=["echo *"])
    ex = ToolExecutor(rules, cwd=tmp_path)
    with pytest.raises(PermissionDenied):
        ex.dispatch("bash", {"cmd": "cat /etc/passwd"})


def test_executor_bash_ok(tmp_path):
    rules = PermissionRules(allow_cmd=["echo *"])
    ex = ToolExecutor(rules, cwd=tmp_path)
    result = ex.dispatch("bash", {"cmd": "echo hello"})
    assert result.ok
    assert "hello" in result.output


def test_executor_write_respects_sandbox(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    inside = tmp_path / "inside"
    inside.mkdir()
    rules = PermissionRules(write_allow_paths=[str(inside)])
    ex = ToolExecutor(rules, cwd=tmp_path)
    with pytest.raises(PermissionDenied):
        ex.dispatch("write", {"path": str(outside / "x"), "content": "nope"})
    result = ex.dispatch("write", {"path": str(inside / "x"), "content": "ok"})
    assert result.ok
    assert (inside / "x").read_text() == "ok"
