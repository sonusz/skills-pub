"""Unified design-review revision loop.

Covers:
- L_MAX uniform across all gates
- Target-driven producer dispatch
- One design rerun before halt on repeated prd.md target
- Halt on arch-doc target / mixed producers
- Indeterminate-targets fallback for design-review
- L_MAX halt on (L_MAX+1)th blocking verdict
- route_to_layer shares L budget with panel dispatch
- reset_on_amendment clears L
- Legacy-field tolerance on load
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.artifacts.revision_state import (
    ALL_PANEL_GATES, L_MAX, PRD_TARGET_HALT_STREAK, RevisionState, load_state,
    reset_on_amendment, write_state,
)
from autodev.artifacts.verdict import PanelFinding, PanelVerdict, ReviewDecision
from autodev.revision_loop import (
    Decision, DecisionKind, handle_panel_verdict, route_to_layer,
)


@pytest.fixture
def active(tmp_path):
    a = tmp_path / "active"
    a.mkdir()
    return a


def _v(gate, findings) -> PanelVerdict:
    return PanelVerdict(
        gate=gate, verdict="needs_revision", findings=findings,
        source="x", source_hash="sha256:" + "0" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-04-20T00:00:00Z",
    )


def _f(severity="invariant_violation", targets=()):
    return PanelFinding(
        severity=severity, vendor="claude", summary="x",
        targets=list(targets),
    )


# ---------- L_MAX is uniform ----------

def test_l_max_is_set():
    assert L_MAX == 10


def test_all_panel_gates_cover_tracked_gates():
    """Unified design-review gate (with internal dual-group dispatch) and close-approval."""
    assert set(ALL_PANEL_GATES) == {
        "design-review", "close-approval",
    }


# ---------- Target → producer dispatch ----------

@pytest.mark.parametrize("filename,producer", [
    ("design.md", "design"),
    ("scope.json", "design"),
    ("trace.md", "design"),
    ("test-plan.md", "design"),
])
def test_target_maps_to_producer(active, filename, producer):
    gate = "design-review"
    v = _v(gate, [_f(targets=[f"primary_pair.{filename}"])])
    d = handle_panel_verdict(active, gate, v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == producer


def test_prd_target_routes_design_once(active):
    v = _v("design-review", [_f(targets=["primary_pair.prd.md"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "design"
    s = load_state(active)
    assert s.L["design-review"] == 1
    assert s.prd_target_streak["design-review"] == 1
    assert "apparent PRD conflict" in d.reason


def test_anchor_prd_target_routes_design_once(active):
    v = _v("design-review", [_f(targets=["anchor.prd.md"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "design"
    assert load_state(active).prd_target_streak["design-review"] == 1


def test_canonical_retry_design_uses_persisted_prd_targeted_bit(active):
    v = PanelVerdict(
        gate="design-review",
        verdict="needs_revision",
        findings=[_f(targets=["primary_pair.trace.md"])],
        source="x",
        source_hash="sha256:" + "0" * 64,
        prompt_file="p",
        prompt_hash="sha256:" + "0" * 64,
        harness_version="t",
        run_ts="2026-04-20T00:00:00Z",
        decision=ReviewDecision(
            node="design_review",
            outcome="retry_design",
            blocking=False,
            severity="risk",
            summary="canonical retry",
            prd_targeted=True,
        ),
    )
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert load_state(active).prd_target_streak["design-review"] == 1


def test_canonical_retry_design_ignores_prd_targets_in_findings_when_decision_says_false(active):
    v = PanelVerdict(
        gate="design-review",
        verdict="needs_revision",
        findings=[_f(targets=["anchor.prd.md"])],
        source="x",
        source_hash="sha256:" + "0" * 64,
        prompt_file="p",
        prompt_hash="sha256:" + "0" * 64,
        harness_version="t",
        run_ts="2026-04-20T00:00:00Z",
        decision=ReviewDecision(
            node="design_review",
            outcome="retry_design",
            blocking=False,
            severity="risk",
            summary="canonical retry",
            prd_targeted=False,
        ),
    )
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    state = load_state(active)
    assert state.L["design-review"] == 1
    assert state.prd_target_streak["design-review"] == 0


def test_prd_target_halts_on_second_consecutive_round(active):
    v = _v("design-review", [_f(targets=["primary_pair.prd.md"])])
    first = handle_panel_verdict(active, "design-review", v)
    assert first.kind == DecisionKind.LOCAL_REVISE
    second = handle_panel_verdict(active, "design-review", v)
    assert second.kind == DecisionKind.HALT_FOR_HUMAN
    assert second.would_rerun == "design"
    s = load_state(active)
    assert s.L["design-review"] == 1
    assert s.prd_target_streak["design-review"] == PRD_TARGET_HALT_STREAK


def test_prd_target_streak_resets_on_non_prd_blocking_round(active):
    prd_v = _v("design-review", [_f(targets=["primary_pair.prd.md"])])
    trace_v = _v("design-review", [_f(targets=["primary_pair.trace.md"])])
    handle_panel_verdict(active, "design-review", prd_v)
    d = handle_panel_verdict(active, "design-review", trace_v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    s = load_state(active)
    assert s.L["design-review"] == 2
    assert s.prd_target_streak["design-review"] == 0


def test_close_prd_target_still_halts(active):
    v = _v("close-approval", [_f(targets=["primary_pair.prd.md"])])
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert "prd" in d.reason.lower()


def test_spec_target_halts_in_design_review(active):
    v = _v("design-review", [_f(targets=["primary_pair.implemented-spec.md"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_checklist_target_halts_in_design_review(active):
    v = _v("design-review", [_f(targets=["primary_pair.prd-checklist.json"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_arch_doc_target_halts(active):
    """At the design-review gate, arbitrary arch-doc filenames
    (not in the gate's pipeline-artifact set) halt for human."""
    v = _v("design-review",
           [_f(targets=["primary_pair.docs/architecture.md"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_close_spec_target_halts(active):
    """close-approval reviewers must NOT target implemented-spec.md
    (spec is a passive describer; rerunning spec cannot fix code or
    design gaps). The close-approval filename map deliberately omits
    it, so the dispatch sees a non-rerunnable target and halts."""
    v = _v("close-approval", [
        _f(targets=["primary_pair.implemented-spec.md"]),
    ])
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_close_build_target_reruns_build(active):
    """close-approval can route findings about shipped behavior to
    the build stage (the producer that owns code)."""
    v = _v("close-approval", [
        _f(targets=["primary_pair.build.json"]),
    ])
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "build"


def test_close_findings_span_design_and_build_picks_design(active):
    """When close-approval findings target both design and build
    artifacts, the orchestrator must pick the upstream-most producer
    (design) and let the cascade rerun build downstream — not halt
    for human. Halt is only correct for non-rerunnable targets (PRD,
    arch-doc) which are handled separately."""
    v = _v("close-approval", [
        _f(targets=["primary_pair.build.json"]),
        _f(targets=["primary_pair.trace.md"]),  # design-owned
    ])
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "design"
    assert "upstream-most" in d.reason


def test_close_design_target_reruns_design(active):
    """close-approval can route to design when the upstream design
    artifact is the source of the gap (e.g. trace.md weakened a
    PRD modal verb)."""
    v = _v("close-approval", [
        _f(targets=["primary_pair.trace.md"]),
    ])
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "design"


def test_design_review_fallback_rerun_design(active):
    v = _v("design-review", [_f(targets=[])])  # indeterminate
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "design"


def test_close_indeterminate_halts(active):
    """close-approval has no fallback producer: indeterminate
    blocking findings halt for human (previously fell back to spec,
    which is the wrong layer for shipped-behavior gaps)."""
    v = _v("close-approval", [_f(targets=[])])
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_bare_primary_pair_literal_halts_close_approval(active):
    """Bare 'primary_pair' literal yields no specific filename,
    so the indeterminate path halts (no fallback)."""
    v = _v("close-approval", [_f(targets=["primary_pair"])])
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_opinion_targets_ignored_for_dispatch(active):
    """opinion findings' targets are ignored for rerun dispatch."""
    v = _v("design-review", [
        _f(severity="invariant_violation", targets=["primary_pair.trace.md"]),
        _f(severity="opinion", targets=["primary_pair.prd.md"]),  # would-halt if considered
    ])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "design"


def test_risk_severity_triggers_dispatch(active):
    v = _v("design-review", [_f(severity="risk", targets=["primary_pair.trace.md"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE


# ---------- L_MAX halt ----------

def test_l_max_halt_on_fourth_blocking(active):
    """Initial + 3 reruns = 4 panel runs; 4th blocking halts."""
    s = RevisionState()
    s.L["design-review"] = L_MAX  # already at cap
    write_state(active, s)
    v = _v("design-review", [_f(targets=["primary_pair.trace.md"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert "L_MAX" in d.reason or f"L_MAX={L_MAX}" in d.reason


def test_third_blocking_verdict_dispatches(active):
    s = RevisionState()
    s.L["design-review"] = L_MAX - 1
    write_state(active, s)
    v = _v("design-review", [_f(targets=["primary_pair.trace.md"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    # L bumped to L_MAX
    assert load_state(active).L["design-review"] == L_MAX


def test_first_blocking_verdict_bumps_l_to_1(active):
    v = _v("design-review", [_f(targets=["primary_pair.trace.md"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert load_state(active).L["design-review"] == 1


def test_halt_does_not_bump_l(active):
    v = _v("design-review", [_f(targets=["primary_pair.docs/architecture.md"])])
    handle_panel_verdict(active, "design-review", v)
    assert load_state(active).L["design-review"] == 0


def test_mixed_producer_halt_does_not_bump_l(active):
    v = _v("close-approval", [
        _f(targets=["primary_pair.scope.json"]),
        _f(targets=["primary_pair.implemented-spec.md"]),
    ])
    handle_panel_verdict(active, "close-approval", v)
    assert load_state(active).L["close-approval"] == 0


# ---------- route_to_layer shares L budget ----------

def test_route_to_layer_bumps_l(active):
    d = route_to_layer(active, "design", trigger_ref="build.json#/0")
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "design"
    assert load_state(active).L["design-review"] == 1


def test_route_to_layer_halts_at_l_max(active):
    s = RevisionState()
    s.L["design-review"] = L_MAX
    write_state(active, s)
    d = route_to_layer(active, "design", trigger_ref="b")
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_route_to_layer_prd_halts_unconditionally(active):
    d = route_to_layer(active, "prd", trigger_ref="b")
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_route_to_layer_ambiguous_halts(active):
    d = route_to_layer(active, "ambiguous", trigger_ref="b")
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


# ---------- reset_on_amendment ----------

def test_reset_on_amendment_clears_l(active):
    s = RevisionState()
    s.L["design-review"] = 2
    s.L["close-approval"] = 1
    s.prd_target_streak["design-review"] = 1
    s.pending_feedback = {"design": ["x"]}
    write_state(active, s)
    s2 = reset_on_amendment(active)
    assert all(v == 0 for v in s2.L.values())
    assert all(v == 0 for v in s2.prd_target_streak.values())
    assert s2.pending_feedback == {}


# ---------- legacy-field tolerance ----------

def test_load_tolerates_legacy_m_and_auto_pass_next(active):
    """Pre-v3-core state files have M, auto_pass_next, g_dev_bumped."""
    legacy = {
        "G": 2,
        "L": {"design-review": 1},
        "M": {"design-review": 2},
        "auto_pass_next": {"close-approval": True},
        "g_dev_bumped": True,
        "pending_feedback": {"design": ["x"]},
    }
    (active / "revision-state.json").write_text(json.dumps(legacy))
    s = load_state(active)
    assert s.L["design-review"] == 1
    assert s.prd_target_streak["design-review"] == 0
    assert s.pending_feedback == {"design": ["x"]}


def test_write_state_drops_legacy_fields(active):
    s = RevisionState(L={"design-review": 1})
    s.L = {"design-review": 1, "close-approval": 0}
    write_state(active, s)
    raw = json.loads((active / "revision-state.json").read_text())
    assert "M" not in raw
    assert "auto_pass_next" not in raw
    assert "g_dev_bumped" not in raw


def test_out_of_scope_gate_returns_out_of_scope(active):
    v = _v("design-review", [])
    d = handle_panel_verdict(active, "bogus-gate", v)
    assert d.kind == DecisionKind.OUT_OF_SCOPE


def test_would_rerun_field_populated_on_halt(active):
    """Informational field: even on halt, declare which producer would have run."""
    s = RevisionState()
    s.L["design-review"] = L_MAX
    write_state(active, s)
    v = _v("design-review", [_f(targets=["primary_pair.trace.md"])])
    d = handle_panel_verdict(active, "design-review", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert d.would_rerun == "design"


def test_rerun_does_not_set_pending_feedback(active):
    """v3-core: pending_feedback is no longer populated on LOCAL_REVISE.
    CONTEXT_ARTIFACTS in _advance_coding picks up the relevant panel verdict
    from disk directly — no explicit queue needed."""
    v = _v("design-review", [_f(targets=["primary_pair.trace.md"])])
    handle_panel_verdict(active, "design-review", v)
    assert load_state(active).pending_feedback == {}
