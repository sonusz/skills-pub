"""G20 revision loop (R4f/R4g/R7c).

Covers the walkthrough in prd.md amendment: local revises, escalation,
halt-for-human thresholds, update-semantics, close cleanup, and the
PANEL_FEEDBACK_PATHS channel into stage prompts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.artifacts.revision_state import (
    G_MAX, L_MAX, REVISION_GATES, RevisionState,
    clear_state, load_state, reset_on_amendment, state_path, write_state,
)
from autodev.revision_loop import (
    Decision, DecisionKind, consume_pending_feedback, handle_panel_verdict,
)


@pytest.fixture
def active(tmp_path):
    a = tmp_path / "active"
    a.mkdir()
    return a


# ---- state persistence -----------------------------------------------

def test_load_state_missing_file_returns_zero_counters(active):
    s = load_state(active)
    assert s.G == 0
    for g in REVISION_GATES:
        assert s.L[g] == 0
    assert s.pending_feedback == {}


def test_write_then_load_roundtrips(active):
    s = RevisionState(G=3, L={"architecture-review": 2, "test-plan-review": 1})
    write_state(active, s)
    loaded = load_state(active)
    assert loaded.G == 3
    assert loaded.L["architecture-review"] == 2
    assert loaded.L["test-plan-review"] == 1


def test_write_is_atomic(active):
    """Atomic writes don't leave orphan .tmp alongside the final file."""
    write_state(active, RevisionState(G=1))
    assert state_path(active).exists()
    assert not state_path(active).with_name(
        state_path(active).name + ".tmp"
    ).exists()


def test_state_invalid_L_rejected(active):
    import json as _json
    state_path(active).write_text(
        _json.dumps({"G": 0, "L": {"architecture-review": L_MAX + 1}}) + "\n"
    )
    with pytest.raises(Exception):
        load_state(active)


# ---- local revise ----------------------------------------------------

def test_local_revise_bumps_L_and_G(active):
    d = handle_panel_verdict(active, "test-plan-review")
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "plan"
    assert d.feedback_paths == ["panel-test-plan-review.json"]
    s = load_state(active)
    assert s.L["test-plan-review"] == 1
    assert s.L["architecture-review"] == 0
    assert s.G == 1
    assert s.pending_feedback == {"plan": ["panel-test-plan-review.json"]}


def test_local_revise_architecture_reruns_scope(active):
    d = handle_panel_verdict(active, "architecture-review")
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.stage_to_rerun == "scope"
    assert load_state(active).pending_feedback == {
        "scope": ["panel-architecture-review.json"]
    }


def test_local_revise_accumulates_up_to_L_MAX(active):
    for i in range(1, L_MAX + 1):
        d = handle_panel_verdict(active, "test-plan-review")
        assert d.kind == DecisionKind.LOCAL_REVISE, f"iter {i}"
        assert load_state(active).L["test-plan-review"] == i


# ---- escalation ------------------------------------------------------

def test_escalate_from_test_plan_to_architecture(active):
    """L[tp]==MAX and L[arch]<MAX and G<MAX → escalate."""
    s = RevisionState(
        G=5, L={"architecture-review": 2, "test-plan-review": L_MAX},
    )
    write_state(active, s)
    # The architecture-review verdict file exists → feedback list
    # includes BOTH tp and arch verdicts.
    (active / "panel-architecture-review.json").write_text("{}")
    d = handle_panel_verdict(active, "test-plan-review")
    assert d.kind == DecisionKind.ESCALATE
    assert d.stage_to_rerun == "scope"
    assert set(d.feedback_paths) == {
        "panel-test-plan-review.json", "panel-architecture-review.json",
    }
    reloaded = load_state(active)
    assert reloaded.L["architecture-review"] == 3
    assert reloaded.L["test-plan-review"] == 0  # reset
    assert reloaded.G == 6
    assert set(reloaded.pending_feedback["scope"]) == set(d.feedback_paths)


