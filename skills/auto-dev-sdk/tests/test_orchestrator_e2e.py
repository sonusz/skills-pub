"""ad-9, ad-10, ad-11: orchestrator end-to-end with FakeVendor."""
from __future__ import annotations

import json
import textwrap

import pytest

from auto_dev.artifacts.common import write_markdown_with_hash
from auto_dev.artifacts.scope import Scope, ScopeItem, write_scope
from auto_dev.errors import GatePending, LockConflict
from auto_dev.orchestrator import Orchestrator, OrchestratorConfig, status
from auto_dev.stages import gates
from auto_dev.stages.paths import FeaturePaths
from auto_dev.state.atomic import atomic_write
from auto_dev.state.hashing import hash_file


def _seed_feature(repo_root, feature="toy"):
    """Seed a planned/ PRD + later the active/scope.json (manually, SDK doesn't auto-scope)."""
    fp = FeaturePaths(repo_root=repo_root, feature=feature)
    planned = fp.status_dir("planned")
    planned.mkdir(parents=True, exist_ok=True)
    prd_text = textwrap.dedent(
        """\
        # PRD: toy

        ## 1. Problem
        placeholder

        ## 2. Users
        devs

        ## 3. Requirements
        R1 do the thing

        ## 4. Constraints
        none

        ## 5. Success
        it works

        ## 6. Out of scope
        nothing
        """
    )
    atomic_write(planned / "prd.md", prd_text)
    return fp


def _seed_scope(active, prd_path):
    """Write scope.json referencing the PRD. Hash captured at seed time."""
    prd_hash = hash_file(prd_path)
    scope = Scope(
        source=str(prd_path),
        source_hash=prd_hash,
        written="2026-04-19",
        feature="toy",
        mode="fresh",
        diff_base="main",
        in_scope=[ScopeItem(id="toy-1", description="thing", prd_ref="§3", status="active")],
    )
    write_scope(active / "scope.json", scope)


def test_implement_blocks_on_prd_review(repo_root, fake_vendor, fake_vendors_config):
    fp = _seed_feature(repo_root)
    orch = Orchestrator(OrchestratorConfig(
        repo_root=repo_root, vendors=fake_vendors_config,
        prd_review_mode="none",
    ))
    with pytest.raises(GatePending) as exc:
        orch.implement("toy")
    assert exc.value.gate == "prd-review"


def test_implement_full_pipeline(repo_root, fake_vendor, fake_vendors_config):
    fp = _seed_feature(repo_root)
    orch = Orchestrator(OrchestratorConfig(
        repo_root=repo_root, vendors=fake_vendors_config,
        prd_review_mode="none", yes=False,
    ))

    # First run — blocked on prd-review.
    with pytest.raises(GatePending):
        orch.implement("toy")
    active = fp.active()
    assert active.is_dir()

    # Approve PRD + author scope.json (harness doesn't auto-scope in v0.1).
    gates.approve(active, "prd-review")
    _seed_scope(active, active / "prd.md")

    # Script fake vendor responses for each stage.
    fake_vendor.script(stage_tag="fake-plan", response={
        "trace_md": "| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status |\n|---|---|---|---|---|---|---|\n| 1 | toy-1.r1 | toy-1 | thing | -- | -- | pending |\n",
        "test_plan_md": "## Test Strategy\nTiered tests.\n\n## Test Cases\n| Scope ID | Description |\n|---|---|\n| toy-1 | happy path |\n\n## Coverage\nNo gaps.\n",
        "row_count": 1,
        "coverage_gaps": [],
    })
    fake_vendor.script(stage_tag="fake-implement", response={
        "test_results": {"passed": 1, "failed": 0, "skipped": 0, "cmd": "pytest"},
        "lint": {"passed": True},
        "files_changed": ["x.py"],
        "deviations": [],
        "blocking": False,
    })
    fake_vendor.script(stage_tag="fake-spec", response={
        "spec_md": "## 1. Purpose\nx\n## 2. Users\ny\n## 3. Contract\n## 4. Data model\n## 5. Architecture\n## 6. Out of scope\n## 7. Testing\n## 8. Operational notes\n",
        "readme_md": "# README\nsee spec.md\n",
        "envelope": False,
        "sub_files": [],
    })
    fake_vendor.script(stage_tag="fake-review", response={
        "review_md": "# Review\nFullyImplemented.\n",
        "classifications": [{"id": "toy-1", "classification": "FullyImplemented", "notes": "ok"}],
        "deviations": [],
    })

    # Second run — advances through plan, build, spec, review; stops at close-approval.
    with pytest.raises(GatePending) as exc:
        orch.implement("toy")
    assert exc.value.gate == "close-approval"

    # All artifacts present.
    for name in ("trace.md", "test-plan.md", "build.json", "spec.md", "README.md", "review.md"):
        assert (active / name).exists(), f"missing {name}"


def test_lock_conflict(repo_root, fake_vendors_config):
    fp = _seed_feature(repo_root)
    # Pre-seed an active lock.
    active = fp.active()
    fp.base.mkdir(parents=True, exist_ok=True)
    # Promote planned to active so lock dir has a parent.
    import shutil
    shutil.move(str(fp.status_dir("planned")), str(active))
    (active / ".lock").mkdir()
    (active / ".lock" / "owner.json").write_text(json.dumps({"session_id": "other"}))

    orch = Orchestrator(OrchestratorConfig(repo_root=repo_root, vendors=fake_vendors_config))
    with pytest.raises(LockConflict):
        orch.implement("toy")


def test_status_on_fresh_feature(repo_root):
    _seed_feature(repo_root)
    report = status(repo_root, "toy")
    assert report["exists"]
    assert report["status"] == "planned"
