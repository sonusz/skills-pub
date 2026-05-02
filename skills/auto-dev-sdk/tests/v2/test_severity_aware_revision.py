"""G21 severity-aware revision loop tests.

Covers:
  - PanelVerdict.classify_severity (severe/risk/minor)
  - minor-only iterations don't consume L/G; bump M
  - severe/risk reset M[g] to 0
  - auto_pass_next armed at M_MAX; orchestrator consumes it to
    synthesize a pass verdict with audit fields
  - prd-review M_MAX_PRD=1 (first minor → auto-pass; PRD not invalidated)
  - close-approval minor path (review.md invalidated, not scope)
  - amendment resets M and auto_pass_next
  - CLI status surfaces M + auto_pass armed
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autodev.artifacts.revision_state import (
    ALL_PANEL_GATES, G_MAX, L_MAX, M_MAX, M_MAX_PRD, REVISION_GATES,
    RevisionState, clear_state, load_state, m_max_for, reset_on_amendment,
    state_path, write_state,
)
from autodev.artifacts.verdict import (
    PanelFinding, PanelVerdict, load_verdict, write_verdict,
)
from autodev.revision_loop import (
    Decision, DecisionKind, consume_auto_pass_arm, handle_panel_verdict,
)


@pytest.fixture
def active(tmp_path):
    a = tmp_path / "active"
    a.mkdir()
    return a


def _v(gate: str, severity: str | None, *, verdict_str: str = "needs_revision") -> PanelVerdict:
    """Synth a PanelVerdict of the requested severity.

    severity=None → empty findings (classifies as minor).
    """
    findings = []
    if severity == "invariant_violation":
        findings = [PanelFinding(severity="invariant_violation",
                                  vendor="claude", summary="contradicts X")]
    elif severity == "risk":
        findings = [PanelFinding(severity="risk", vendor="claude",
                                  summary="possible issue Y")]
    elif severity == "opinion":
        findings = [PanelFinding(severity="opinion", vendor="claude",
                                  summary="style preference Z")]
    return PanelVerdict(
        gate=gate, verdict=verdict_str, findings=findings,
        source="x", source_hash="sha256:" + "0" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts=datetime.now(timezone.utc).isoformat(),
    )


# ---- classify_severity -----------------------------------------------

def test_classify_severe_from_invariant():
    v = _v("architecture-review", "invariant_violation")
    assert v.classify_severity() == "severe"


def test_classify_risk_from_risk_finding():
    v = _v("architecture-review", "risk")
    assert v.classify_severity() == "risk"


def test_classify_minor_from_opinion_only():
    v = _v("architecture-review", "opinion")
    assert v.classify_severity() == "minor"


def test_classify_minor_from_empty_findings():
    v = _v("architecture-review", None)
    assert v.classify_severity() == "minor"


def test_classify_severe_outweighs_risk_and_opinion():
    """Any IV → severe, regardless of other findings."""
    v = PanelVerdict(
        gate="architecture-review", verdict="needs_revision",
        findings=[
            PanelFinding(severity="opinion", vendor="claude", summary="a"),
            PanelFinding(severity="risk", vendor="gemini", summary="b"),
            PanelFinding(severity="invariant_violation", vendor="codex",
                          summary="c"),
        ],
        source="x", source_hash="sha256:0",
        prompt_file="p", prompt_hash="sha256:0",
        harness_version="t", run_ts="t",
    )
    assert v.classify_severity() == "severe"


def test_classify_reads_finding_severity_not_verdict_string():
    """verdict='fail' with only opinion findings is still minor — the
    panel's tone is not the contract."""
    v = _v("architecture-review", "opinion", verdict_str="fail")
    assert v.classify_severity() == "minor"


# ---- severe / risk paths (L/G behavior + M reset) -------------------

