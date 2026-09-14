"""v2-8 / v2-16: workspace cleanliness + git prerequisite."""
from __future__ import annotations

import subprocess

import pytest

from autodev.errors import PreflightError
from autodev.workspace import (
    committed_baseline_dirt, detect_out_of_scope_writes, diff_snapshots,
    ensure_git_repo, is_git_repo, snapshot, user_visible_changes,
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


def test_user_visible_changes_preserves_existing_dirt_and_ignores_active_state(
    git_repo,
):
    existing = git_repo / "existing.txt"
    existing.write_text("user dirt\n")
    before = snapshot(git_repo)

    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    (active / "scratch.md").write_text("harness state\n")
    product = git_repo / "product.py"
    product.write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "--", "product.py"], cwd=git_repo, check=True)

    residue = user_visible_changes(before, snapshot(git_repo))
    assert any("product.py" in line for line in residue)
    assert not any("scratch.md" in line for line in residue)
    assert not any("existing.txt" in line for line in residue)

    subprocess.run(
        ["git", "commit", "-q", "-m", "commit product"],
        cwd=git_repo,
        check=True,
    )
    assert user_visible_changes(before, snapshot(git_repo)) == []


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


def test_protected_path_wins_over_broad_allowed_scope(git_repo):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    protected = active / "prd.md"
    protected.write_text("original")
    product = git_repo / "service.py"
    product.write_text("before")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"], cwd=git_repo, check=True,
    )
    before = snapshot(git_repo)

    protected.write_text("mutated")
    product.write_text("after")
    after = snapshot(git_repo)

    escapes = detect_out_of_scope_writes(
        before,
        after,
        allowed_scope=[git_repo],
        protected_scope=[protected],
        repo_root=git_repo,
    )

    assert any("prd.md" in entry for entry in escapes)
    assert not any("service.py" in entry for entry in escapes)


def test_explicitly_protected_harness_file_is_not_ignored(git_repo):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    verdict = active / "panel-design-review.json"
    verdict.write_text('{"verdict":"pass"}\n')
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"], cwd=git_repo, check=True,
    )
    before = snapshot(git_repo)

    verdict.write_text('{"verdict":"needs_revision"}\n')
    after = snapshot(git_repo)
    escapes = detect_out_of_scope_writes(
        before,
        after,
        allowed_scope=[active],
        protected_scope=[verdict],
        repo_root=git_repo,
    )

    assert any("panel-design-review.json" in entry for entry in escapes)


def test_protected_content_change_is_caught_after_agent_commits(git_repo):
    protected = git_repo / "docs" / "features" / "demo" / "active" / "prd.md"
    protected.parent.mkdir(parents=True)
    protected.write_text("original")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"], cwd=git_repo, check=True,
    )
    before = snapshot(git_repo, watched_paths=[protected])

    protected.write_text("mutated")
    subprocess.run(["git", "add", str(protected)], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "agent commit"], cwd=git_repo, check=True,
    )
    after = snapshot(git_repo, watched_paths=[protected])

    escapes = detect_out_of_scope_writes(
        before,
        after,
        allowed_scope=[git_repo],
        protected_scope=[protected],
        repo_root=git_repo,
    )

    assert any("prd.md" in entry for entry in escapes)


def test_committed_out_of_scope_change_is_caught(git_repo):
    allowed = git_repo / "docs" / "features" / "demo" / "active"
    allowed.mkdir(parents=True)
    outside = git_repo / "service.py"
    outside.write_text("original")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"], cwd=git_repo, check=True,
    )
    before = snapshot(git_repo)

    outside.write_text("mutated")
    subprocess.run(["git", "add", str(outside)], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "agent commit"], cwd=git_repo, check=True,
    )
    after = snapshot(git_repo)

    escapes = detect_out_of_scope_writes(
        before,
        after,
        allowed_scope=[allowed],
        repo_root=git_repo,
    )

    assert any("service.py" in entry for entry in escapes)


def test_direct_protected_fingerprint_does_not_depend_on_git_status(git_repo):
    protected = git_repo / "prd.md"
    protected.write_text("original")
    subprocess.run(["git", "add", "prd.md"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"], cwd=git_repo, check=True,
    )
    before = snapshot(git_repo, watched_paths=[protected])

    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "prd.md"],
        cwd=git_repo,
        check=True,
    )
    protected.write_text("hidden mutation")
    after = snapshot(git_repo, watched_paths=[protected])
    assert not any("prd.md" in entry for entry in after.lines())

    escapes = detect_out_of_scope_writes(
        before,
        after,
        allowed_scope=[git_repo],
        protected_scope=[protected],
        repo_root=git_repo,
    )

    assert any("prd.md" in entry for entry in escapes)


def test_index_only_transition_does_not_fake_protected_content_change(git_repo):
    protected = git_repo / "prd.md"
    protected.write_text("unchanged")
    before = snapshot(git_repo, watched_paths=[protected])

    subprocess.run(["git", "add", "prd.md"], cwd=git_repo, check=True)
    after = snapshot(git_repo, watched_paths=[protected])
    escapes = detect_out_of_scope_writes(
        before,
        after,
        allowed_scope=[git_repo],
        protected_scope=[protected],
        repo_root=git_repo,
    )

    assert escapes == []


