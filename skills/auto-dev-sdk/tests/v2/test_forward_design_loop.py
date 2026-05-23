"""Forward design loop dogfood regressions.

These tests exercise the accepted forward-path contract:

prd -> design -> design-packet -> design-review -> accepted-design -> build
"""
from __future__ import annotations

import argparse
import autodev.artifacts.design_packet as design_packet_module
import json
from pathlib import Path

import pytest

from autodev.artifacts.common import write_markdown_with_hash
from autodev.artifacts.design_packet import (
    accepted_design_fresh,
    build_design_packet,
    design_packet_fresh,
    write_accepted_design,
)
from autodev.artifacts.revision_state import clear_state
from autodev.artifacts.verdict import load_verdict, write_verdict, PanelVerdict
from autodev.artifacts.workflow_state import load_workflow_state
from autodev.cli import cmd_prd
from autodev.errors import SchemaError
from autodev.panel import run_panel_gate, verdict_exists_and_valid
from autodev.panel.runner import FAKE_INVOKER_ENV, run_panel_gate_internal
from autodev.prompts_loader import render_stage_prompt
from autodev.revision_loop import DecisionKind, handle_panel_verdict
from autodev.state.atomic import atomic_write, atomic_write_json
from autodev.state.hashing import hash_file
from autodev.vendors.config import (
    PanelConfig,
    PanelReviewerSpec,
    PanelSynthesizerSpec,
)


def _write_feature_architecture(active: Path, *, path: str = "docs/architecture-proposal.md") -> None:
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n"
        "This feature uses:\n\n"
        f"- `{path}`\n",
        encoding="utf-8",
    )


def _seed_forward_feature(active: Path) -> None:
    repo_root = active.parents[3]
    (repo_root / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n\n## 4.2 Design packet\n",
        encoding="utf-8",
    )
    (repo_root / "docs" / "architecture.md").write_text(
        "# Legacy Architecture\n",
        encoding="utf-8",
    )
    _write_feature_architecture(active)

    prd = active / "prd.md"
    atomic_write(
        prd,
        "# PRD: forward loop\n\n"
        "## Problem\nForward design loop.\n\n"
        "## Users\nHarness.\n\n"
        "## Requirements\n"
        "### R1: build a design packet.\n"
        "### R2: review the packet.\n"
        "### R3: write accepted design.\n"
        "### R4: block build until acceptance.\n"
        "### R5: forward path only.\n"
        "### R6: verify without live vendor calls.\n\n"
        "## Constraints\nUse docs/architecture-proposal.md and atomic writes.\n\n"
        "## Success criteria\nForward path is fresh and testable.\n\n"
        "## Out of scope\nLive vendor review.\n",
    )
    prd_hash = hash_file(prd)

    write_markdown_with_hash(
        active / "design.md",
        (
            "# Design\n\n"
            "## 2. Primitives & commitments\n\n"
            "### SS2.1 Minimal workflow-state root context\n\n"
            "Bootstrap workflow-state.\n\n"
            "### SS2.2 Design packet\n\n"
            "Validation commands: [\"pytest -q\"]\n\n"
            "Packet contract.\n"
        ),
        source=str(prd),
        source_hash=prd_hash,
    )
    atomic_write_json(
        active / "scope.json",
        {
            "source": str(prd),
            "source_hash": prd_hash,
            "written": "2026-04-26",
            "feature": "demo",
            "mode": "fresh",
            "diff_base": "main",
            "in_scope": [
                {
                    "id": "fdl-1",
                    "description": "packet",
                    "prd_ref": ["R1", "R6", "Constraints"],
                    "design_ref": ["SS2.1", "SS2.2"],
                    "status": "active",
                }
            ],
            "excluded": [
                {
                    "id": "x-R2", "description": "R2 review the packet",
                    "reason": "covered by panel gate, not a build scope item",
                },
                {
                    "id": "x-R3", "description": "R3 write accepted design",
                    "reason": "harness-owned artifact, not a build scope item",
                },
                {
                    "id": "x-R4", "description": "R4 block build until acceptance",
                    "reason": "harness-owned gate logic, not a build scope item",
                },
                {
                    "id": "x-R5", "description": "R5 forward path only",
                    "reason": "test-scope constraint, not a build scope item",
                },
            ],
        },
    )
    write_markdown_with_hash(
        active / "trace.md",
        (
            "# Trace\n\n"
            "| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status | Source |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| 1 | fdl-1.r1 | fdl-1 | packet exists | -- | -- | pending | Source: prd:R1 |\n"
        ),
        source=str(prd),
        source_hash=prd_hash,
    )
    write_markdown_with_hash(
        active / "test-plan.md",
        (
            "# Test plan\n\n"
            "## Test Cases\n\n"
            "| Scope ID | Description | Tier | Edges | Fixtures | Source |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| fdl-1 | packet flow | integration | -- | repo | Source: prd:R1 |\n"
        ),
        source=str(prd),
        source_hash=prd_hash,
    )


