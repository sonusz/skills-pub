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