def test_escalate_without_upstream_verdict_file_still_escalates(active):
    """If upstream verdict doesn't yet exist, feedback is just the blocking one."""
    write_state(active, RevisionState(
        G=5, L={"architecture-review": 0, "test-plan-review": L_MAX},
    ))
    d = handle_panel_verdict(active, "test-plan-review")
    assert d.kind == DecisionKind.ESCALATE
    assert d.feedback_paths == ["panel-test-plan-review.json"]


def test_architecture_exhausted_has_no_escalation_halts(active):
    write_state(active, RevisionState(
        G=5, L={"architecture-review": L_MAX, "test-plan-review": 0},
    ))
    d = handle_panel_verdict(active, "architecture-review")
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_upstream_also_exhausted_halts(active):
    write_state(active, RevisionState(
        G=5, L={"architecture-review": L_MAX, "test-plan-review": L_MAX},
    ))
    d = handle_panel_verdict(active, "test-plan-review")
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert "upstream" in d.reason.lower() or "exhausted" in d.reason.lower()


# ---- G_MAX ceiling ---------------------------------------------------

def test_G_ceiling_halts_regardless_of_local(active):
    write_state(active, RevisionState(
        G=G_MAX, L={"architecture-review": 0, "test-plan-review": 0},
    ))
    d = handle_panel_verdict(active, "test-plan-review")
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert "global" in d.reason.lower() or str(G_MAX) in d.reason


# ---- G21 out-of-scope semantics (prd-review + close-approval -------
# participate in M but not in L/G substantive revision) ---------------

def test_unknown_gate_is_out_of_scope(active):
    """Truly unknown gates (e.g. 'security-review') return OUT_OF_SCOPE."""
    d = handle_panel_verdict(active, "security-review")
    assert d.kind == DecisionKind.OUT_OF_SCOPE


