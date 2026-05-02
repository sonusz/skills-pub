"""Session-level allow/deny rules for bash commands and file paths.

Patterns may be:
  * Shell glob (matched against the full command string)
  * `re:<python-regex>` for anchored regex

Deny beats allow. Unmatched command → denied by default.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class PermissionCheck:
    allowed: bool
    reason: str


@dataclass
class PermissionRules:
    allow_cmd: list[str] = field(default_factory=list)
    deny_cmd: list[str] = field(default_factory=list)
    read_allow_paths: list[str] = field(default_factory=list)
    write_allow_paths: list[str] = field(default_factory=list)

    @classmethod
    def from_flags(
        cls,
        allow_cmd: Iterable[str] | str | None,
        deny_cmd: Iterable[str] | str | None,
        *,
        repo_root: Path | None = None,
        feature_root: Path | None = None,
    ) -> "PermissionRules":
        def _split(v):
            if v is None:
                return []
            if isinstance(v, str):
                return [p.strip() for p in v.split(",") if p.strip()]
            return [str(p).strip() for p in v if str(p).strip()]

        read_paths: list[str] = []
        write_paths: list[str] = []
        if repo_root:
            read_paths.append(str(Path(repo_root).resolve()))
        if feature_root:
            write_paths.append(str(Path(feature_root).resolve()))

        return cls(
            allow_cmd=_split(allow_cmd),
            deny_cmd=_split(deny_cmd),
            read_allow_paths=read_paths,
            write_allow_paths=write_paths,
        )

    def check_cmd(self, cmd: str) -> PermissionCheck:
        cmd = cmd.strip()
        if not cmd:
            return PermissionCheck(False, "empty command")
        for pat in self.deny_cmd:
            if _match(pat, cmd):
                return PermissionCheck(False, f"denied by rule: {pat!r}")
        for pat in self.allow_cmd:
            if _match(pat, cmd):
                return PermissionCheck(True, f"allowed by rule: {pat!r}")
        return PermissionCheck(False, "no matching allow rule (session-level policy)")

    def check_read(self, path: str) -> PermissionCheck:
        return _check_path(path, self.read_allow_paths, kind="read")

    def check_write(self, path: str) -> PermissionCheck:
        return _check_path(path, self.write_allow_paths, kind="write")


def _match(pattern: str, cmd: str) -> bool:
    if pattern.startswith("re:"):
        try:
            return re.match(pattern[3:], cmd) is not None
        except re.error:
            return False
    return fnmatch.fnmatchcase(cmd, pattern)


def _check_path(path: str, allow: list[str], *, kind: str) -> PermissionCheck:
    if not allow:
        # No allow-list → permissive (Read often needed broadly).
        return PermissionCheck(True, f"{kind}: no sandbox configured")
    p = str(Path(path).resolve())
    for a in allow:
        if p == a or p.startswith(a.rstrip("/") + "/"):
            return PermissionCheck(True, f"{kind}: inside {a}")
    return PermissionCheck(False, f"{kind}: {p} outside sandbox {allow}")