def _panel_config() -> PanelConfig:
    return PanelConfig(
        reviewers=(
            PanelReviewerSpec(vendor="claude", model="fake"),
            PanelReviewerSpec(vendor="gemini", model="fake"),
            PanelReviewerSpec(vendor="codex", model="fake"),
        ),
        synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake"),
        reviewer_probe_interval_sec=10,
        synthesizer_probe_interval_sec=10,
    )


def _write_fake_panel_script(
    tmp_path: Path,
    *,
    synth_payload: dict | str,
    touch_path: Path | None = None,
) -> Path:
    payload = synth_payload if isinstance(synth_payload, str) else json.dumps(synth_payload)
    touch_line = f'echo invoked > "{touch_path}"\n' if touch_path is not None else ""
    script = tmp_path / "fake-panel.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cat > /dev/null\n"
        f"{touch_line}"
        'if [[ "${AUTODEV_PANEL_FAKE_ROLE:-reviewer}" == "reviewer" ]]; then\n'
        "  echo 'Verdict: pass'\n"
        "  exit 0\n"
        "fi\n"
        f"cat <<'EOF'\n{payload}\nEOF\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_cmd_prd_bootstraps_workflow_state_from_architecture_input(git_repo, tmp_path):
    feature = "demo"
    planned = git_repo / "docs" / "features" / feature / "planned"
    planned.mkdir(parents=True)
    _write_feature_architecture(planned)
    (git_repo / "docs" / "architecture-proposal.md").write_text("# proposal\n", encoding="utf-8")

    source_prd = tmp_path / "prd.md"
    source_prd.write_text(
        "# PRD: demo\n\n"
        "## Problem\np\n\n"
        "## Users\nu\n\n"
        "## Requirements\n### R1: do the thing.\n\n"
        "## Constraints\nc\n\n"
        "## Success criteria\ns\n\n"
        "## Out of scope\no\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        feature=feature,
        repo_root=str(git_repo),
        vendors_yml=None,
        from_file=str(source_prd),
    )
    assert cmd_prd(args) == 0

    state = load_workflow_state(planned)
    assert state["root_context_paths"] == ["docs/architecture-proposal.md"]


def test_build_design_packet_bootstraps_workflow_state_and_records_packet_inputs(git_repo):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)

    packet = build_design_packet(active)

    state = load_workflow_state(active)
    assert state["root_context_paths"] == ["docs/architecture-proposal.md"]
    assert packet["input"]["path"] == str(active / "prd.md")
    assert packet["validation_commands"] == ["pytest -q"]
    assert packet["context_refs"] == [
        {
            "path": "docs/architecture-proposal.md",
            "hash": hash_file(git_repo / "docs" / "architecture-proposal.md"),
        }
    ]
    assert packet["dev_input_hash"] == packet["review_subject_hash"]
    assert packet["response_to_feedback"] == []