def test_prd_review_severe_halts_for_human(active):
    """prd-review severe/risk has no producer to rerun → halt."""
    from autodev.artifacts.verdict import PanelFinding, PanelVerdict
    v = PanelVerdict(
        gate="prd-review", verdict="fail",
        findings=[PanelFinding(severity="invariant_violation",
                               vendor="claude", summary="contradiction")],
        source="prd.md", source_hash="sha256:0",
        prompt_file="x", prompt_hash="sha256:0",
        harness_version="t", run_ts="t",
    )
    d = handle_panel_verdict(active, "prd-review", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert d.severity == "severe"


def test_close_approval_risk_halts_for_human(active):
    """close-approval severe/risk has no L/G path → halt (invariant
    from pre-G21)."""
    from autodev.artifacts.verdict import PanelFinding, PanelVerdict
    v = PanelVerdict(
        gate="close-approval", verdict="needs_revision",
        findings=[PanelFinding(severity="risk", vendor="claude", summary="x")],
        source="review.md", source_hash="sha256:0",
        prompt_file="x", prompt_hash="sha256:0",
        harness_version="t", run_ts="t",
    )
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


# ---- amendment semantics --------------------------------------------

def test_reset_on_amendment_resets_L_bumps_G(active):
    write_state(active, RevisionState(
        G=7, L={"architecture-review": 3, "test-plan-review": L_MAX},
        pending_feedback={"scope": ["panel-test-plan-review.json"]},
    ))
    s = reset_on_amendment(active)
    assert s.G == 8  # incremented
    for g in REVISION_GATES:
        assert s.L[g] == 0
    assert s.pending_feedback == {}
    # G persists; L reset to 0
    reloaded = load_state(active)
    assert reloaded.G == 8
    assert reloaded.L["test-plan-review"] == 0


def test_reset_on_amendment_when_state_absent_creates_G1(active):
    """Fresh feature, amendment still increments G from 0 to 1."""
    s = reset_on_amendment(active)
    assert s.G == 1
    assert load_state(active).G == 1


def test_clear_state_removes_file(active):
    write_state(active, RevisionState(G=5))
    assert state_path(active).exists()
    clear_state(active)
    assert not state_path(active).exists()


# ---- PANEL_FEEDBACK_PATHS consumption --------------------------------

def test_consume_pending_feedback_pops_and_persists(active):
    write_state(active, RevisionState(
        pending_feedback={"scope": ["panel-architecture-review.json"]},
    ))
    paths = consume_pending_feedback(active, "scope")
    assert len(paths) == 1
    assert paths[0].endswith("panel-architecture-review.json")
    assert str(active) in paths[0]
    # Pop persists
    assert "scope" not in load_state(active).pending_feedback


def test_consume_pending_feedback_missing_returns_empty(active):
    paths = consume_pending_feedback(active, "scope")
    assert paths == []


def test_consume_pending_feedback_for_unrelated_stage_is_noop(active):
    write_state(active, RevisionState(
        pending_feedback={"scope": ["panel-architecture-review.json"]},
    ))
    paths = consume_pending_feedback(active, "plan")
    assert paths == []
    # scope entry must be preserved
    assert load_state(active).pending_feedback["scope"] == [
        "panel-architecture-review.json"
    ]


# ---- stage prompt rendering injects PANEL_FEEDBACK_PATHS ------------

def test_scope_prompt_shows_feedback_paths(active, tmp_path):
    from autodev.prompts_loader import render_stage_prompt
    active.mkdir(exist_ok=True)
    (active / "prd.md").write_text("# prd\n")
    body = render_stage_prompt(
        stage="scope",
        feature="demo",
        feature_active=active,
        repo_root=tmp_path,
        primary_target=active / "scope.json",
        extra_targets=[],
        panel_feedback_paths=[str(active / "panel-architecture-review.json")],
    )
    assert "PANEL_FEEDBACK_PATHS:" in body
    assert "panel-architecture-review.json" in body
    assert "REVISION RUN" in body


def test_plan_prompt_marks_initial_run_empty(active, tmp_path):
    from autodev.prompts_loader import render_stage_prompt
    active.mkdir(exist_ok=True)
    (active / "prd.md").write_text("# prd\n")
    (active / "scope.json").write_text('{"x":1}')
    body = render_stage_prompt(
        stage="plan",
        feature="demo",
        feature_active=active,
        repo_root=tmp_path,
        primary_target=active / "trace.md",
        extra_targets=[active / "test-plan.md"],
    )
    assert "PANEL_FEEDBACK_PATHS: [] (initial run)" in body
    assert "REVISION RUN" not in body


# ---- walkthrough (abbreviated) ---------------------------------------

def test_walkthrough_full_ladder(active):
    """Compressed walkthrough (adapted to G21 L_MAX=3 + severity-aware):
    arch-review locally revises (L_MAX-1)×; then tp-review exhausts
    locally; escalates to arch which was at L_MAX-1 → arch=L_MAX,
    tp=0. Subsequent tp exhaust with arch at L_MAX halts.
    """
    # (L_MAX-1)× arch-review fail → that many local revises
    for _ in range(L_MAX - 1):
        d = handle_panel_verdict(active, "architecture-review")
        assert d.kind == DecisionKind.LOCAL_REVISE
    s = load_state(active)
    assert s.L["architecture-review"] == L_MAX - 1
    # L_MAX× tp-review fail → L_MAX local revises, tp now at MAX
    for _ in range(L_MAX):
        d = handle_panel_verdict(active, "test-plan-review")
        assert d.kind == DecisionKind.LOCAL_REVISE
    assert load_state(active).L["test-plan-review"] == L_MAX
    # tp-review fail one more time → escalate to arch
    d = handle_panel_verdict(active, "test-plan-review")
    assert d.kind == DecisionKind.ESCALATE
    s = load_state(active)
    assert s.L["architecture-review"] == L_MAX
    assert s.L["test-plan-review"] == 0
    # tp-review fail again — arch now at MAX → halt on escalation
    for _ in range(L_MAX):
        d = handle_panel_verdict(active, "test-plan-review")
        assert d.kind == DecisionKind.LOCAL_REVISE
    d = handle_panel_verdict(active, "test-plan-review")
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


# ---- orchestrator integration ----------------------------------------

def test_orchestrator_local_revise_invalidates_producer_and_continues(
    git_repo, feature_active, monkeypatch,
):
    """When test-plan-review returns a blocking verdict, the revision
    loop fires: trace.md + test-plan.md get deleted, the orchestrator
    returns success=True (calling run-loop will advance to replan).

    Drives `_advance_gate` directly — we're isolating the gate branch,
    not the full cascade walk.
    """
    from datetime import datetime, timezone
    from autodev.artifacts.verdict import PanelVerdict, write_verdict
    from autodev.state.hashing import hash_file
    from autodev.state.log import JsonlLog
    from autodev.orchestrator import Orchestrator, OrchestratorConfig
    from autodev.vendors.config import VendorsConfig, StageSpec, STAGES

    # Seed scope + trace + test-plan so the producer stage has something
    # to invalidate.
    prd = feature_active / "prd.md"
    prd.write_text("# prd\n")
    scope = feature_active / "scope.json"
    scope.write_text(json.dumps({
        "source": str(prd), "source_hash": hash_file(prd),
        "written": "2026-04-20", "feature": "demo",
        "mode": "fresh", "diff_base": "main",
        "in_scope": [{"id": "d-1", "description": "x",
                      "prd_ref": "§1", "status": "active"}],
        "excluded": [],
    }) + "\n")
    scope_h = hash_file(scope)
    for name in ("trace.md", "test-plan.md"):
        (feature_active / name).write_text(
            f"<!-- source: scope.json -->\n<!-- source_hash: {scope_h} -->\nbody\n"
        )

    # Pre-seed a FAILING test-plan-review verdict with a risk finding;
    # _advance_gate will read it from cache and invoke the revision
    # loop. Risk severity → LOCAL_REVISE (L/G bump; producer
    # invalidated). An empty-findings verdict would be classified as
    # minor under G21 and take the MINOR_REVISE branch instead.
    from autodev.artifacts.verdict import PanelFinding
    tp = feature_active / "test-plan.md"
    failing = PanelVerdict(
        gate="test-plan-review",
        verdict="needs_revision",
        findings=[PanelFinding(severity="risk", vendor="claude",
                                summary="missing edge case X")],
        source=str(tp), source_hash=hash_file(tp),
        prompt_file="x", prompt_hash="sha256:" + "0" * 64,
        harness_version="test", run_ts=datetime.now(timezone.utc).isoformat(),
    )
    write_verdict(feature_active / "panel-test-plan-review.json", failing)

    cfg = OrchestratorConfig(
        repo_root=git_repo,
        vendors=VendorsConfig(
            path=git_repo / "vendors.yml",
            stages={s: StageSpec(stage=s, vendor="claude", model="m",
                                  timeout_sec=30) for s in STAGES},
        ),
        session_id="test",
    )
    orch = Orchestrator(cfg)
    logger = JsonlLog(feature_active / "log.jsonl")
    result = orch._advance_gate("demo", feature_active, "test-plan-review", logger)

    assert result.success, result.detail
    # Plan stage artifacts invalidated
    assert not (feature_active / "trace.md").exists()
    assert not (feature_active / "test-plan.md").exists()
    # Counter bumped
    s = load_state(feature_active)
    assert s.L["test-plan-review"] == 1
    assert s.G == 1
    # Pending feedback queued for plan stage
    assert s.pending_feedback.get("plan") == ["panel-test-plan-review.json"]


# ---- CLI integration -------------------------------------------------

def test_cli_update_resets_L_bumps_G(git_repo, feature_active):
    """`autodev update` advances cycle AND resets L[*], bumps G by 1."""
    from autodev.cli import main
    # Seed PRD + revision-state with L saturated.
    (feature_active / "prd.md").write_text("# prd\n")
    write_state(feature_active, RevisionState(
        G=3, L={"architecture-review": 2, "test-plan-review": L_MAX},
    ))
    code = main([
        "update", "demo", "--amendment", "second cycle",
        "--repo-root", str(git_repo),
    ])
    assert code == 0
    s = load_state(feature_active)
    assert s.G == 4  # incremented
    for g in REVISION_GATES:
        assert s.L[g] == 0


def test_cli_close_clears_revision_state(git_repo, feature_active):
    from autodev.cli import main
    from autodev.artifacts.verdict import PanelVerdict, write_verdict
    from autodev.state.hashing import hash_file
    from datetime import datetime, timezone

    # Seed a passing close-approval so close proceeds.
    (feature_active / "prd.md").write_text("# prd\n")
    review = feature_active / "review.md"
    review.write_text("# review\n")
    v = PanelVerdict(
        gate="close-approval", verdict="pass", findings=[],
        source=str(review), source_hash=hash_file(review),
        prompt_file="x", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts=datetime.now(timezone.utc).isoformat(),
    )
    write_verdict(feature_active / "panel-close-approval.json", v)
    write_state(feature_active, RevisionState(G=2))
    assert state_path(feature_active).exists()

    code = main([
        "close", "demo", "complete", "--yes", "--repo-root", str(git_repo),
    ])
    assert code == 0
    # After close, active/ has been moved; revision-state should NOT
    # follow into complete/.
    dest = git_repo / "docs" / "features" / "demo" / "complete"
    assert dest.exists()
    assert not (dest / "revision-state.json").exists()


def test_cli_status_surfaces_revision_counters(git_repo, feature_active, capsys):
    from autodev.cli import main
    (feature_active / "prd.md").write_text("# prd\n")
    write_state(feature_active, RevisionState(
        G=3, L={"architecture-review": 2, "test-plan-review": 0},
        pending_feedback={"scope": ["panel-architecture-review.json"]},
    ))
    code = main(["status", "demo", "--repo-root", str(git_repo)])
    assert code == 0
    out = capsys.readouterr().out
    assert "G=3" in out or "G:" in out or "revisions:" in out
    # The human-readable line advertises G and per-gate state
    assert "revisions:" in out
    assert "architecture-review=2" in out


def test_orchestrator_halt_for_human_raises_gate_pending(
    git_repo, feature_active,
):
    """When both local and upstream counters are exhausted, the gate
    raises GatePending with revision-loop guidance."""
    from datetime import datetime, timezone
    from autodev.artifacts.verdict import PanelVerdict, write_verdict
    from autodev.state.hashing import hash_file
    from autodev.state.log import JsonlLog
    from autodev.orchestrator import Orchestrator, OrchestratorConfig
    from autodev.errors import GatePending
    from autodev.vendors.config import VendorsConfig, StageSpec, STAGES

    # Saturate counters.
    write_state(feature_active, RevisionState(
        G=5, L={"architecture-review": L_MAX, "test-plan-review": L_MAX},
    ))
    from autodev.artifacts.verdict import PanelFinding
    tp = feature_active / "test-plan.md"
    tp.write_text("body\n")
    failing = PanelVerdict(
        gate="test-plan-review", verdict="fail",
        findings=[PanelFinding(severity="invariant_violation",
                                vendor="claude",
                                summary="contradicts R1")],
        source=str(tp), source_hash=hash_file(tp),
        prompt_file="x", prompt_hash="sha256:" + "0" * 64,
        harness_version="test", run_ts=datetime.now(timezone.utc).isoformat(),
    )
    write_verdict(feature_active / "panel-test-plan-review.json", failing)

    cfg = OrchestratorConfig(
        repo_root=git_repo,
        vendors=VendorsConfig(
            path=git_repo / "vendors.yml",
            stages={s: StageSpec(stage=s, vendor="claude", model="m",
                                  timeout_sec=30) for s in STAGES},
        ),
        session_id="test",
    )
    orch = Orchestrator(cfg)
    logger = JsonlLog(feature_active / "log.jsonl")
    with pytest.raises(GatePending):
        orch._advance_gate("demo", feature_active, "test-plan-review", logger)
