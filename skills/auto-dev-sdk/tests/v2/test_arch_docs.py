"""v2-12: architecture-doc discovery (collect-all, priority, cap)."""
from __future__ import annotations

from pathlib import Path

from autodev.arch_docs import discover


def test_discover_empty_repo(git_repo):
    docs = discover(git_repo, feature="demo")
    assert docs == []


def test_discover_feature_local_architecture(git_repo):
    planned = git_repo / "docs" / "features" / "demo" / "planned"
    planned.mkdir(parents=True)
    (planned / "architecture.md").write_text("# arch\n")
    docs = discover(git_repo, feature="demo")
    names = [d.path.name for d in docs]
    assert "architecture.md" in names
    assert docs[0].priority == 1


def test_discover_repo_global(git_repo):
    (git_repo / "docs").mkdir(exist_ok=True)
    (git_repo / "docs" / "architecture.md").write_text("# arch\n")
    (git_repo / "CLAUDE.md").write_text("# claude\n")
    docs = discover(git_repo, feature="demo")
    priorities = sorted(d.priority for d in docs)
    assert 2 in priorities  # docs/architecture.md
    assert 3 in priorities  # CLAUDE.md


def test_discover_shipped_specs_limit_5(git_repo):
    for i in range(7):
        complete = git_repo / "docs" / "features" / f"other{i}" / "complete"
        complete.mkdir(parents=True)
        (complete / "spec.md").write_text(f"# spec {i}\n")
    docs = discover(git_repo, feature="demo")
    shipped = [d for d in docs if d.priority == 4]
    assert len(shipped) == 5


def test_discover_cap_respected(git_repo):
    planned = git_repo / "docs" / "features" / "demo" / "planned"
    planned.mkdir(parents=True)
    # Create a large doc
    (planned / "architecture.md").write_text("x" * 100_000)
    # Plus a CLAUDE.md
    (git_repo / "CLAUDE.md").write_text("y" * 150_000)  # together > 200K
    docs = discover(git_repo, feature="demo")
    total = sum(d.size for d in docs)
    assert total <= 200_000