def test_design_packet_hash_semantics_and_validation_commands(git_repo):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)

    packet = build_design_packet(active)
    old_input = packet["input_fingerprint"]
    old_subject = packet["review_subject_hash"]
    regenerated = build_design_packet(active)
    assert regenerated["review_subject_hash"] == old_subject

    design_path = active / "design.md"
    design_path.write_text(
        design_path.read_text(encoding="utf-8").replace("Packet contract.", "Packet contract updated."),
        encoding="utf-8",
    )
    changed_design = build_design_packet(active)
    assert changed_design["input_fingerprint"] == old_input
    assert changed_design["review_subject_hash"] != old_subject

    design_path.write_text(
        design_path.read_text(encoding="utf-8").replace("Packet contract updated.", "Packet contract."),
        encoding="utf-8",
    )
    proposal = git_repo / "docs" / "architecture-proposal.md"
    proposal.write_text("# Architecture Proposal\n\n## 4.2 Design packet changed\n", encoding="utf-8")
    changed_context = build_design_packet(active)
    assert changed_context["input_fingerprint"] != old_input
    assert changed_context["review_subject_hash"] != old_subject

    design_path.write_text(
        design_path.read_text(encoding="utf-8").replace(
            'Validation commands: ["pytest -q"]',
            'Validation commands: ["pytest -q", "pytest -q tests/v2"]',
        ),
        encoding="utf-8",
    )
    changed_commands = build_design_packet(active)
    assert changed_commands["input_fingerprint"] == changed_context["input_fingerprint"]
    assert changed_commands["review_subject_hash"] != changed_context["review_subject_hash"]

    design_path.write_text(
        design_path.read_text(encoding="utf-8").replace(
            'Validation commands: ["pytest -q", "pytest -q tests/v2"]',
            "Validation commands: []",
        ),
        encoding="utf-8",
    )
    with pytest.raises(SchemaError):
        build_design_packet(active)


def test_build_design_packet_rejects_invalid_resolved_context_refs(git_repo, monkeypatch):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)

    authoritative = design_packet_module._authoritative_context_refs(active, bootstrap=True)
    bad_context_sets = {
        "missing": [],
        "duplicate": [authoritative[0], authoritative[0]],
        "extra": [
            *authoritative,
            {
                "path": "docs/architecture.md",
                "hash": hash_file(git_repo / "docs" / "architecture.md"),
            },
        ],
        "stale": [
            {
                "path": authoritative[0]["path"],
                "hash": "sha256:" + ("9" * 64),
            }
        ],
    }

    for name, bad_refs in bad_context_sets.items():
        monkeypatch.setattr(
            design_packet_module,
            "_context_refs",
            lambda _active, *, bootstrap=True, refs=bad_refs: json.loads(json.dumps(refs)),
        )
        with pytest.raises(SchemaError, match="context"):
            build_design_packet(active)
        monkeypatch.undo()


def test_design_packet_references_changelog_when_present(git_repo):
    """response_to_feedback points at design-changelog.json when it exists.

    The legacy round-NNN.json filtering is gone; the changelog is the
    single source of truth for design history in the active loop.
    """
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)
    first_packet = build_design_packet(active)
    atomic_write_json(active / "design-packet.json", first_packet)

    # No changelog → empty response_to_feedback.
    assert first_packet["response_to_feedback"] == []

    # Once a changelog exists, response_to_feedback references it.
    changelog_path = active / "design-changelog.json"
    atomic_write_json(changelog_path, {
        "kind": "design-changelog",
        "schema_version": 1,
        "entries": [
            {"round": 1, "trigger": "initial", "reason": "first pass",
             "artifacts_changed": ["design.md"], "added": [], "removed": []},
        ],
    })
    packet = build_design_packet(active)
    assert packet["response_to_feedback"] == [
        {"path": str(changelog_path), "hash": hash_file(changelog_path)},
    ]


def test_review_subject_hash_ignores_packet_write_and_self_field_rewrites(git_repo):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)

    packet = build_design_packet(active)
    original_subject_hash = packet["review_subject_hash"]
    atomic_write_json(active / "design-packet.json", packet)

    mutated = json.loads((active / "design-packet.json").read_text(encoding="utf-8"))
    mutated["written"] = "2099-01-01T00:00:00+00:00"
    mutated["source"] = str(active / "renamed-design-alias.md")
    mutated["review_subject_hash"] = "sha256:" + ("7" * 64)
    mutated["dev_input_hash"] = "sha256:" + ("8" * 64)
    atomic_write_json(active / "design-packet.json", mutated)

    regenerated = build_design_packet(active)
    assert regenerated["review_subject_hash"] == original_subject_hash
    assert regenerated["dev_input_hash"] == original_subject_hash


