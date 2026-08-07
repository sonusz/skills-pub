"""v3-core harness-layer bug fixes.

Covers three fixes to dogfood-discovered bugs:

- Bug 1a: cascade multi-upstream freshness for panel verdicts.
- Bug 1b: orchestrator pre-scan routes fresh blocking panel verdicts on
  disk through the same revision loop used by newly-run panels.
- Bug 2: gate-aware filename → producer dispatch (design-review treats
  implemented-spec.md as external to the design packet).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autodev.artifacts.revision_state import filename_to_producer, load_state
from autodev.artifacts.design_packet import write_design_packet
from autodev.artifacts.design_package_history import archive_design_package
from autodev.artifacts.scope import Scope, ScopeItem, write_scope
from autodev.artifacts.verdict import (
    PanelFinding, PanelVerdict, load_verdict, write_verdict,
)
from autodev.revision_loop import DecisionKind, handle_panel_verdict
from autodev.paths import find_repo_root
from autodev.state.atomic import atomic_write
from autodev.state.cascade import StalenessCascade
from autodev.state.hashing import hash_file


@pytest.fixture
def active(tmp_path):
    a = tmp_path / "active"
    a.mkdir()
    return a


def _seed_prd_and_scope(active: Path) -> tuple[str, str]:
    repo_root = find_repo_root(active)
    (repo_root / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n",
        encoding="utf-8",
    )
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    prd = active / "prd.md"
    atomic_write(
        prd,
        "# PRD\n## Problem\np\n## Users\nu\n"
        "## Requirements\n### R1: do\n"
        "## Constraints\n\n## Success criteria\n\n## Out of scope\n",
    )
    prd_h = hash_file(prd)
    (active / "design.md").write_text(
        f"<!-- source: {prd} -->\n<!-- source_hash: {prd_h} -->\n"
        "<!-- written: 2026-04-22 -->\n\n## 1. Context\nx\n"
        "## 2. Primitives & commitments\n"
        "Validation commands: [\"pytest -q\"]\n\nx\n",
        encoding="utf-8",
    )
    scope = Scope(
        source=str(prd), source_hash=prd_h, written="2026-04-22",
        feature="demo", mode="fresh", diff_base="main",
        in_scope=[ScopeItem(id="s-1", description="do it", prd_ref=["R1"], design_ref=["§2"])],
    )
    write_scope(active / "scope.json", scope)
    (active / "trace.md").write_text(
        f"<!-- source: {prd} -->\n<!-- source_hash: {prd_h} -->\n"
        "| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status | Source |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| 1 | s-1.r1 | s-1 | do it | -- | -- | pending | Source: prd:R1 |\n",
        encoding="utf-8",
    )
    (active / "test-plan.md").write_text(
        f"<!-- source: {prd} -->\n<!-- source_hash: {prd_h} -->\n"
        "## Test Strategy\nx\n## Test Cases\n"
        "| Scope ID | Desc | Tier | Edges | Fixtures | Source |\n"
        "|---|---|---|---|---|---|\n"
        "| s-1 | do it | unit | -- | -- | Source: prd:R1 |\n",
        encoding="utf-8",
    )
    write_design_packet(active)
    scope_h = hash_file(active / "scope.json")
    return prd_h, scope_h


def _write_panel_verdict(active: Path, gate: str, scope_h: str) -> Path:
    """Write a panel-<gate>.json with scope.json as a consulted doc."""
    packet = active / "design-packet.json"
    scope_path = active / "scope.json"
    v = PanelVerdict(
        gate=gate, verdict="pass", findings=[],
        source=str(packet), source_hash=hash_file(packet),
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-22T00:00:00Z",
        consulted_docs=[{
            "path": str(scope_path), "hash": scope_h, "priority": "consulted",
        }],
    )
    p = active / f"panel-{gate}.json"
    write_verdict(p, v)
    return p


# ---------- Bug 1a: cascade multi-upstream freshness ----------

def test_cascade_panel_fresh_when_consulted_doc_unchanged(active):
    prd_h, scope_h = _seed_prd_and_scope(active)
    _write_panel_verdict(active, "design-review", scope_h)
    c = StalenessCascade(active)
    assert c.fresh()["panel_design_review"] is True


def test_cascade_panel_stale_when_scope_changes(active):
    """The core fix — scope regen invalidates panel verdict even though
    prd hash is unchanged."""
    prd_h, scope_h = _seed_prd_and_scope(active)
    _write_panel_verdict(active, "design-review", scope_h)
    # Simulate scope regen — rewrite scope.json with a new in_scope
    # item; its hash will differ.
    scope = Scope(
        source=str(active / "prd.md"), source_hash=prd_h, written="2026-04-22",
        feature="demo", mode="fresh", diff_base="main",
        in_scope=[
            ScopeItem(id="s-1", description="do it", prd_ref=["R1"]),
            ScopeItem(id="s-2", description="added", prd_ref=["R1"], design_ref=["§2"]),
        ],
    )
    write_scope(active / "scope.json", scope)
    c = StalenessCascade(active)
    assert c.fresh()["panel_design_review"] is False


def test_cascade_panel_stale_when_consulted_doc_deleted(active):
    prd_h, scope_h = _seed_prd_and_scope(active)
    _write_panel_verdict(active, "design-review", scope_h)
    (active / "scope.json").unlink()
    c = StalenessCascade(active)
    # scope.json deleted → cascade will also mark scope itself stale
    # (not present), and panel_design_review stale via missing upstream.
    # The panel_consulted_docs_fresh check returns False for missing path
    # but upstream-fresh check already catches this; assert fresh is False.
    assert c.fresh()["panel_design_review"] is False


def test_cascade_panel_fresh_when_no_consulted_docs(active):
    """Back-compat: panel verdicts without consulted_docs (legacy) still
    judged fresh on the single-upstream hash check."""
    _seed_prd_and_scope(active)
    packet = active / "design-packet.json"
    v = PanelVerdict(
        gate="design-review", verdict="pass", findings=[],
        source=str(packet), source_hash=hash_file(packet),
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-22T00:00:00Z",
        # no consulted_docs
    )
    write_verdict(active / "panel-design-review.json", v)
    c = StalenessCascade(active)
    assert c.fresh()["panel_design_review"] is True


# ---------- Bug 1b: orchestrator enforces blocking on disk ----------

def _git_init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True)
    (repo / "vendors.yml").write_text(
        "stages:\n"
        "  design: {vendor: claude, model: x}\n"
        "  build: {vendor: claude, model: x}\n"
        "  spec: {vendor: claude, model: x}\n"
        "  review: {vendor: claude, model: x}\n"
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
    return repo


def test_orchestrator_routes_on_disk_blocking_verdict(tmp_path):
    """Even if cascade says panel verdict is fresh, orchestrator routes
    a blocking verdict through revision_loop instead of blindly halting."""
    from autodev.orchestrator import Orchestrator, OrchestratorConfig
    from autodev.state.log import JsonlLog
    from autodev.vendors.config import (
        PanelConfig,
        PanelReviewerSpec,
        PanelSynthesizerSpec,
        ProbeConfig,
        STAGES,
        StageSpec,
        VendorsConfig,
    )

    repo = _git_init_repo(tmp_path)
    feature = "demo"
    active = repo / "docs" / "features" / feature / "active"
    active.mkdir(parents=True)

    # Seed prd + scope + blocking panel verdict.
    _seed_prd_and_scope(active)
    scope_h = hash_file(active / "scope.json")
    packet = active / "design-packet.json"
    v = PanelVerdict(
        gate="design-review", verdict="needs_revision",
        findings=[PanelFinding(
            severity="invariant_violation", vendor="claude",
            summary="blocking finding",
            targets=["primary_pair.scope.json"],
        )],
        source=str(packet), source_hash=hash_file(packet),
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-22T00:00:00Z",
        consulted_docs=[{
            "path": str(active / "scope.json"), "hash": scope_h,
            "priority": "consulted",
        }],
    )
    write_verdict(active / "panel-design-review.json", v)
    from autodev import overrides_api as ov
    ov.record_acknowledge_dirty(active, reason="test", who="t")

    cfg = OrchestratorConfig(
        repo_root=repo,
        vendors=VendorsConfig(
            path=repo / "vendors.yml",
            stages={s: StageSpec(stage=s, vendor="claude", model="x") for s in STAGES},
            panel=PanelConfig(
                reviewers=(
                    PanelReviewerSpec(vendor="claude", model="fake-panel-claude"),
                    PanelReviewerSpec(vendor="agy", model="fake-panel-agy"),
                    PanelReviewerSpec(vendor="codex", model="fake-panel-codex"),
                ),
                synthesizer=PanelSynthesizerSpec(
                    vendor="claude", model="fake-panel-synth"
                ),
            ),
            probe=ProbeConfig(vendor="claude", model="fake-probe"),
        ),
    )
    orch = Orchestrator(cfg)
    decision = orch._enforce_pending_blocking_verdicts(
        active, JsonlLog(active / "log.jsonl"), feature,
    )
    assert decision is not None
    assert decision.kind == DecisionKind.LOCAL_REVISE
    assert decision.stage_to_rerun == "design"
    assert load_state(active).L["design-review"] == 1


def test_orchestrator_skips_enforcement_on_skip_gate(tmp_path):
    """If a skip-gate override is active, the on-disk blocking-verdict
    pre-scan must NOT halt on that gate. Exercises the pre-scan
    function directly rather than going through full advance_one
    (which would then try to dispatch the next gate's panel)."""
    from autodev import overrides_api as ov
    from autodev.orchestrator import Orchestrator, OrchestratorConfig
    from autodev.state.log import JsonlLog
    from autodev.vendors.config import (
        PanelConfig,
        PanelReviewerSpec,
        PanelSynthesizerSpec,
        ProbeConfig,
        STAGES,
        StageSpec,
        VendorsConfig,
    )

    repo = _git_init_repo(tmp_path)
    feature = "demo"
    active = repo / "docs" / "features" / feature / "active"
    active.mkdir(parents=True)

    _seed_prd_and_scope(active)
    scope_h = hash_file(active / "scope.json")
    packet = active / "design-packet.json"
    v = PanelVerdict(
        gate="design-review", verdict="needs_revision",
        findings=[PanelFinding(severity="risk", vendor="claude", summary="x")],
        source=str(packet), source_hash=hash_file(packet),
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-22T00:00:00Z",
        consulted_docs=[{"path": str(active / "scope.json"), "hash": scope_h,
                         "priority": "consulted"}],
    )
    write_verdict(active / "panel-design-review.json", v)
    ov.record_skip_gate(active, gate="design-review", reason="test", who="t")

    orch = Orchestrator(OrchestratorConfig(
        repo_root=repo,
        vendors=VendorsConfig(
            path=repo / "vendors.yml",
            stages={s: StageSpec(stage=s, vendor="claude", model="x") for s in STAGES},
            panel=PanelConfig(
                reviewers=(
                    PanelReviewerSpec(vendor="claude", model="fake-panel-claude"),
                    PanelReviewerSpec(vendor="agy", model="fake-panel-agy"),
                    PanelReviewerSpec(vendor="codex", model="fake-panel-codex"),
                ),
                synthesizer=PanelSynthesizerSpec(
                    vendor="claude", model="fake-panel-synth"
                ),
            ),
            probe=ProbeConfig(vendor="claude", model="fake-probe"),
        ),
    ))
    # Pre-scan directly — should NOT raise because skip-gate is active.
    logger = JsonlLog(active / "log.jsonl")
    orch._enforce_pending_blocking_verdicts(active, logger, feature)


# ---------- Bug 2: gate-aware filename → producer ----------

def test_filename_to_producer_close_approval_excludes_spec():
    """close-approval's gate-specific map deliberately omits
    implemented-spec.md so reviewers cannot route findings back to
    the spec stage (passive describer; cannot fix shipped behavior).
    A reviewer who ignores the prompt and targets implemented-spec.md
    sees the dispatch resolve to None → halt for human."""
    assert filename_to_producer("close-approval", "implemented-spec.md") is None


def test_filename_to_producer_close_approval_routes_to_build():
    assert filename_to_producer("close-approval", "build.json") == "build"


def test_filename_to_producer_close_approval_routes_to_design():
    """close-approval can route to upstream design artifacts."""
    assert filename_to_producer("close-approval", "design.md") == "design"
    assert filename_to_producer("close-approval", "scope.json") == "design"
    assert filename_to_producer("close-approval", "trace.md") == "design"
    assert filename_to_producer("close-approval", "test-plan.md") == "design"


def test_filename_to_producer_design_review_spec_is_external():
    assert filename_to_producer("design-review", "implemented-spec.md") is None


def test_filename_to_producer_design_review_scope_works():
    assert filename_to_producer("design-review", "scope.json") == "design"


def test_filename_to_producer_design_review_unknown_is_halt():
    assert filename_to_producer("design-review", "architecture.md") is None


def test_handle_verdict_design_review_spec_target_halts(active):
    """End-to-end: a blocking finding targeting implemented-spec.md in design-review
    produces a halt, NOT a spec rerun."""
    v = PanelVerdict(
        gate="design-review", verdict="needs_revision",
        findings=[PanelFinding(
            severity="invariant_violation", vendor="claude",
            summary="arch inconsistency",
            targets=["primary_pair.implemented-spec.md"],
        )],
        source="x", source_hash="sha256:" + "0" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-22T00:00:00Z",
    )
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert d.stage_to_rerun is None


def test_handle_verdict_close_spec_target_halts(active):
    """A close-approval reviewer that ignores the prompt and targets
    implemented-spec.md sees the dispatch halt (spec is not in the
    close-approval filename map; non-rerunnable target halts for
    human)."""
    v = PanelVerdict(
        gate="close-approval", verdict="needs_revision",
        findings=[PanelFinding(
            severity="invariant_violation", vendor="claude", summary="x",
            targets=["primary_pair.implemented-spec.md"],
        )],
        source="x", source_hash="sha256:" + "0" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-22T00:00:00Z",
    )
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert d.stage_to_rerun is None


def test_handle_verdict_design_review_scope_and_spec_still_halt(active):
    """In the design-review gate, a finding set that targets both
    scope.json (rerunnable) and implemented-spec.md (external, halt) still
    halts because at least one target is non-rerunnable."""
    v = PanelVerdict(
        gate="design-review", verdict="needs_revision",
        findings=[
            PanelFinding(
                severity="invariant_violation", vendor="claude",
                summary="scope gap", targets=["primary_pair.scope.json"],
            ),
            PanelFinding(
                severity="invariant_violation", vendor="claude",
                summary="arch mismatch", targets=["primary_pair.implemented-spec.md"],
            ),
        ],
        source="x", source_hash="sha256:" + "0" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-22T00:00:00Z",
    )
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert "non-rerunnable" in d.reason


# ---------- Design package snapshots ----------

def test_design_package_history_archives_distinct_package_versions(git_repo):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_prd_and_scope(active)
    (active / "design-changelog.json").write_text(
        json.dumps({
            "kind": "design-changelog",
            "schema_version": 1,
            "entries": [{
                "round": 1,
                "trigger": "initial",
                "reason": "first pass",
                "artifacts_changed": ["design.md", "scope.json", "trace.md", "test-plan.md"],
                "added": [],
                "removed": [],
            }],
        }),
        encoding="utf-8",
    )

    first = archive_design_package(active)
    duplicate = archive_design_package(active)
    assert duplicate == first

    original_design = (first / "design.md").read_text(encoding="utf-8")
    current_design = (active / "design.md").read_text(encoding="utf-8")
    (active / "design.md").write_text(
        current_design + "\n## Changed\nnew package version\n",
        encoding="utf-8",
    )

    second = archive_design_package(active)
    snapshots = sorted((active / "design-package-history").glob("package-*"))
    assert snapshots == [first, second]
    assert "new package version" not in original_design
    assert "new package version" not in (first / "design.md").read_text(encoding="utf-8")
    assert "new package version" in (second / "design.md").read_text(encoding="utf-8")
    assert (second / "scope.json").exists()
    assert (second / "trace.md").exists()
    assert (second / "test-plan.md").exists()
    assert (second / "design-changelog.json").exists()

    manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "design-package-snapshot"
    assert manifest["package_hash"].startswith("sha256:")
    assert {a["path"] for a in manifest["artifacts"]} == {
        "design.md",
        "scope.json",
        "trace.md",
        "test-plan.md",
        "design-changelog.json",
    }
    first_manifest = json.loads((first / "manifest.json").read_text())
    first_git = first_manifest["git_snapshot"]
    second_git = manifest["git_snapshot"]
    assert first_git["ref"] == "refs/autodev/design/demo/package-001"
    assert second_git["ref"] == "refs/autodev/design/demo/package-002"
    assert second_git["parent_ref"] == first_git["ref"]
    assert second_git["parent_commit"] == first_git["commit"]
    assert subprocess.run(
        ["git", "rev-parse", "--verify", f"{second_git['ref']}^{{commit}}"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == second_git["commit"]
    design_path = active.relative_to(git_repo) / "design.md"
    revision_diff = subprocess.run(
        [
            "git", "diff", "--no-ext-diff", first_git["ref"],
            second_git["ref"], "--", design_path.as_posix(),
        ],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "+new package version" in revision_diff
    # Package refs use a throwaway index and never stage the user's worktree.
    assert subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    assert not (active / "design-rework-memory.json").exists()
