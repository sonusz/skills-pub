"""Regression tests for _merge_trace_into_design.

Bug: design-review group's synthesizer is the only one that emits a
`decision` object. If design.decision.outcome="pass" but trace-review's
group surfaces blocking findings, naively copying design.decision into
the merged verdict short-circuits handle_panel_verdict via the
``outcome == "pass"`` → OUT_OF_SCOPE branch — trace's blocking findings
never drive a design rerun.

Fix: when design.decision.outcome=="pass" AND trace has blocking
findings, drop the decision in the merged verdict so handle_panel_verdict
falls back to filename-based dispatch on the merged findings list.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from autodev.artifacts.verdict import (
    PanelFinding, PanelVerdict, ReviewDecision, write_verdict,
)
from autodev.orchestrator import Orchestrator, OrchestratorConfig
from autodev.vendors.config import (
    PanelConfig, PanelReviewerSpec, PanelSynthesizerSpec, ProbeConfig,
    StageSpec, VendorsConfig,
)


def _make_orch(tmp_path: Path) -> Orchestrator:
    return Orchestrator(OrchestratorConfig(
        repo_root=tmp_path,
        vendors=VendorsConfig(
            path=Path("/dev/null/vendors.yml"),
            stages={
                "design": StageSpec(stage="design", vendor="claude", model="x", probe_interval_sec=10),
                "build":  StageSpec(stage="build",  vendor="claude", model="x", probe_interval_sec=10),
                "spec":   StageSpec(stage="spec",   vendor="claude", model="x", probe_interval_sec=10),
                "review": StageSpec(stage="review", vendor="claude", model="x", probe_interval_sec=10),
            },
            panel=PanelConfig(
                reviewers=(PanelReviewerSpec(vendor="claude", model="x"),),
                synthesizer=PanelSynthesizerSpec(vendor="claude", model="x"),
                reviewer_probe_interval_sec=10,
                synthesizer_probe_interval_sec=10,
            ),
            probe=ProbeConfig(vendor="claude", model="x", timeout_sec=10),
        ),
        session_id="test",
    ))


def _seed_verdict(
    active: Path, name: str, *,
    verdict: str, gate: str, findings: list[PanelFinding],
    decision: ReviewDecision | None = None,
    source_hash: str = "sha256:" + "0" * 64,
) -> None:
    write_verdict(active / name, PanelVerdict(
        gate=gate, verdict=verdict, findings=findings,
        source="design-packet.json",
        source_hash=source_hash,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-05-20T00:00:00Z",
        decision=decision,
    ))


def test_merge_drops_decision_when_design_passes_but_trace_blocks(tmp_path):
    """design.decision=pass + trace blocking → merged.decision is None
    so dispatch falls through to filename-based routing."""
    orch = _make_orch(tmp_path)
    active = tmp_path / "active"
    active.mkdir()

    design_decision = ReviewDecision(
        node="design_review", outcome="pass", blocking=False,
        severity="opinion", summary="design-review group OK",
    )
    _seed_verdict(active, "panel-design-review.json",
                  verdict="pass", gate="design-review",
                  findings=[], decision=design_decision)
    trace_finding = PanelFinding(
        severity="invariant_violation", vendor="claude",
        summary="R4 timeout invariant missing from trace.md",
        targets=["primary_pair.trace.md"],
    )
    _seed_verdict(active, "panel-trace-review.json",
                  verdict="needs_revision", gate="trace-review",
                  findings=[trace_finding])

    from autodev.artifacts.verdict import load_verdict
    v_design = load_verdict(active / "panel-design-review.json")
    merged, feedback_paths = orch._merge_trace_into_design(active, v_design)

    assert merged is not None
    # Findings merged, verdict promoted to needs_revision
    assert len(merged.findings) == 1
    assert merged.findings[0].summary.startswith("R4 timeout invariant")
    assert merged.verdict == "needs_revision"
    # The bug: decision was copied from design (outcome="pass"). The fix:
    # decision is dropped so dispatch routes on the merged findings instead.
    assert merged.decision is None
    assert feedback_paths == ["panel-design-review.json", "panel-trace-review.json"]


def test_merge_keeps_decision_when_both_pass(tmp_path):
    """If neither group has blocking findings, merged.decision keeps
    design's decision (the clean pass-through case)."""
    orch = _make_orch(tmp_path)
    active = tmp_path / "active"
    active.mkdir()

    design_decision = ReviewDecision(
        node="design_review", outcome="pass", blocking=False,
        severity="opinion", summary="all good",
    )
    _seed_verdict(active, "panel-design-review.json",
                  verdict="pass", gate="design-review",
                  findings=[], decision=design_decision)
    _seed_verdict(active, "panel-trace-review.json",
                  verdict="pass", gate="trace-review",
                  findings=[PanelFinding(severity="opinion", vendor="claude",
                                         summary="minor style note")])

    from autodev.artifacts.verdict import load_verdict
    v_design = load_verdict(active / "panel-design-review.json")
    merged, _ = orch._merge_trace_into_design(active, v_design)

    assert merged is not None
    assert merged.decision is not None
    assert merged.decision.outcome == "pass"
    assert merged.verdict == "pass"


def test_merge_keeps_decision_when_design_says_retry(tmp_path):
    """If design.decision.outcome=retry_design, it stays — trace's findings
    just add to the list. No need to drop it; retry already triggers rerun."""
    orch = _make_orch(tmp_path)
    active = tmp_path / "active"
    active.mkdir()

    design_decision = ReviewDecision(
        node="design_review", outcome="retry_design", blocking=False,
        severity="risk", summary="design group wants rerun",
    )
    _seed_verdict(active, "panel-design-review.json",
                  verdict="needs_revision", gate="design-review",
                  findings=[PanelFinding(severity="risk", vendor="claude",
                                         summary="design issue",
                                         targets=["primary_pair.design.md"])],
                  decision=design_decision)
    _seed_verdict(active, "panel-trace-review.json",
                  verdict="needs_revision", gate="trace-review",
                  findings=[PanelFinding(severity="invariant_violation", vendor="claude",
                                         summary="trace issue",
                                         targets=["primary_pair.trace.md"])])

    from autodev.artifacts.verdict import load_verdict
    v_design = load_verdict(active / "panel-design-review.json")
    merged, _ = orch._merge_trace_into_design(active, v_design)

    assert merged is not None
    assert merged.decision is not None
    assert merged.decision.outcome == "retry_design"
    assert len(merged.findings) == 2