def test_design_packet_fresh_rejects_validation_command_and_context_drift(git_repo):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)
    packet = build_design_packet(active)
    atomic_write_json(active / "design-packet.json", packet)
    assert design_packet_fresh(active / "design-packet.json") is True

    for mutate in ("missing", "duplicate", "extra", "stale"):
        changed = json.loads(json.dumps(packet))
        if mutate == "missing":
            changed["context_refs"] = []
        elif mutate == "duplicate":
            changed["context_refs"] = [packet["context_refs"][0], packet["context_refs"][0]]
        elif mutate == "extra":
            changed["context_refs"] = [
                *packet["context_refs"],
                {"path": "docs/architecture.md", "hash": hash_file(git_repo / "docs" / "architecture.md")},
            ]
        else:
            changed["context_refs"][0]["hash"] = "sha256:" + ("9" * 64)
        atomic_write_json(active / "design-packet.json", changed)
        assert design_packet_fresh(active / "design-packet.json") is False
    atomic_write_json(active / "design-packet.json", packet)

    (active / "workflow-state.json").unlink()
    assert design_packet_fresh(active / "design-packet.json") is False
    assert (active / "workflow-state.json").exists() is False

    _seed_forward_feature(active)
    packet = build_design_packet(active)
    atomic_write_json(active / "design-packet.json", packet)
    (git_repo / "docs" / "architecture-proposal.md").write_text("# drifted\n", encoding="utf-8")
    assert design_packet_fresh(active / "design-packet.json") is False

    _seed_forward_feature(active)
    packet = build_design_packet(active)
    atomic_write_json(active / "design-packet.json", packet)
    design_path = active / "design.md"
    design_path.write_text(
        design_path.read_text(encoding="utf-8").replace(
            'Validation commands: ["pytest -q"]',
            'Validation commands: ["pytest -q tests/v2"]',
        ),
        encoding="utf-8",
    )
    assert design_packet_fresh(active / "design-packet.json") is False


def test_design_prompt_requires_validation_commands_contract(tmp_path):
    active = tmp_path / "active"
    active.mkdir(parents=True)
    (active / "prd.md").write_text("# prd\n", encoding="utf-8")
    body = render_stage_prompt(
        stage="design",
        feature="demo",
        feature_active=active,
        repo_root=tmp_path,
        primary_target=active / "design.md",
        extra_targets=[
            active / "scope.json",
            active / "trace.md",
            active / "test-plan.md",
        ],
    )
    assert "Validation commands: [" in body
    assert "[\"pytest -q\"]" in body
    assert "missing or malformed" in body.lower()


def test_design_review_consulted_docs_include_context_and_feedback_refs(git_repo, monkeypatch):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)
    # Seed a design-changelog.json — it should flow into consulted_docs
    # via the packet's response_to_feedback field.
    changelog_path = active / "design-changelog.json"
    atomic_write_json(changelog_path, {
        "kind": "design-changelog",
        "schema_version": 1,
        "entries": [
            {"round": 1, "trigger": "initial", "reason": "first pass",
             "artifacts_changed": ["design.md", "scope.json", "trace.md", "test-plan.md"],
             "added": [], "removed": []},
        ],
    })
    packet = build_design_packet(active)
    atomic_write_json(active / "design-packet.json", packet)

    monkeypatch.setenv(
        FAKE_INVOKER_ENV,
        str(Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"),
    )
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    run_panel_gate(
        gate="design-review",
        feature_active=active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=active / "design-packet.json",
        panel_config=_panel_config(),
    )

    docs = json.loads((active / "panel-design-review.docs.json").read_text(encoding="utf-8"))
    names = {Path(entry["path"]).name for entry in docs}
    assert {
        "design.md",
        "scope.json",
        "trace.md",
        "test-plan.md",
        "prd.md",
        "architecture-proposal.md",
        "design-changelog.json",
    }.issubset(names)


def test_design_review_precheck_blocks_stale_packet_without_vendor_dispatch(git_repo, monkeypatch, tmp_path):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)
    atomic_write_json(active / "design-packet.json", build_design_packet(active))

    design_text = (active / "design.md").read_text(encoding="utf-8")
    (active / "design.md").write_text(
        design_text.replace('Validation commands: ["pytest -q"]', 'Validation commands: ["pytest -q tests/v2"]'),
        encoding="utf-8",
    )

    touched = tmp_path / "vendor-invoked.txt"
    fake = _write_fake_panel_script(
        tmp_path,
        synth_payload={"per_reviewer": [{"vendor": "claude", "verdict": "pass", "findings": []}]},
        touch_path=touched,
    )
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(fake))

    verdict = run_panel_gate(
        gate="design-review",
        feature_active=active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=active / "design-packet.json",
        panel_config=_panel_config(),
    )
    assert touched.exists() is False
    assert verdict.effectively_blocks() is True
    assert "design-packet.json" in verdict.findings[0].summary


