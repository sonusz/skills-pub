"""Shared v2 test fixtures."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """Initialize a git worktree at tmp_path and return the root."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example"],
                   cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "test"],
                   cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=str(tmp_path), check=True)
    (tmp_path / "docs" / "features").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def feature_active(git_repo) -> Path:
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    return active
