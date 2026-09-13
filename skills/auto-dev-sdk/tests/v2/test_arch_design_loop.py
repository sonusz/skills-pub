"""arch-design / arch-review loop -- Stage A coverage (detail §9.3, tests 1/2/7/8).

Covers only the harness-layer primitives introduced in Stage A: cascade
freshness propagation from arch-design.md, the arch-review.json schema/
freshness helper, and the two session mechanisms (shared design-role
session key, and the `credit-turn` rotation-credit subcommand). Orchestrator
loop behavior (tests 3/4/5/6/9) is Stage B and lives elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from autodev.artifacts.arch_review import arch_review_fresh
from autodev.artifacts.common import write_markdown_with_hash
from autodev.artifacts.design_packet import write_design_packet
from autodev.artifacts.scope import Scope, ScopeItem, write_scope
from autodev.artifacts.verdict import PanelVerdict, write_verdict
from autodev.paths import find_repo_root
from autodev.state.atomic import atomic_write, atomic_write_json
from autodev.state.cascade import StalenessCascade
from autodev.state.hashing import hash_file
from autodev.vendors.session_keys import (
    ARCH_REVIEW_SESSION_MAX_TURNS,
    feature_session_key,
    session_max_turns_for_role,
)


SESSION_HELPER = (
    Path(__file__).resolve().parents[4]
    / "shared" / "vendors" / "scripts" / "session-state.py"
)


# ---------- test 1: cascade freshness propagation from arch-design.md ----

def _seed_through_panel_design_review(active: Path) -> None:
    """Seed prd -> arch_design -> arch_review(pass) -> {design, scope,
    trace, test_plan} -> design_packet -> panel_design_review(pass), all
    hash-consistent under the new arch_design-anchored chain (core R4)."""
    repo_root = find_repo_root(active)
    (repo_root / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n", encoding="utf-8",
    )
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )

    prd = active / "prd.md"
    atomic_write(
        prd,
        "# PRD\n## 1. Problem\n## 2. Users\n## 3. Requirements\n"
        "## 4. Constraints\n## 5. Success\n## 6. Out of scope\n",
    )
    prd_h = hash_file(prd)

    arch_design = active / "arch-design.md"
    write_markdown_with_hash(
        arch_design,
        "## 1. Goal\nbody\n## 5. PRD coverage\nbody\n",
        source=str(prd), source_hash=prd_h,
    )
    arch_design_h = hash_file(arch_design)

    atomic_write_json(active / "arch-review.json", {
        "kind": "arch-review",
        "source": str(arch_design),
        "source_hash": arch_design_h,
        "prd_hash": prd_h,
        "written": "2026-04-20T00:00:00Z",
        "verdict": "pass",
        "findings": [],
    })

    write_markdown_with_hash(
        active / "design.md",
        "## 2. Primitives & commitments\nValidation commands: [\"pytest -q\"]\n\nbody\n",
        source=str(arch_design), source_hash=arch_design_h,
    )
    write_scope(active / "scope.json", Scope(
        source=str(arch_design), source_hash=arch_design_h, written="2026-04-20",
        feature="demo", mode="fresh", diff_base="main",
        in_scope=[ScopeItem(id="s-1", description="x", prd_ref=["§1"], design_ref=["§1"])],
    ))
    write_markdown_with_hash(active / "trace.md", "body\n",
                             source=str(arch_design), source_hash=arch_design_h)
    write_markdown_with_hash(active / "test-plan.md", "body\n",
                             source=str(arch_design), source_hash=arch_design_h)

    packet = write_design_packet(active)
    packet_h = hash_file(packet)
    design_h = hash_file(active / "design.md")
    scope_h = hash_file(active / "scope.json")

    v_design = PanelVerdict(
        gate="design-review", verdict="pass", findings=[],
        source=str(packet), source_hash=packet_h,
        prompt_file="x", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-20T00:00:00Z",
        consulted_docs=[
            {"path": str(active / "design.md"), "hash": design_h, "priority": "consulted"},
            {"path": str(active / "scope.json"), "hash": scope_h, "priority": "consulted"},
            {"path": str(active / "trace.md"), "hash": hash_file(active / "trace.md"), "priority": "consulted"},
            {"path": str(active / "test-plan.md"), "hash": hash_file(active / "test-plan.md"), "priority": "consulted"},
            {"path": str(prd), "hash": prd_h, "priority": "consulted"},
        ],
    )
    write_verdict(active / "panel-design-review.json", v_design)


def test_arch_design_mutation_invalidates_downstream_chain(feature_active):
    _seed_through_panel_design_review(feature_active)
    c = StalenessCascade(feature_active)
    fresh = c.fresh()
    assert fresh["prd"] is True
    assert fresh["arch_design"] is True
    assert fresh["arch_review"] is True
    assert fresh["design"] is True
    assert fresh["scope"] is True
    assert fresh["trace"] is True
    assert fresh["test_plan"] is True
    assert fresh["design_packet"] is True
    assert fresh["panel_design_review"] is True

    # Mutate arch-design.md by a single byte (append), leaving prd.md
    # untouched.
    with (feature_active / "arch-design.md").open("a", encoding="utf-8") as fh:
        fh.write("x")

    c2 = StalenessCascade(feature_active)
    fresh2 = c2.fresh()
    assert fresh2["prd"] is True
    assert fresh2["arch_review"] is False
    assert fresh2["design"] is False
    assert fresh2["scope"] is False
    assert fresh2["trace"] is False
    assert fresh2["test_plan"] is False
    assert fresh2["design_packet"] is False
    assert fresh2["panel_design_review"] is False


# ---------- test 2: arch_review_fresh ----

def _write_arch_design(active: Path) -> tuple[Path, str, str]:
    prd = active / "prd.md"
    atomic_write(prd, "# PRD\n### R1: thing\n")
    prd_h = hash_file(prd)
    arch_design = active / "arch-design.md"
    write_markdown_with_hash(
        arch_design, "## 1. Goal\nbody\n", source=str(prd), source_hash=prd_h,
    )
    return arch_design, hash_file(arch_design), prd_h


def test_arch_review_fresh_pass(tmp_path):
    active = tmp_path
    arch_design, arch_design_h, prd_h = _write_arch_design(active)
    review_path = active / "arch-review.json"
    atomic_write_json(review_path, {
        "kind": "arch-review",
        "source": str(arch_design),
        "source_hash": arch_design_h,
        "prd_hash": prd_h,
        "written": "2026-04-20T00:00:00Z",
        "verdict": "pass",
        "findings": [],
    })
    assert arch_review_fresh(review_path, arch_design) is True


def test_arch_review_fresh_false_on_hash_mismatch(tmp_path):
    active = tmp_path
    arch_design, arch_design_h, prd_h = _write_arch_design(active)
    review_path = active / "arch-review.json"
    atomic_write_json(review_path, {
        "kind": "arch-review",
        "source": str(arch_design),
        "source_hash": "sha256:" + "0" * 64,  # stale/wrong hash
        "prd_hash": prd_h,
        "written": "2026-04-20T00:00:00Z",
        "verdict": "pass",
        "findings": [],
    })
    assert arch_review_fresh(review_path, arch_design) is False


def test_arch_review_fresh_false_on_needs_revision(tmp_path):
    active = tmp_path
    arch_design, arch_design_h, prd_h = _write_arch_design(active)
    review_path = active / "arch-review.json"
    atomic_write_json(review_path, {
        "kind": "arch-review",
        "source": str(arch_design),
        "source_hash": arch_design_h,
        "prd_hash": prd_h,
        "written": "2026-04-20T00:00:00Z",
        "verdict": "needs_revision",
        "findings": [{
            "category": "missing", "prd_ref": "R1",
            "evidence": "arch-design.md §2", "problem": "no coverage",
            "correction": "add a component",
        }],
    })
    assert arch_review_fresh(review_path, arch_design) is False


def test_arch_review_fresh_false_on_missing_field(tmp_path):
    active = tmp_path
    arch_design, arch_design_h, prd_h = _write_arch_design(active)
    review_path = active / "arch-review.json"
    # Missing "kind" -- schema-invalid.
    atomic_write_json(review_path, {
        "source": str(arch_design),
        "source_hash": arch_design_h,
        "prd_hash": prd_h,
        "written": "2026-04-20T00:00:00Z",
        "verdict": "pass",
        "findings": [],
    })
    assert arch_review_fresh(review_path, arch_design) is False


def test_arch_review_fresh_false_when_missing(tmp_path):
    assert arch_review_fresh(tmp_path / "arch-review.json", tmp_path / "arch-design.md") is False


# ---------- test 7: session key sharing + arch-review max turns ----

def test_arch_design_shares_design_session_key(feature_active):
    assert (
        feature_session_key(feature_active, "arch-design")
        == feature_session_key(feature_active, "design")
    )


def test_arch_review_session_max_turns_is_three():
    assert session_max_turns_for_role("arch-review") == 3
    assert ARCH_REVIEW_SESSION_MAX_TURNS == 3


# ---------- test 8: session-state.py credit-turn ----

def _key_sha256(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def test_credit_turn_decrements_floored_at_zero(tmp_path):
    state_dir = tmp_path / "sessions"
    state_dir.mkdir()
    key = "autodev:v1:demo:design"
    key_hash = _key_sha256(key)

    def _record(turn_count: int, session_id: str) -> dict:
        return {
            "schema_version": 1,
            "identity_hash": hashlib.sha256(session_id.encode()).hexdigest(),
            "key_sha256": key_hash,
            "vendor": "claude",
            "model": "x",
            "cwd": "",
            "transport_sha256": "0" * 64,
            "session_id": session_id,
            "session_turn_count": turn_count,
            "session_max_turns": 15,
            "updated_at": "2026-04-20T00:00:00Z",
        }

    rec_a = state_dir / "a.json"
    rec_a.write_text(json.dumps(_record(3, "sess-aaaaaaaaaa")), encoding="utf-8")
    rec_b = state_dir / "b.json"
    rec_b.write_text(json.dumps(_record(0, "sess-bbbbbbbbbb")), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable, str(SESSION_HELPER), "credit-turn",
            "--state-dir", str(state_dir), "--key", key,
        ],
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "2"

    a_after = json.loads(rec_a.read_text())
    b_after = json.loads(rec_b.read_text())
    assert a_after["session_turn_count"] == 2
    assert a_after["session_id"] == "sess-aaaaaaaaaa"
    assert b_after["session_turn_count"] == 0
    assert b_after["session_id"] == "sess-bbbbbbbbbb"


def test_credit_turn_no_state_dir_prints_zero(tmp_path):
    proc = subprocess.run(
        [
            sys.executable, str(SESSION_HELPER), "credit-turn",
            "--state-dir", str(tmp_path / "does-not-exist"),
            "--key", "some-key",
        ],
        text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0"
