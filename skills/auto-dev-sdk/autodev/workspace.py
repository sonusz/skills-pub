"""Workspace cleanliness + out-of-scope write detection (R2a, v2-8).

Best-effort: wraps `git status --porcelain`. Not a security boundary.

Harness-internal paths (`.lock/`, `.pause`, `.running.pid`, `*.stdout.log`,
`*.stderr.log`, `*.tmp` mid-rename, `*-failure.json`, `log.jsonl`,
`overrides.json`, `panel-*.json`) are excluded from the dirty check —
they're orchestration state, not user code. `is_dirty` reflects only
user-visible modifications.
"""
from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path

from autodev.errors import PreflightError

# Harness-internal filename patterns — NOT considered "dirty" for the
# pipeline-blocking check. Per-feature state + subprocess byproducts.
_HARNESS_IGNORE_PATTERNS = (
    "docs/features/*/active/.lock",
    "docs/features/*/active/.lock/*",
    "docs/features/*/active/.pause",
    "docs/features/*/active/.running.pid",
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


def _parse_porcelain_path(line: str) -> str:
    if len(line) < 4:
        return ""
    path = line[3:].strip().strip('"')
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path


def _line_is_harness_internal(porcelain_line: str) -> bool:
    path = _parse_porcelain_path(porcelain_line)
    if not path:
        return False
    for pat in _HARNESS_IGNORE_PATTERNS:
        if fnmatch.fnmatchcase(path, pat):
            return True
    return False


@dataclass
class WorkspaceState:
    """Snapshot of `git status --porcelain` at a moment.

    `is_dirty` reflects user-visible dirt only (excludes harness-internal);
    `raw` is full porcelain output for diffing.
    """
    is_git: bool
    is_dirty: bool
    raw: str

    def lines(self) -> list[str]:
        return [l for l in self.raw.splitlines() if l.strip()]

    def user_visible_lines(self) -> list[str]:
        return [l for l in self.lines() if not _line_is_harness_internal(l)]


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
    rc, out = _run_git(["status", "--porcelain"], cwd)
    if rc != 0:
        raise PreflightError(f"git status failed (rc={rc})")
    state = WorkspaceState(is_git=True, is_dirty=False, raw=out)
    state.is_dirty = bool(state.user_visible_lines())
    return state


def diff_snapshots(before: WorkspaceState, after: WorkspaceState) -> list[str]:
    """Files touched between two porcelain snapshots (set diff by status+path)."""
    before_keys = set(before.lines())
    after_keys = set(after.lines())
    return sorted(after_keys - before_keys)


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
        path_str = _parse_porcelain_path(entry)
        if not path_str:
            continue
        resolved = (Path(repo_root) / path_str).resolve()
        if not any(_is_under(resolved, a) for a in allowed_resolved):
            escapes.append(entry)
    return escapes


def _is_under(path: Path, ancestor: Path) -> bool:
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except ValueError:
        return False
