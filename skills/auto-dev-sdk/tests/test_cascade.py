"""ad-2: staleness cascade."""
from __future__ import annotations

from auto_dev.artifacts.common import write_markdown_with_hash
from auto_dev.state.atomic import atomic_write, atomic_write_json
from auto_dev.state.cascade import StalenessCascade
from auto_dev.state.hashing import hash_file


def _seed_full_chain(active):
    active.mkdir(parents=True, exist_ok=True)
    prd = active / "prd.md"
    atomic_write(prd, "# PRD\n\n## Problem\n\n## Users\n\n## Requirements\n\n## Constraints\n\n## Success\n\n## Out of scope\n")
    prd_hash = hash_file(prd)

    scope = active / "scope.json"
    atomic_write_json(scope, {
        "source": str(prd), "source_hash": prd_hash, "written": "2026-04-19",
        "feature": "toy", "mode": "fresh", "diff_base": "main",
        "in_scope": [{"id": "t-1", "description": "x", "prd_ref": "§1", "status": "active"}],
        "excluded": [],
    })
    scope_hash = hash_file(scope)

    write_markdown_with_hash(active / "trace.md", "body", source=str(scope), source_hash=scope_hash)
    write_markdown_with_hash(active / "test-plan.md", "body", source=str(scope), source_hash=scope_hash)
    atomic_write_json(active / "build.json", {
        "source": str(scope), "source_hash": scope_hash, "written": "2026-04-19",
        "test_results": {"passed": 1, "failed": 0, "skipped": 0},
        "lint": {"passed": True},
        "files_changed": [], "deviations": [], "blocking": False,
    })
    write_markdown_with_hash(active / "spec.md", "body", source=str(scope), source_hash=scope_hash)
    write_markdown_with_hash(active / "review.md", "body", source=str(scope), source_hash=scope_hash)


def test_full_chain_fresh(tmp_path):
    _seed_full_chain(tmp_path)
    cascade = StalenessCascade(tmp_path)
    fresh = cascade.fresh()
    assert all(fresh.values())
    assert cascade.next_stage() == "done"


def test_prd_change_invalidates_scope_and_down(tmp_path):
    _seed_full_chain(tmp_path)
    atomic_write(tmp_path / "prd.md", "# PRD modified\n\n## Problem\n\n## Users\n\n## Requirements\n\n## Constraints\n\n## Success\n\n## Out of scope\n")
    cascade = StalenessCascade(tmp_path)
    fresh = cascade.fresh()
    assert fresh["prd"] is True
    assert fresh["scope"] is False
    assert fresh["trace"] is False
    assert fresh["build"] is False
    assert fresh["review"] is False
    # Next stage jumps back to scope.
    assert cascade.next_stage() == "scope"


def test_scope_bytes_change_invalidates_downstream_but_not_scope(tmp_path):
    _seed_full_chain(tmp_path)
    # Mutate scope.json bytes (e.g. reformat). Scope itself is still fresh
    # against its PRD upstream, but every DOWNSTREAM artifact's recorded
    # scope-hash no longer matches current scope.json bytes.
    (tmp_path / "scope.json").write_text((tmp_path / "scope.json").read_text() + "\n")
    cascade = StalenessCascade(tmp_path)
    fresh = cascade.fresh()
    assert fresh["prd"] is True
    assert fresh["scope"] is True
    assert fresh["trace"] is False
    assert fresh["test_plan"] is False
    assert fresh["build"] is False
    assert fresh["spec"] is False
    assert fresh["review"] is False
    assert cascade.next_stage() == "trace"


def test_missing_artifact_not_fresh(tmp_path):
    _seed_full_chain(tmp_path)
    (tmp_path / "review.md").unlink()
    cascade = StalenessCascade(tmp_path)
    assert cascade.fresh()["review"] is False
    assert cascade.next_stage() == "review"
