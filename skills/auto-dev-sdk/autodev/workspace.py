"""Workspace cleanliness + out-of-scope write detection (R2a, v2-8).

Best-effort: wraps `git status --porcelain`. Not a security boundary.

Harness-owned feature workspaces (`docs/features/<feature>/active/`) and
internal paths (`.lock/`, `.pause`, `.running.pid`,
`.running-pids.json`, `*.stdout.log`,
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
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from autodev.errors import PreflightError
from autodev.state.hashing import hash_bytes, hash_file

# Harness-internal filename patterns — NOT considered "dirty" for the
# pipeline-blocking check. Per-feature state + subprocess byproducts.
_HARNESS_IGNORE_PATTERNS = (
    "docs/features/*/active/.lock",
    "docs/features/*/active/.lock/*",
    "docs/features/*/active/.pause",
    "docs/features/*/active/.running.pid",
    "docs/features/*/active/.running-pids.json",
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
            return (
                f"symlink:mode={info.st_mode}:uid={info.st_uid}:gid={info.st_gid}:"
                f"target={os.readlink(path)}"
            )
        except OSError:
            return "symlink:unreadable"
    if stat.S_ISREG(info.st_mode):
        try:
            return (
                f"file:mode={info.st_mode}:uid={info.st_uid}:gid={info.st_gid}:"
                f"size={info.st_size}:mtime_ns={info.st_mtime_ns}:"
                f"hash={hash_file(path)}"
            )
        except OSError:
            return "file:unreadable"
    return (
        f"other:mode={info.st_mode}:uid={info.st_uid}:gid={info.st_gid}:"
        f"size={info.st_size}:mtime_ns={info.st_mtime_ns}"
    )


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
    head: str | None = None
    watched_fingerprints: dict[str, str] = field(default_factory=dict)

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


def _repo_relative_key(repo_root: Path, path: Path) -> str:
    """Return a stable logical key without resolving the final symlink."""
    root = Path(repo_root).absolute()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.absolute()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return str(candidate)


def snapshot(
    cwd: Path,
    *,
    watched_paths: list[Path] | None = None,
) -> WorkspaceState:
    """Take a git/worktree snapshot, optionally pinning path contents.

    ``git status`` alone cannot see a protected-file edit that an agent
    commits before returning, and it treats an unchanged untracked file as a
    change when another command merely stages it. Watched fingerprints are
    independent of index/HEAD state and close both gaps.
    """
    if not is_git_repo(cwd):
        return WorkspaceState(is_git=False, is_dirty=False, raw="")
    rc, out = _run_git(
        ["status", "--porcelain", "--untracked-files=all"], cwd,
    )
    if rc != 0:
        raise PreflightError(f"git status failed (rc={rc})")
    head_rc, head_out = _run_git(["rev-parse", "--verify", "HEAD"], Path(cwd))
    watched = {
        _repo_relative_key(Path(cwd), path): _path_fingerprint(
            Path(cwd), _repo_relative_key(Path(cwd), path),
        )
        for path in (watched_paths or [])
    }
    state = WorkspaceState(
        is_git=True,
        is_dirty=False,
        raw=out,
        path_fingerprints=_snapshot_fingerprints(Path(cwd), out),
        head=head_out.strip() if head_rc == 0 and head_out.strip() else None,
        watched_fingerprints=watched,
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


def user_visible_changes(
    before: WorkspaceState,
    after: WorkspaceState,
) -> list[str]:
    """Return user-visible worktree/index changes between two snapshots.

    This keeps pre-existing user dirt as a baseline while still noticing a
    stage that adds, stages, removes, or rewrites a product file. Active
    feature artifacts and harness internals remain excluded by the same rules
    used for the preflight dirty check. The comparison is symmetric: a
    baseline entry that vanished is reported too. Callers that accept a
    stage committing pre-existing dirt subtract ``committed_baseline_dirt``.
    """
    visible = set(before.user_visible_lines()) | set(after.user_visible_lines())
    return [entry for entry in diff_snapshots(before, after) if entry in visible]


def _fingerprint_content_hash(fingerprint: str | None) -> str | None:
    if not fingerprint or not fingerprint.startswith("file:"):
        return None
    marker = ":hash="
    index = fingerprint.rfind(marker)
    return fingerprint[index + len(marker):] if index >= 0 else None


def _committed_blob_sha256(repo_root: Path, ref: str, path: str) -> str | None:
    """``hash_file``-format digest of ``path`` at ``ref``; None if absent."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "blob", f"{ref}:{path}"],
            capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return hash_bytes(proc.stdout)


