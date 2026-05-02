"""Provider-native tool schemas + dispatch mapping.

Shapes here are Anthropic's tool-use schema (closely matches OpenAI's
function-calling and Google's function-declaration shapes). The adapters
accept these as-is for now; per-vendor translation lives in each adapter.

A schema entry is a plain dict with `name`, `description`, and
`input_schema` (JSON Schema for the tool's arguments). The schema names
match `ToolExecutor.dispatch()`'s accepted `tool` values (bash, read,
write, edit).
"""
from __future__ import annotations

from typing import Any


BASH_TOOL: dict[str, Any] = {
    "name": "bash",
    "description": "Run a shell command. Subject to session-level allow/deny rules declared at `implement` start; unauthorized commands are rejected without elevation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cmd": {"type": "string", "description": "Shell command to run (through /bin/sh -c)."},
            "timeout_sec": {"type": "integer", "description": "Optional per-call timeout.", "default": 600},
        },
        "required": ["cmd"],
    },
}

READ_TOOL: dict[str, Any] = {
    "name": "read",
    "description": "Read a file from disk. Path must resolve under the session's read sandbox.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or cwd-relative file path."},
        },
        "required": ["path"],
    },
}

WRITE_TOOL: dict[str, Any] = {
    "name": "write",
    "description": "Write (or overwrite) a file atomically. Path must be inside the session's write sandbox (feature-root).",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
}

EDIT_TOOL: dict[str, Any] = {
    "name": "edit",
    "description": "In-place edit: replace occurrences of `old` with `new` in a file. By default `old` must match exactly once; pass replace_all=true to relax.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["path", "old", "new"],
    },
}


DEFAULT_TOOLS: list[dict[str, Any]] = [BASH_TOOL, READ_TOOL, WRITE_TOOL, EDIT_TOOL]


def default_tools() -> list[dict[str, Any]]:
    """Fresh copy so callers can mutate without polluting the module-level list."""
    import copy
    return copy.deepcopy(DEFAULT_TOOLS)


def tool_names() -> list[str]:
    return [t["name"] for t in DEFAULT_TOOLS]
