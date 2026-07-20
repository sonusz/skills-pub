"""Panel write-integrity guard.

Panel reviewers run with the sandbox bypassed (``yolo``) so they can read
cross-repo material and reach the network. That also means a reviewer *could*
write — and reviewers run concurrently against the same canonical artifacts,
so a stray write by one would silently poison the others' reviews before any
after-the-fact check could notice.

This guard brackets the reviewer dispatch:

- ``snapshot_before`` records the content hashes of the review surface (the
  primary artifact + consulted docs) and, when the working tree is a git repo,
  pins a restore point as a *local, never-pushed* ref
  ``refs/autodev/panel-pre/<gate>`` plus the pre-run ``git status`` baseline.
- ``detect_after`` re-hashes the canonical files and diffs ``git status`` to
  see whether the review surface changed or a tracked file was modified during
  the round.

On detection the caller does **not** auto-revert (the change may have come
from another system, not a reviewer) — it preserves the restore point and
pauses for a human decision, surfacing ready-to-run ``git restore`` commands.

The restore point uses a temp-index ``commit-tree`` so it captures the working
tree (including untracked files) without touching the working tree, the real
index, or the stash stack. The ref is never pushed and is deleted on a clean
pass, so it leaves no trace in shareable history.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from autodev.state.hashing import hash_file


@dataclass
class GuardState:
    repo_root: Path
    gate: str
    canonical_hashes: dict[str, str] = field(default_factory=dict)
    git_ref: str | None = None
    status_baseline: frozenset[str] = frozenset()


def _git(repo_root: Path, *args: str, index_file: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if index_file is not None:
        env["GIT_INDEX_FILE"] = index_file
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, env=env,
    )


def _is_git_repo(repo_root: Path) -> bool:
    r = _git(repo_root, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def _ref_name(gate: str) -> str:
    return f"refs/autodev/panel-pre/{gate}"


def _canonical_hashes(canonical_files: list[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in canonical_files:
        p = Path(raw)
        if p.exists() and p.is_file():
            out[str(p)] = hash_file(p)
    return out


def snapshot_before(
    repo_root: Path,
    gate: str,
    canonical_files: list[Path],
    *,
    log_emit=None,
) -> GuardState:
    """Capture the pre-dispatch state of the review surface + working tree."""
    repo_root = Path(repo_root)
    state = GuardState(
        repo_root=repo_root,
        gate=gate,
        canonical_hashes=_canonical_hashes(canonical_files),
    )
    if not _is_git_repo(repo_root):
        return state  # content-hash detection still works without git
    try:
        ref = _ref_name(gate)
        idx_fd, idx_path = tempfile.mkstemp(prefix="autodev-panel-idx-")
        os.close(idx_fd)
        try:
            # Capture the whole working tree (tracked + untracked) into a tree
            # object via a throwaway index, leaving the real index/worktree/stash
            # untouched. Best-effort: if any step fails we fall back to
            # content-hash-only detection (git_ref stays None).
            os.unlink(idx_path)  # git wants to create it fresh
            _git(repo_root, "add", "-A", index_file=idx_path)
            wt = _git(repo_root, "write-tree", index_file=idx_path)
            if wt.returncode == 0 and wt.stdout.strip():
                ct = _git(repo_root, "commit-tree", wt.stdout.strip(),
                          "-m", f"autodev panel-pre {gate}")
                if ct.returncode == 0 and ct.stdout.strip():
                    ur = _git(repo_root, "update-ref", ref, ct.stdout.strip())
                    if ur.returncode == 0:
                        state.git_ref = ref
        finally:
            try:
                os.unlink(idx_path)
            except OSError:
                pass
        st = _git(repo_root, "status", "--porcelain")
        if st.returncode == 0:
            state.status_baseline = frozenset(st.stdout.splitlines())
    except Exception:
        state.git_ref = None
    if log_emit is not None:
        log_emit({
            "event": "panel-integrity-snapshot",
            "gate": gate,
            "git_ref": state.git_ref,
            "canonical_count": len(state.canonical_hashes),
        })
    return state


def detect_after(state: GuardState, canonical_files: list[Path]) -> list[str]:
    """Return human-readable descriptions of any change to the review surface
    or tracked files since ``snapshot_before``. Empty list == clean."""
    changes: list[str] = []

    # (1) Review surface — content hashes. Catches modification of tracked OR
    # untracked canonical files (a reviewer editing design.md etc.).
    canonical_keys = {str(Path(p)) for p in canonical_files}
    canonical_abspaths = {
        str(Path(p).resolve()) for p in canonical_files
    }
    # The panel writes its verdict and reviewer-cache files before this guard
    # performs the post-round check. Those files are outputs of the round, not
    # reviewer inputs, and may already be tracked from an earlier run. Treating
    # their legitimate rewrite as an out-of-band reviewer mutation makes every
    # subsequent panel run fail integrity. Keep the exemption narrowly scoped
    # to the active feature directory and the output groups owned by this gate.
    panel_output_abspaths: set[str] = set()
    if canonical_files:
        output_dir = Path(canonical_files[0]).parent
        output_gates = (
            ("design-review", "trace-review")
            if state.gate == "design-review"
            else (state.gate,)
        )
        for output_gate in output_gates:
            panel_output_abspaths.add(
                str((output_dir / f"panel-{output_gate}.json").resolve())
            )
            panel_output_abspaths.add(
                str(
                    (
                        output_dir
                        / f"panel-{output_gate}.reviewers.json"
                    ).resolve()
                )
            )
    for raw in canonical_files:
        p = Path(raw)
        key = str(p)
        before = state.canonical_hashes.get(key)
        if p.exists() and p.is_file():
            now = hash_file(p)
            if before is None:
                changes.append(f"{key} (created during review)")
            elif now != before:
                changes.append(f"{key} (modified during review)")
        elif before is not None:
            changes.append(f"{key} (deleted during review)")

    # (2) Repo hygiene — tracked files modified beyond the canonical set.
    # Only *tracked* modifications are flagged (status code != "??"); new
    # untracked files are skipped because the panel itself writes untracked
    # scratch (verdicts, caches, logs) and would cause false positives.
    # Pre-existing modifications are excluded via the status baseline, so only
    # changes introduced during this round surface.
    if state.git_ref is not None:
        st = _git(state.repo_root, "status", "--porcelain")
        if st.returncode == 0:
            for line in st.stdout.splitlines():
                if line in state.status_baseline:
                    continue
                if len(line) < 4:
                    continue
                code, path = line[:2], line[3:]
                if code == "??":
                    continue  # new untracked file — panel scratch, skip
                abspath = str((state.repo_root / path).resolve())
                if abspath in canonical_abspaths:
                    continue  # canonical handled by (1)
                if abspath in panel_output_abspaths:
                    continue  # harness-owned output, not review input
                changes.append(f"{path} (tracked file modified during review)")

    return changes


def discard(state: GuardState) -> None:
    """Delete the restore-point ref (clean pass)."""
    if state.git_ref is not None:
        _git(state.repo_root, "update-ref", "-d", state.git_ref)


def format_report(state: GuardState, changes: list[str]) -> str:
    """Build the human-facing pause report, including manual git rollback
    commands when a restore point exists. Never auto-applied."""
    lines = [
        f"panel {state.gate}: the review surface changed DURING the reviewer "
        "round, so the verdict from this round is untrustworthy (concurrent "
        "reviewers may have read mutated content). This round was discarded "
        "and the run is paused.",
        "",
        "Changed during review:",
    ]
    lines += [f"  - {c}" for c in changes]
    lines.append("")
    lines.append(
        "This was NOT auto-reverted — the change may have come from a reviewer "
        "OR from another system/process. Investigate which, then either keep "
        "the change (resume) or roll it back manually."
    )
    if state.git_ref is not None:
        lines += [
            "",
            f"A pre-review restore point is pinned at `{state.git_ref}` "
            "(local-only, never pushed). To roll a file back to its "
            "pre-review content:",
            f"  git -C {state.repo_root} restore --worktree "
            f"--source={state.git_ref} -- <path>",
            "Files the reviewer newly created can simply be deleted. After "
            "resolving, resume the run; the panel re-runs fresh.",
            f"When done, remove the restore point: "
            f"git -C {state.repo_root} update-ref -d {state.git_ref}",
        ]
    else:
        lines += [
            "",
            "(No git restore point — working tree is not a git repo. Restore "
            "the listed files manually if the change was a reviewer.)",
        ]
    return "\n".join(lines)
