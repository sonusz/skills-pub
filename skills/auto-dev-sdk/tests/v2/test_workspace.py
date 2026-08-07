"""v2-8 / v2-16: workspace cleanliness + git prerequisite."""
from __future__ import annotations

import subprocess

import pytest

from autodev.errors import PreflightError
from autodev.workspace import (
    detect_out_of_scope_writes, diff_snapshots, ensure_git_repo,
    is_git_repo, snapshot,
)


def test_non_git_dir_refused(tmp_path):
    with pytest.raises(PreflightError):
        ensure_git_repo(tmp_path)


def test_git_repo_accepted(git_repo):
    ensure_git_repo(git_repo)  # no raise


def test_is_git_repo_true(git_repo):
    assert is_git_repo(git_repo)


def test_snapshot_clean(git_repo):
    s = snapshot(git_repo)
    assert s.is_git is True
    assert s.is_dirty is False


def test_snapshot_dirty(git_repo):
    (git_repo / "new.txt").write_text("x")
    s = snapshot(git_repo)
    assert s.is_dirty is True


def test_snapshot_ignores_active_feature_workspace(git_repo):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    (active / "prd.md").write_text("# PRD\n")
    (active / "design.md").write_text("# Design\n")

    state = snapshot(git_repo)

    assert state.is_dirty is False
    assert {"prd.md", "design.md"} <= {
        line.rsplit("/", 1)[-1] for line in state.lines()
    }


def test_snapshot_still_counts_non_active_feature_docs(git_repo):
    completed = git_repo / "docs" / "features" / "demo" / "complete"
    completed.mkdir(parents=True)
    (completed / "implemented-spec.md").write_text("# Spec\n")

    assert snapshot(git_repo).is_dirty is True


def test_rename_from_user_code_into_active_workspace_stays_dirty(git_repo):
    source = git_repo / "src.txt"
    source.write_text("tracked user code")
    subprocess.run(["git", "add", "src.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add source"], cwd=git_repo, check=True,
    )
    destination = git_repo / "docs" / "features" / "demo" / "active"
    destination.mkdir(parents=True)
    subprocess.run(
        ["git", "mv", "src.txt", str(destination / "src.txt")],
        cwd=git_repo,
        check=True,
    )

    state = snapshot(git_repo)

    assert state.is_dirty is True
    assert any("src.txt" in line for line in state.user_visible_lines())


def test_diff_snapshots_detects_new_file(git_repo):
    before = snapshot(git_repo)
    (git_repo / "new.txt").write_text("x")
    after = snapshot(git_repo)
    diff = diff_snapshots(before, after)
    assert any("new.txt" in d for d in diff)


def test_detect_out_of_scope_write(git_repo):
    feature_active = git_repo / "docs" / "features" / "demo" / "active"
    feature_active.mkdir(parents=True)
    before = snapshot(git_repo)
    # Write inside allowed scope
    (feature_active / "inside.json").write_text("{}")
    # Write outside allowed scope
    (git_repo / "elsewhere.txt").write_text("escape")
    after = snapshot(git_repo)
    escapes = detect_out_of_scope_writes(
        before, after,
        allowed_scope=[feature_active],
        repo_root=git_repo,
    )
    assert any("elsewhere.txt" in e for e in escapes)
    assert not any("inside.json" in e for e in escapes)


def test_detect_out_of_scope_write_to_another_active_feature(git_repo):
    feature_active = git_repo / "docs" / "features" / "demo" / "active"
    feature_active.mkdir(parents=True)
    before = snapshot(git_repo)
    other = git_repo / "docs" / "features" / "other" / "active"
    other.mkdir(parents=True)
    (other / "design.md").write_text("escape")
    after = snapshot(git_repo)

    escapes = detect_out_of_scope_writes(
        before,
        after,
        allowed_scope=[feature_active],
        repo_root=git_repo,
    )

    assert any("docs/features/other/active/design.md" in e for e in escapes)


def test_detects_overwrite_of_preexisting_file_in_another_active_feature(git_repo):
    feature_active = git_repo / "docs" / "features" / "demo" / "active"
    feature_active.mkdir(parents=True)
    other_file = (
        git_repo / "docs" / "features" / "other" / "active" / "design.md"
    )
    other_file.parent.mkdir(parents=True)
    other_file.write_text("original")
    before = snapshot(git_repo)

    # The porcelain line remains `?? .../design.md`; only its bytes change.
    other_file.write_text("corrupted")
    after = snapshot(git_repo)
    escapes = detect_out_of_scope_writes(
        before,
        after,
        allowed_scope=[feature_active],
        repo_root=git_repo,
    )

    assert any("docs/features/other/active/design.md" in e for e in escapes)
