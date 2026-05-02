"""ad-16: tool schemas are well-formed and match ToolExecutor dispatch names."""
from __future__ import annotations

from auto_dev.executor.tool_schemas import (
    BASH_TOOL,
    DEFAULT_TOOLS,
    EDIT_TOOL,
    READ_TOOL,
    WRITE_TOOL,
    default_tools,
    tool_names,
)


def test_default_tools_are_independent_copies():
    a = default_tools()
    b = default_tools()
    a[0]["name"] = "mutated"
    assert b[0]["name"] != "mutated"


def test_schema_names_match_executor_dispatch():
    # These must exactly match ToolExecutor.dispatch() accepted tool args.
    assert tool_names() == ["bash", "read", "write", "edit"]


def test_every_tool_has_required_shape():
    for t in DEFAULT_TOOLS:
        assert "name" in t
        assert "description" in t
        assert "input_schema" in t
        assert t["input_schema"]["type"] == "object"
        assert "properties" in t["input_schema"]


def test_bash_tool_requires_cmd():
    assert "cmd" in BASH_TOOL["input_schema"]["required"]


def test_write_requires_path_and_content():
    required = set(WRITE_TOOL["input_schema"]["required"])
    assert {"path", "content"}.issubset(required)


def test_edit_requires_path_old_new():
    required = set(EDIT_TOOL["input_schema"]["required"])
    assert {"path", "old", "new"}.issubset(required)


def test_read_only_needs_path():
    required = READ_TOOL["input_schema"]["required"]
    assert required == ["path"]
