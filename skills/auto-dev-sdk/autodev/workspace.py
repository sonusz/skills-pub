"""Workspace cleanliness + out-of-scope write detection (R2a, v2-8).

Best-effort: wraps `git status --porcelain`. Not a security boundary.

Harness-owned feature workspaces (`docs/features/<feature>/active/`) and
internal paths (`.lock/`, `.pause`, `.running.pid`,
`.running-pids.json`, `.route-feedback.json`, `*.stdout.log`,
`*.stderr.log`, `*.tmp` mid-rename, `*-failure.json`, `log.jsonl`,
`overrides.json`, `panel-*.json`) are excluded from the dirty check —
they're pipeline state, not user code. The raw porcelain snapshot is retained,
so post-stage containment checks still catch an agent writing into a different
feature workspace. `is_dirty` reflects only user-visible modifications.
"""
from __future__ import annotations

import fnmatch
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from autodev.errors import PreflightError
from autodev.state.hashing import hash_file

# Harness-internal filename patterns — NOT considered "dirty" for the
# pipeline-blocking check. Per-feature state + subprocess byproducts.
_HARNESS_IGNORE_PATTERNS = (
    "docs/features/*/active/.lock",
    "docs/features/*/active/.lock/*",
    "docs/features/*/active/.pause",
    "docs/features/*/active/.running.pid",
    "docs/features/*/active/.running-pids.json",
    "docs/features/*/active/.route-feedback.json",
    "docs/features/*/active/.*.stdout.log",
    "docs/features/*/active/.*.stderr.log",
    "docs/features/*/active/*.tmp",
    "docs/features/*/active/*-failure.json",
    "docs/features/*/active/log.jsonl",
    "docs/features/*/active/overrides.json",
    "docs/features/*/active/panel-*.json",
    "docs/features/*/active/panel-*-raw",
    "docs/features/*/active/panel-*-raw/*",
)


def _unquote_porcelain_path(value: str) -> str:
    return value.strip().strip('"')


def _parse_porcelain_paths(line: str) -> tuple[str, ...]:
    """Return every path represented by a porcelain-v1 status entry."""
    if len(line) < 4:
        return ()
    payload = line[3:].strip()
    if " -> " in payload:
        source, destination = payload.split(" -> ", 1)
        return (
            _unquote_porcelain_path(source),
            _unquote_porcelain_path(destination),
        )
    path = _unquote_porcelain_path(payload)
    return (path,) if path else ()


def _line_is_harness_internal(porcelain_line: str) -> bool:
    paths = _parse_porcelain_paths(porcelain_line)
    if not paths:
        return False
    return all(
        any(fnmatch.fnmatchcase(path, pat) for pat in _HARNESS_IGNORE_PATTERNS)
        for path in paths
    )


def _line_is_active_feature_workspace(porcelain_line: str) -> bool:
    """True for a path owned by an active auto-dev feature workspace.

    Keep this separate from ``_line_is_harness_internal``: active feature
    artifacts should not block preflight, but a stage writing into another
    feature's workspace must still be visible to out-of-scope-write detection.
    ``git status --untracked-files=all`` ensures an untracked active directory
    is reported as individual paths rather than a collapsed parent directory.
    """
    paths = _parse_porcelain_paths(porcelain_line)
    if not paths:
        return False
    return all(
        len(parts) >= 4
        and parts[:2] == ["docs", "features"]
        and parts[3] == "active"
        for parts in (Path(path).as_posix().split("/") for path in paths)
    )


def _path_fingerprint(repo_root: Path, relative_path: str) -> str:
    """Fingerprint a dirty path without following symlinks."""
    path = Path(repo_root) / relative_path
    try:
        info = path.lstat()
    except OSError:
        return "missing"
    if stat.S_ISLNK(info.st_mode):
        try:
            return f"symlink:{os.readlink(path)}"
        except OSError:
            return "symlink:unreadable"
    if stat.S_ISREG(info.st_mode):
        try:
            return hash_file(path)
        except OSError:
            return "file:unreadable"
    return f"mode:{info.st_mode}:size:{info.st_size}:mtime_ns:{info.st_mtime_ns}"