def test_protected_permission_change_is_caught(git_repo):
    protected = git_repo / "prd.md"
    protected.write_text("immutable")
    subprocess.run(["git", "add", "prd.md"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"], cwd=git_repo, check=True,
    )
    before = snapshot(git_repo, watched_paths=[protected])

    protected.chmod(protected.stat().st_mode | 0o111)
    after = snapshot(git_repo, watched_paths=[protected])
    escapes = detect_out_of_scope_writes(
        before,
        after,
        allowed_scope=[git_repo],
        protected_scope=[protected],
        repo_root=git_repo,
    )

    assert any("prd.md" in entry for entry in escapes)


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


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True)


def test_committed_baseline_dirt_absorbs_dirt_committed_unchanged(git_repo):
    """A stage committing the user's edit byte-for-byte absorbs it."""
    tracked = git_repo / "src" / "tracked.py"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("VALUE = 0\n", encoding="utf-8")
    _git(git_repo, "add", "src/tracked.py")
    _git(git_repo, "commit", "-q", "-m", "seed")
    tracked.write_text("VALUE = 1\n", encoding="utf-8")  # user WIP
    (git_repo / "src" / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    before = snapshot(git_repo)
    assert set(before.user_visible_lines()) == {" M src/tracked.py", "?? src/new.py"}

    _git(git_repo, "add", "src/tracked.py", "src/new.py")
    _git(git_repo, "commit", "-q", "-m", "stage commit")
    after = snapshot(git_repo)

    absorbed = committed_baseline_dirt(before, after, git_repo)
    assert set(absorbed) == {" M src/tracked.py", "?? src/new.py"}
    residue = [e for e in user_visible_changes(before, after) if e not in absorbed]
    assert residue == []


def test_committed_baseline_dirt_keeps_discarded_user_edit_as_residue(git_repo):
    """A stage that throws the user's edit away and commits its own content
    did not absorb dirt: the vanished entry stays residue."""
    tracked = git_repo / "src" / "tracked.py"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    tracked.write_text("VALUE = 0\n", encoding="utf-8")
    _git(git_repo, "add", "src/tracked.py")
    _git(git_repo, "commit", "-q", "-m", "seed")
    tracked.write_text("VALUE = 1  # user WIP\n", encoding="utf-8")
    before = snapshot(git_repo)

    tracked.write_text("VALUE = 2  # stage rewrite\n", encoding="utf-8")
    _git(git_repo, "add", "src/tracked.py")
    _git(git_repo, "commit", "-q", "-m", "stage commit")
    after = snapshot(git_repo)

    assert committed_baseline_dirt(before, after, git_repo) == []
    assert user_visible_changes(before, after) == [" M src/tracked.py"]


def test_committed_baseline_dirt_absorbs_committed_deletion(git_repo):
    gone = git_repo / "src" / "gone.py"
    gone.parent.mkdir(parents=True, exist_ok=True)
    gone.write_text("X = 1\n", encoding="utf-8")
    _git(git_repo, "add", "src/gone.py")
    _git(git_repo, "commit", "-q", "-m", "seed")
    gone.unlink()
    before = snapshot(git_repo)
    assert before.user_visible_lines() == [" D src/gone.py"]

    _git(git_repo, "rm", "-q", "src/gone.py")
    _git(git_repo, "commit", "-q", "-m", "stage commit")
    after = snapshot(git_repo)

    assert committed_baseline_dirt(before, after, git_repo) == [" D src/gone.py"]


def test_committed_baseline_dirt_is_empty_without_head_movement(git_repo):
    (git_repo / "new.txt").write_text("x", encoding="utf-8")
    before = snapshot(git_repo)
    (git_repo / "new.txt").unlink()
    after = snapshot(git_repo)
    assert committed_baseline_dirt(before, after, git_repo) == []
    assert user_visible_changes(before, after) == ["?? new.txt"]


def test_protected_touch_without_content_change_is_not_an_escape(git_repo):
    """An mtime-only change (touch, branch round-trip) must not count as a
    protected-input edit; content and mode changes still do."""
    import os
    import time

    protected = git_repo / "prd.md"
    protected.write_text("immutable", encoding="utf-8")
    subprocess.run(["git", "add", "prd.md"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=git_repo, check=True)
    before = snapshot(git_repo, watched_paths=[protected])

    later = time.time() + 5
    os.utime(protected, (later, later))
    after = snapshot(git_repo, watched_paths=[protected])
    assert before.watched_fingerprints != after.watched_fingerprints
    assert detect_out_of_scope_writes(
        before, after, allowed_scope=[git_repo],
        protected_scope=[protected], repo_root=git_repo,
    ) == []

    protected.write_text("edited", encoding="utf-8")
    edited = snapshot(git_repo, watched_paths=[protected])
    assert any(
        "prd.md" in entry for entry in detect_out_of_scope_writes(
            before, edited, allowed_scope=[git_repo],
            protected_scope=[protected], repo_root=git_repo,
        )
    )