def test_severe_bumps_L_G_and_resets_M(active):
    # Prime M to 2 to verify reset.
    write_state(active, RevisionState(
        G=0, M={"architecture-review": 2, "test-plan-review": 0,
                "prd-review": 0, "close-approval": 0},
    ))
    v = _v("architecture-review", "invariant_violation")
    d = handle_panel_verdict(active, "architecture-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.severity == "severe"
    s = load_state(active)
    assert s.L["architecture-review"] == 1
    assert s.G == 1
    assert s.M["architecture-review"] == 0  # reset


def test_risk_bumps_L_G_and_resets_M(active):
    write_state(active, RevisionState(
        G=0, M={"architecture-review": 1, "test-plan-review": 0,
                "prd-review": 0, "close-approval": 0},
    ))
    v = _v("architecture-review", "risk")
    d = handle_panel_verdict(active, "architecture-review", v)
    assert d.kind == DecisionKind.LOCAL_REVISE
    assert d.severity == "risk"
    s = load_state(active)
    assert s.L["architecture-review"] == 1
    assert s.G == 1
    assert s.M["architecture-review"] == 0


# ---- minor path (M only, L/G untouched) ------------------------------

def test_minor_arch_review_bumps_M_not_L_G(active):
    v = _v("architecture-review", "opinion")
    d = handle_panel_verdict(active, "architecture-review", v)
    assert d.kind == DecisionKind.MINOR_REVISE
    assert d.severity == "minor"
    assert d.stage_to_rerun == "scope"
    assert d.feedback_paths == ["panel-architecture-review.json"]
    s = load_state(active)
    assert s.G == 0
    assert s.L["architecture-review"] == 0
    assert s.M["architecture-review"] == 1
    assert s.auto_pass_next["architecture-review"] is False


def test_minor_streak_arms_auto_pass_at_M_MAX(active):
    # M_MAX-1 iterations of minor
    for i in range(M_MAX - 1):
        v = _v("architecture-review", "opinion")
        d = handle_panel_verdict(active, "architecture-review", v)
        assert d.kind == DecisionKind.MINOR_REVISE
    # The M_MAX-th minor arms auto-pass
    v = _v("architecture-review", "opinion")
    d = handle_panel_verdict(active, "architecture-review", v)
    assert d.kind == DecisionKind.AUTO_PASS_ARMED
    s = load_state(active)
    assert s.auto_pass_next["architecture-review"] is True
    assert s.M["architecture-review"] == M_MAX
    assert s.G == 0  # never counted


def test_minor_streak_resets_after_risk(active):
    # 2 minor
    for _ in range(2):
        handle_panel_verdict(active, "architecture-review",
                              _v("architecture-review", "opinion"))
    assert load_state(active).M["architecture-review"] == 2
    # risk → resets M, bumps L/G
    handle_panel_verdict(active, "architecture-review",
                          _v("architecture-review", "risk"))
    s = load_state(active)
    assert s.M["architecture-review"] == 0
    assert s.L["architecture-review"] == 1
    assert s.G == 1
    # fresh minor streak does not inherit the old count
    for _ in range(M_MAX - 1):
        handle_panel_verdict(active, "architecture-review",
                              _v("architecture-review", "opinion"))
    assert load_state(active).M["architecture-review"] == M_MAX - 1


# ---- prd-review special case (M_MAX_PRD = 1) ------------------------

def test_prd_review_minor_immediately_arms_auto_pass(active):
    v = _v("prd-review", "opinion")
    d = handle_panel_verdict(active, "prd-review", v)
    assert d.kind == DecisionKind.AUTO_PASS_ARMED
    assert d.stage_to_rerun is None  # PRD is NOT invalidated
    s = load_state(active)
    assert s.auto_pass_next["prd-review"] is True
    assert s.M["prd-review"] == 1


def test_prd_review_minor_does_not_schedule_scope_rerun(active):
    """Critical: PRD is human-authored — agent can't fix an opinion
    on the PRD, so scope is NOT queued for rerun."""
    handle_panel_verdict(active, "prd-review", _v("prd-review", "opinion"))
    s = load_state(active)
    assert "scope" not in s.pending_feedback


def test_m_max_for_prd_is_one():
    assert m_max_for("prd-review") == M_MAX_PRD == 1


def test_m_max_for_others_is_M_MAX():
    for g in ("architecture-review", "test-plan-review", "close-approval"):
        assert m_max_for(g) == M_MAX


# ---- close-approval minor (review.md is the producer) ---------------

def test_close_approval_minor_reruns_review_stage(active):
    """close-approval's minor path re-runs the `review` stage (which
    produces review.md, the close-approval primary artifact)."""
    v = _v("close-approval", "opinion")
    d = handle_panel_verdict(active, "close-approval", v)
    assert d.kind == DecisionKind.MINOR_REVISE
    assert d.stage_to_rerun == "review"
    s = load_state(active)
    assert s.pending_feedback["review"] == ["panel-close-approval.json"]


def test_close_approval_minor_streak_arms_auto_pass(active):
    for _ in range(M_MAX - 1):
        handle_panel_verdict(active, "close-approval",
                              _v("close-approval", "opinion"))
    d = handle_panel_verdict(active, "close-approval",
                              _v("close-approval", "opinion"))
    assert d.kind == DecisionKind.AUTO_PASS_ARMED
    assert load_state(active).auto_pass_next["close-approval"] is True


# ---- consume_auto_pass_arm -------------------------------------------

def test_consume_auto_pass_arm_clears_flag(active):
    write_state(active, RevisionState(
        auto_pass_next={g: False for g in ALL_PANEL_GATES},
    ))
    s = load_state(active)
    s.auto_pass_next["architecture-review"] = True
    write_state(active, s)
    assert consume_auto_pass_arm(active, "architecture-review") is True
    assert load_state(active).auto_pass_next["architecture-review"] is False


def test_consume_auto_pass_arm_not_set_returns_false(active):
    assert consume_auto_pass_arm(active, "architecture-review") is False


# ---- orchestrator integration ---------------------------------------

def test_orchestrator_auto_pass_writes_synthetic_verdict(git_repo, feature_active):
    """When auto_pass_next is armed, _advance_gate writes a pass
    verdict with audit fields and does NOT invoke the panel."""
    from autodev.orchestrator import Orchestrator, OrchestratorConfig
    from autodev.state.hashing import hash_file
    from autodev.state.log import JsonlLog
    from autodev.vendors.config import VendorsConfig, StageSpec, STAGES

    scope = feature_active / "scope.json"
    scope.write_text(json.dumps({
        "source": "prd.md", "source_hash": "sha256:0",
        "written": "2026-04-20", "feature": "demo",
        "mode": "fresh", "diff_base": "main",
        "in_scope": [], "excluded": [],
    }) + "\n")
    # Previous panel verdict (real) with opinion findings — streak-len 3
    prev = PanelVerdict(
        gate="architecture-review", verdict="needs_revision",
        findings=[PanelFinding(severity="opinion", vendor="claude",
                                summary="style note")],
        source=str(scope), source_hash=hash_file(scope),
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="test", run_ts="2026-04-20T00:00:00Z",
    )
    write_verdict(feature_active / "panel-architecture-review.json", prev)
    # Arm auto-pass
    write_state(feature_active, RevisionState(
        M={**{g: 0 for g in ALL_PANEL_GATES},
           "architecture-review": M_MAX},
        auto_pass_next={**{g: False for g in ALL_PANEL_GATES},
                         "architecture-review": True},
    ))

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
    result = orch._advance_gate("demo", feature_active, "architecture-review", logger)

    assert result.success
    # Verdict is now pass with audit fields
    v = load_verdict(feature_active / "panel-architecture-review.json")
    assert v.verdict == "pass"
    assert v.auto_pass_reason == "minor_streak"
    assert v.auto_pass_meta["minor_streak_len"] == M_MAX
    # Flag cleared
    assert load_state(feature_active).auto_pass_next["architecture-review"] is False


# ---- amendment semantics --------------------------------------------

def test_reset_on_amendment_zeros_M_and_auto_pass_next(active):
    write_state(active, RevisionState(
        G=5,
        L={g: 1 for g in REVISION_GATES},
        M={**{g: 2 for g in ALL_PANEL_GATES}, "prd-review": 1},
        auto_pass_next={**{g: False for g in ALL_PANEL_GATES},
                         "architecture-review": True},
    ))
    s = reset_on_amendment(active)
    assert s.G == 6
    assert all(v == 0 for v in s.L.values())
    assert all(v == 0 for v in s.M.values())
    assert all(v is False for v in s.auto_pass_next.values())


# ---- CLI status surface ---------------------------------------------

def test_cli_status_surfaces_M_and_auto_pass(git_repo, feature_active, capsys):
    from autodev.cli import main
    (feature_active / "prd.md").write_text("# prd\n")
    write_state(feature_active, RevisionState(
        M={**{g: 0 for g in ALL_PANEL_GATES}, "architecture-review": 2},
        auto_pass_next={**{g: False for g in ALL_PANEL_GATES},
                         "test-plan-review": True},
    ))
    code = main(["status", "demo", "--repo-root", str(git_repo)])
    assert code == 0
    out = capsys.readouterr().out
    assert "minor streak:" in out
    assert "architecture-review=2" in out
    assert "auto-pass armed:" in out
    assert "test-plan-review" in out


# ---- counter sizing -------------------------------------------------

def test_L_MAX_is_3_and_G_MAX_is_12():
    """G21 reduced budgets since minor iterations no longer consume
    them."""
    assert L_MAX == 3
    assert G_MAX == 12


def test_M_MAX_is_3():
    assert M_MAX == 3
    assert M_MAX_PRD == 1


# ---- state schema round-trip ---------------------------------------

def test_write_then_load_roundtrips_M_and_auto_pass(active):
    original = RevisionState(
        G=2,
        L={"architecture-review": 1, "test-plan-review": 0},
        M={"prd-review": 0, "architecture-review": 2,
           "test-plan-review": 1, "close-approval": 0},
        auto_pass_next={"prd-review": False, "architecture-review": True,
                         "test-plan-review": False, "close-approval": False},
    )
    write_state(active, original)
    loaded = load_state(active)
    assert loaded.G == 2
    assert loaded.M["architecture-review"] == 2
    assert loaded.auto_pass_next["architecture-review"] is True
    assert loaded.auto_pass_next["close-approval"] is False


def test_invalid_M_value_rejected(active):
    import json as _json
    state_path(active).write_text(_json.dumps({
        "G": 0, "L": {}, "M": {"architecture-review": M_MAX + 1}, "auto_pass_next": {},
    }) + "\n")
    with pytest.raises(Exception):
        load_state(active)