def test_canonical_design_review_pass_persists_decision_and_projection(git_repo, monkeypatch, tmp_path):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)
    atomic_write_json(active / "design-packet.json", build_design_packet(active))

    fake = _write_fake_panel_script(
        tmp_path,
        synth_payload={
            "per_reviewer": [{"vendor": "claude", "verdict": "pass", "findings": []}],
            "decision": {
                "node": "design_review",
                "outcome": "pass",
                "blocking": False,
                "severity": "opinion",
                "summary": "packet accepted",
            },
        },
    )
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(fake))
    run_panel_gate(
        gate="design-review",
        feature_active=active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=active / "design-packet.json",
        panel_config=_panel_config(),
    )

    raw = json.loads((active / "panel-design-review.json").read_text(encoding="utf-8"))
    assert raw["verdict"] == "pass"
    assert raw["decision"]["node"] == "design_review"
    assert raw["decision"]["outcome"] == "pass"


def test_first_prd_targeted_canonical_halt_normalizes_then_second_halts(git_repo, monkeypatch, tmp_path):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)
    atomic_write_json(active / "design-packet.json", build_design_packet(active))
    clear_state(active)

    synth_payload = {
        "per_reviewer": [
            {
                "vendor": "claude",
                "verdict": "fail",
                "findings": [
                    {
                        "severity": "risk",
                        "summary": "PRD contradiction",
                        "targets": ["anchor.prd.md"],
                    }
                ],
            }
        ],
        "decision": {
            "node": "design_review",
            "outcome": "halt_for_human",
            "blocking": True,
            "severity": "risk",
            "summary": "PRD contradiction",
        },
    }
    fake = _write_fake_panel_script(tmp_path, synth_payload=synth_payload)
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(fake))

    run_panel_gate(
        gate="design-review",
        feature_active=active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=active / "design-packet.json",
        panel_config=_panel_config(),
    )
    first = load_verdict(active / "panel-design-review.json")
    assert first.verdict == "needs_revision"
    assert first.decision.outcome == "retry_design"
    first_decision = handle_panel_verdict(active, "design-review", first)
    assert first_decision.kind == DecisionKind.LOCAL_REVISE

    run_panel_gate(
        gate="design-review",
        feature_active=active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=active / "design-packet.json",
        panel_config=_panel_config(),
    )
    second = load_verdict(active / "panel-design-review.json")
    assert second.verdict == "fail"
    assert second.decision.outcome == "halt_for_human"
    second_decision = handle_panel_verdict(active, "design-review", second)
    assert second_decision.kind == DecisionKind.HALT_FOR_HUMAN


def test_invalid_anchor_architecture_target_is_rejected_before_persistence(git_repo, monkeypatch, tmp_path):
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)
    atomic_write_json(active / "design-packet.json", build_design_packet(active))

    fake = _write_fake_panel_script(
        tmp_path,
        synth_payload={
            "per_reviewer": [
                {
                    "vendor": "claude",
                    "verdict": "fail",
                    "findings": [
                        {
                            "severity": "risk",
                            "summary": "architecture contradiction",
                            "targets": ["anchor.architecture-proposal.md"],
                        }
                    ],
                }
            ],
            "decision": {
                "node": "design_review",
                "outcome": "halt_for_human",
                "blocking": True,
                "severity": "risk",
                "summary": "architecture contradiction",
            },
        },
    )
    monkeypatch.setenv(FAKE_INVOKER_ENV, str(fake))
    with pytest.raises(SchemaError):
        run_panel_gate_internal(
            gate="design-review",
            feature_active=active,
            primary_artifact=active / "design-packet.json",
            prompt_file_for_audit=active / "design.md",
            consulted_docs=[],
            panel_config=_panel_config(),
            repo_root=git_repo,
        )
    assert (active / "panel-design-review.json").exists() is False


