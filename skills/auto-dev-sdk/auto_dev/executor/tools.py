"""Lightweight Bash / Read / Write / Edit tool implementations.

Subagents call these via a single `ToolExecutor.dispatch(tool, args)` entry
point. Permission checks run BEFORE the tool runs; denial raises
`PermissionDenied` and is the subagent's signal to record a deviation.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auto_dev.errors import PermissionDenied
from auto_dev.executor.permissions import PermissionRules


@dataclass
class ToolResult:
    ok: bool
    output: str = ""
    exit_code: int = 0
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    def __init__(
        self,
        rules: PermissionRules,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int = 600,
    ) -> None:
        self.rules = rules
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.env = env or os.environ.copy()
        self.timeout_sec = timeout_sec

    def dispatch(self, tool: str, args: dict[str, Any]) -> ToolResult:
        if tool == "bash":
            return self._bash(args)
        if tool == "read":
            return self._read(args)
        if tool == "write":
            return self._write(args)
        if tool == "edit":
            return self._edit(args)
        raise ValueError(f"unknown tool: {tool!r}")

    def _bash(self, args: dict[str, Any]) -> ToolResult:
        cmd = args.get("cmd") or args.get("command")
        if not cmd:
            raise ValueError("bash requires 'cmd'")
        check = self.rules.check_cmd(cmd)
        if not check.allowed:
            raise PermissionDenied(f"bash: {cmd!r} — {check.reason}")
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(self.cwd),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=args.get("timeout_sec", self.timeout_sec),
        )
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return ToolResult(
            ok=proc.returncode == 0,
            output=output,
            exit_code=proc.returncode,
            tool="bash",
            args={"cmd": cmd},
        )

    def _read(self, args: dict[str, Any]) -> ToolResult:
        path = args.get("path")
        if not path:
            raise ValueError("read requires 'path'")
        check = self.rules.check_read(path)
        if not check.allowed:
            raise PermissionDenied(f"read: {path!r} — {check.reason}")
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult(ok=False, output=str(e), exit_code=1, tool="read", args={"path": path})
        return ToolResult(ok=True, output=text, tool="read", args={"path": path})

    def _write(self, args: dict[str, Any]) -> ToolResult:
        path = args.get("path")
        content = args.get("content", "")
        if not path:
            raise ValueError("write requires 'path'")
        check = self.rules.check_write(path)
        if not check.allowed:
            raise PermissionDenied(f"write: {path!r} — {check.reason}")
        from auto_dev.state.atomic import atomic_write

        atomic_write(Path(path), content)
        return ToolResult(ok=True, output=f"wrote {path}", tool="write", args={"path": path})

    def _edit(self, args: dict[str, Any]) -> ToolResult:
        path = args.get("path")
        old = args.get("old")
        new = args.get("new")
        if not path or old is None or new is None:
            raise ValueError("edit requires 'path', 'old', 'new'")
        check = self.rules.check_write(path)
        if not check.allowed:
            raise PermissionDenied(f"edit: {path!r} — {check.reason}")
        from auto_dev.state.atomic import atomic_write

        text = Path(path).read_text(encoding="utf-8")
        if old not in text:
            return ToolResult(ok=False, output="old string not found", exit_code=1, tool="edit", args={"path": path})
        count = text.count(old)
        if count > 1 and not args.get("replace_all"):
            return ToolResult(ok=False, output=f"old string matches {count} times; set replace_all", exit_code=1, tool="edit", args={"path": path})
        text = text.replace(old, new)
        atomic_write(Path(path), text)
        return ToolResult(ok=True, output=f"edited {path}", tool="edit", args={"path": path})

    # Convenience for callers that want to shell out ad-hoc.
    def bash(self, cmd: str) -> ToolResult:
        return self._bash({"cmd": cmd})