def _snapshot_fingerprints(repo_root: Path, raw: str) -> dict[str, str]:
    paths = {
        path
        for line in raw.splitlines()
        if line.strip()
        for path in _parse_porcelain_paths(line)
    }
    return {path: _path_fingerprint(repo_root, path) for path in paths}


@dataclass
class WorkspaceState:
    """Snapshot of `git status --porcelain` at a moment.

    `is_dirty` reflects user-visible dirt only (excludes harness-internal);
    `raw` is full porcelain output for diffing.
    """
    is_git: bool
    is_dirty: bool
    raw: str
    path_fingerprints: dict[str, str] = field(default_factory=dict)

    def lines(self) -> list[str]:
        return [l for l in self.raw.splitlines() if l.strip()]

    def user_visible_lines(self) -> list[str]:
        return [
            line
            for line in self.lines()
            if not _line_is_harness_internal(line)
            and not _line_is_active_feature_workspace(line)
        ]


def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd),
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise PreflightError(f"git invocation failed: {e}") from e
    return proc.returncode, proc.stdout


def is_git_repo(path: Path) -> bool:
    try:
        rc, out = _run_git(["rev-parse", "--is-inside-work-tree"], Path(path))
    except PreflightError:
        return False
    return rc == 0 and out.strip() == "true"


def ensure_git_repo(path: Path) -> None:
    if not is_git_repo(path):
        raise PreflightError(
            f"{path} is not inside a git repository. "
            f"autodev-sdk v2 requires a git worktree for dirty-state detection "
            f"and post-hoc drift scanning. See PRD §4 Constraints."
        )


def snapshot(cwd: Path) -> WorkspaceState:
    """Take a `git status --porcelain` snapshot. is_dirty filters harness-internal."""
    if not is_git_repo(cwd):
        return WorkspaceState(is_git=False, is_dirty=False, raw="")
    rc, out = _run_git(
        ["status", "--porcelain", "--untracked-files=all"], cwd,
    )
    if rc != 0:
        raise PreflightError(f"git status failed (rc={rc})")
    state = WorkspaceState(
        is_git=True,
        is_dirty=False,
        raw=out,
        path_fingerprints=_snapshot_fingerprints(Path(cwd), out),
    )
    state.is_dirty = bool(state.user_visible_lines())
    return state


def diff_snapshots(before: WorkspaceState, after: WorkspaceState) -> list[str]:
    """Status entries added/removed or whose represented file bytes changed."""
    before_keys = set(before.lines())
    after_keys = set(after.lines())
    changed = before_keys.symmetric_difference(after_keys)
    for entry in before_keys.intersection(after_keys):
        if any(
            before.path_fingerprints.get(path)
            != after.path_fingerprints.get(path)
            for path in _parse_porcelain_paths(entry)
        ):
            changed.add(entry)
    return sorted(changed)


def detect_out_of_scope_writes(
    before: WorkspaceState,
    after: WorkspaceState,
    *,
    allowed_scope: list[Path],
    repo_root: Path,
) -> list[str]:
    """Return porcelain entries whose path isn't under any allowed_scope path.

    Harness-internal paths are excluded — they're expected to appear
    (.lock/, log.jsonl, etc.) and not considered user-visible escapes.
    """
    new_entries = diff_snapshots(before, after)
    escapes: list[str] = []
    allowed_resolved = [Path(p).resolve() for p in allowed_scope]
    for entry in new_entries:
        if _line_is_harness_internal(entry):
            continue
        path_strs = _parse_porcelain_paths(entry)
        if not path_strs:
            continue
        if any(
            not any(
                _is_under((Path(repo_root) / path_str).resolve(), allowed)
                for allowed in allowed_resolved
            )
            for path_str in path_strs
        ):
            escapes.append(entry)
    return escapes


def _is_under(path: Path, ancestor: Path) -> bool:
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except ValueError:
        return False