def test_write_accepted_design_rejects_blocking_verdict_and_preserves_existing_marker(tmp_path):
    active = tmp_path / "repo" / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)
    packet = build_design_packet(active)
    atomic_write_json(active / "design-packet.json", packet)

    passing = PanelVerdict(
        gate="design-review",
        verdict="pass",
        findings=[],
        source=str(active / "design-packet.json"),
        source_hash=hash_file(active / "design-packet.json"),
        prompt_file="p",
        prompt_hash="sha256:" + ("0" * 64),
        harness_version="test",
        run_ts="2026-04-26T00:00:00Z",
    )
    write_verdict(active / "panel-design-review.json", passing)
    passing_trace = PanelVerdict(
        gate="trace-review",
        verdict="pass",
        findings=[],
        source=str(active / "design-packet.json"),
        source_hash=hash_file(active / "design-packet.json"),
        prompt_file="p",
        prompt_hash="sha256:" + ("0" * 64),
        harness_version="test",
        run_ts="2026-04-26T00:00:00Z",
    )
    write_verdict(active / "panel-trace-review.json", passing_trace)
    write_accepted_design(active)
    original = (active / "accepted-design.json").read_text(encoding="utf-8")

    blocking = PanelVerdict(
        gate="design-review",
        verdict="fail",
        findings=[],
        source=str(active / "design-packet.json"),
        source_hash=hash_file(active / "design-packet.json"),
        prompt_file="p",
        prompt_hash="sha256:" + ("0" * 64),
        harness_version="test",
        run_ts="2026-04-26T00:01:00Z",
    )
    write_verdict(active / "panel-design-review.json", blocking)
    with pytest.raises(SchemaError):
        write_accepted_design(active)
    assert (active / "accepted-design.json").read_text(encoding="utf-8") == original
    assert accepted_design_fresh(active / "accepted-design.json") is False


def test_authoritative_context_drift_stales_cached_acceptance(
    git_repo,
    monkeypatch,
):
    """Context drift (e.g. arch-proposal change) invalidates cached panel
    verdicts and the accepted-design marker. The design-changelog
    reference itself persists — it is a historical record, not a
    cache of validated state — but the surrounding packet/verdict
    machinery must re-validate after upstream change.
    """
    active = git_repo / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    _seed_forward_feature(active)

    first_packet = build_design_packet(active)
    atomic_write_json(active / "design-packet.json", first_packet)

    # Seed a design-changelog.json to exercise the response_to_feedback path
    changelog_path = active / "design-changelog.json"
    atomic_write_json(changelog_path, {
        "kind": "design-changelog",
        "schema_version": 1,
        "entries": [
            {"round": 1, "trigger": "initial", "reason": "first pass",
             "artifacts_changed": ["design.md"], "added": [], "removed": []},
        ],
    })
    packet = build_design_packet(active)
    assert packet["response_to_feedback"] == [
        {"path": str(changelog_path), "hash": hash_file(changelog_path)},
    ]
    atomic_write_json(active / "design-packet.json", packet)

    monkeypatch.setenv(
        FAKE_INVOKER_ENV,
        str(Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"),
    )
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", "reviewers_all_pass")
    # design-review now dispatches BOTH reviewer groups internally and
    # writes panel-design-review.json + panel-trace-review.json atomically.
    run_panel_gate(
        gate="design-review",
        feature_active=active,
        repo_root=git_repo,
        feature="demo",
        primary_artifact=active / "design-packet.json",
        panel_config=_panel_config(),
    )
    assert (active / "panel-design-review.json").exists()
    assert (active / "panel-trace-review.json").exists()
    from autodev.artifacts.verdict import load_verdict as _lv
    assert _lv(active / "panel-design-review.json").gate == "design-review"
    assert _lv(active / "panel-trace-review.json").gate == "trace-review"
    write_accepted_design(active)
    assert accepted_design_fresh(active / "accepted-design.json") is True
    assert verdict_exists_and_valid(
        feature_active=active,
        gate="design-review",
        current_source_hash=hash_file(active / "design-packet.json"),
    ) is not None

    (git_repo / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n\ncontext drift\n",
        encoding="utf-8",
    )

    refreshed = build_design_packet(active)
    # Changelog reference persists across context drift — it is an
    # observational record, not state that becomes "stale".
    assert refreshed["response_to_feedback"] == [
        {"path": str(changelog_path), "hash": hash_file(changelog_path)},
    ]
    # Packet itself is stale because context_refs hashes change.
    assert design_packet_fresh(active / "design-packet.json") is False
    assert accepted_design_fresh(active / "accepted-design.json") is False
    assert verdict_exists_and_valid(
        feature_active=active,
        gate="design-review",
        current_source_hash=hash_file(active / "design-packet.json"),
    ) is None