_MTIME_FIELD_RE = re.compile(r":mtime_ns=\d+")


def _watched_changed(before: str | None, after: str | None) -> bool:
    """Whether a watched path materially changed between two fingerprints.

    Fingerprints embed mtime alongside mode/owner/size/content hash. A touch
    or a branch round-trip that leaves bytes and mode identical must not
    count as a protected-input edit, so mtime alone is ignored; mode, owner
    and content changes still count.
    """
    if before == after:
        return False
    if before is None or after is None:
        return True
    return _MTIME_FIELD_RE.sub("", before) != _MTIME_FIELD_RE.sub("", after)


def stage_residue(
    before: WorkspaceState,
    after: WorkspaceState,
    repo_root: Path,
) -> tuple[list[str], list[str]]:
    """Return ``(residue, absorbed)`` for a stage that ran between snapshots.

    ``residue`` is every user-visible change the stage left behind that is
    not explained by committing pre-existing dirt unchanged; ``absorbed`` is
    that committed dirt (see ``committed_baseline_dirt``). The build loop and
    the resume-time re-validation share the rule; they differ only in which
    baseline they hold the stage to (loop entry vs. the persisted iteration
    start).
    """
    absorbed = committed_baseline_dirt(before, after, repo_root)
    absorbed_set = set(absorbed)
    residue = [
        entry for entry in user_visible_changes(before, after)
        if entry not in absorbed_set
    ]
    return residue, absorbed


def committed_baseline_dirt(
    before: WorkspaceState,
    after: WorkspaceState,
    repo_root: Path,
) -> list[str]:
    """Baseline entries the stage absorbed by committing them unchanged.

    A user-visible entry present in ``before`` and absent from ``after``
    counts only when its path is in ``git diff before.head..after.head``
    AND the committed blob has exactly the content the baseline worktree
    held (or the baseline deletion is now committed). A stage that threw
    the user's edit away and committed its own version of the file is not
    absorbing dirt — that entry stays residue so the attempt is rejected
    instead of silently losing the user's work.
    """
    if not before.head or not after.head or before.head == after.head:
        return []
    committed_paths = {
        path
        for entry in _committed_path_entries(before, after, repo_root)
        for path in _parse_porcelain_paths(entry)
    }
    if not committed_paths:
        return []
    after_entries = set(after.lines())
    absorbed: list[str] = []
    for entry in before.user_visible_lines():
        if entry in after_entries:
            continue
        paths = _parse_porcelain_paths(entry)
        if len(paths) != 1 or paths[0] not in committed_paths:
            continue  # renames and unparsable entries stay residue
        path = paths[0]
        baseline = before.path_fingerprints.get(path)
        committed = _committed_blob_sha256(repo_root, after.head, path)
        if baseline == "missing":
            if committed is None:
                absorbed.append(entry)  # baseline deletion now committed
            continue
        baseline_hash = _fingerprint_content_hash(baseline)
        if baseline_hash is not None and baseline_hash == committed:
            absorbed.append(entry)
    return absorbed


def _committed_path_entries(
    before: WorkspaceState,
    after: WorkspaceState,
    repo_root: Path,
) -> list[str]:
    """Return porcelain-shaped entries changed between stage boundary HEADs."""
    if not before.head or not after.head or before.head == after.head:
        return []
    rc, raw = _run_git(
        [
            "diff", "--name-status", "-z", "--find-renames",
            before.head, after.head, "--",
        ],
        Path(repo_root),
    )
    if rc != 0:
        raise PreflightError(
            "failed to compare pre/post-stage Git trees for write containment"
        )
    tokens = raw.split("\0")
    entries: list[str] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        status_code = tokens[index]
        index += 1
        if status_code[:1] in {"R", "C"}:
            if index + 1 >= len(tokens):
                break
            old_path, new_path = tokens[index], tokens[index + 1]
            index += 2
            entries.append(
                f"{status_code[:2].ljust(2)} {old_path} -> {new_path}"
            )
            continue
        if index >= len(tokens):
            break
        path = tokens[index]
        index += 1
        entries.append(f"{status_code[:2].ljust(2)} {path}")
    return entries


def detect_out_of_scope_writes(
    before: WorkspaceState,
    after: WorkspaceState,
    *,
    allowed_scope: list[Path],
    repo_root: Path,
    protected_scope: list[Path] | None = None,
) -> list[str]:
    """Return writes outside ``allowed_scope`` or inside ``protected_scope``.

    Protected paths take precedence over broader allowed paths. This matters
    for build, whose writable surface is the repository root but whose PRD and
    accepted design packet remain immutable inputs. Harness-internal paths are
    otherwise excluded — they're expected to appear (.lock/, log.jsonl, etc.)
    and are not considered user-visible escapes.
    """
    worktree_entries = set(diff_snapshots(before, after))
    committed_entries = set(_committed_path_entries(before, after, repo_root))
    protected_content_entries: set[str] = set()
    for protected in protected_scope or []:
        key = _repo_relative_key(repo_root, protected)
        if (
            key in before.watched_fingerprints
            and key in after.watched_fingerprints
            and _watched_changed(
                before.watched_fingerprints[key],
                after.watched_fingerprints[key],
            )
        ):
            # Synthetic porcelain-shaped entry: direct protected-path
            # fingerprints must remain effective even when Git status is
            # hidden by ignore/assume-unchanged flags.
            protected_content_entries.add(f"P  {key}")
    new_entries = sorted(
        worktree_entries | committed_entries | protected_content_entries
    )
    escapes: list[str] = []
    allowed_resolved = [Path(p).resolve() for p in allowed_scope]
    protected_resolved = [Path(p).resolve() for p in (protected_scope or [])]
    for entry in new_entries:
        path_strs = _parse_porcelain_paths(entry)
        if not path_strs:
            continue
        resolved_paths = [
            (Path(repo_root) / path_str).resolve() for path_str in path_strs
        ]
        matching_protected = [
            protected
            for path in resolved_paths
            for protected in protected_resolved
            if _is_under(path, protected)
        ]
        if matching_protected:
            watched_keys = [
                _repo_relative_key(repo_root, protected)
                for protected in matching_protected
            ]
            has_direct_baseline = all(
                key in before.watched_fingerprints
                and key in after.watched_fingerprints
                for key in watched_keys
            )
            content_changed = any(
                _watched_changed(
                    before.watched_fingerprints.get(key),
                    after.watched_fingerprints.get(key),
                )
                for key in watched_keys
            )
            # A committed tree change is a mutation even when file bytes are
            # unchanged (for example, accidentally adding an immutable,
            # previously-untracked PRD). Without a direct baseline, retain
            # the conservative legacy status behavior.
            if (
                entry in committed_entries
                or content_changed
                or not has_direct_baseline
            ):
                escapes.append(entry)
            continue
        if _line_is_harness_internal(entry):
            continue
        if any(
            not any(
                _is_under(path, allowed)
                for allowed in allowed_resolved
            )
            for path in resolved_paths
        ):
            escapes.append(entry)
    return escapes


def _is_under(path: Path, ancestor: Path) -> bool:
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except ValueError:
        return False
