"""g-24 — build-driven diagnostic routing.

When a build halt (g-23) carries a ``diagnosis`` on its blocking
deviation(s), orchestrator routes the rerun to the named upstream
layer via the existing StalenessCascade, instead of halting. PRD and
ambiguous diagnoses still halt. L is a unified per-layer counter
(panel OR route bumps); G two-stages on first build entry (12 → 18)
and resets on amendment.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev.artifacts.build import (
    BuildReport, load_build, write_build,
)
from autodev.artifacts.revision_state import (
    G_MAX, G_MAX_POST_DEV, L_MAX, RevisionState, gate_for_layer, load_state,
    mark_dev_entered, producer_stage_for_layer, reset_on_amendment,
    write_state,
)
from autodev.artifacts.scope import Scope, ScopeItem, write_scope
from autodev.errors import GatePending, SchemaError
from autodev.orchestrator import Orchestrator, OrchestratorConfig
from autodev.revision_loop import (
    DecisionKind, RouteDecision, route_to_layer,
)
from autodev.state.log import JsonlLog
from autodev.vendors.config import STAGES, StageSpec, VendorsConfig


def _orch(repo_root: Path) -> Orchestrator:
    vendors = VendorsConfig(
        path=repo_root / "vendors.yml",
        stages={
            s: StageSpec(stage=s, vendor="claude", model="fake", timeout_sec=30)
            for s in STAGES
        },
    )
    return Orchestrator(OrchestratorConfig(
        repo_root=repo_root, vendors=vendors, session_id="test",
    ))


def _write_scope(active: Path, ids: list[str]) -> None:
    items = [ScopeItem(id=sid, description=f"desc {sid}", prd_ref="§1", status="active")
             for sid in ids]
    scope = Scope(
        source="docs/features/demo/active/prd.md",
        source_hash="sha256:" + "0" * 64,
        written="2026-04-20",
        feature="demo",
        mode="fresh",
        diff_base="main",
        in_scope=items,
        excluded=[],
    )
    write_scope(active / "scope.json", scope)


def _write_build_with_diagnosis(
    active: Path, *, scope_id: str, layer: str, evidence: str = "a" * 20,
    proposed_rerun_from: str | None = None,
    extra_deviations: list[dict] | None = None,
    blocking: bool = True,
) -> None:
    dx: dict = {"defective_layer": layer, "evidence": evidence}
    if proposed_rerun_from is not None:
        dx["proposed_rerun_from"] = proposed_rerun_from
    deviations = [{
        "scope_id": scope_id, "severity": "blocking", "blocking": True,
        "detail": "blocks because upstream defect", "diagnosis": dx,
    }]
    if extra_deviations:
        deviations.extend(extra_deviations)
    write_build(active / "build.json", BuildReport(
        source=str(active / "scope.json"),
        source_hash="sha256:" + "0" * 64,
        written="2026-04-20",
        test_cmd_run="pytest",
        test_exit_code=0,
        test_results={"passed": 1, "failed": 0, "skipped": 0},
        files_changed=["src/x.py"],
        deviations=deviations,
        blocking=blocking,
    ))


# ---- g-24a: schema ---------------------------------------------------


def test_schema_valid_diagnosis_roundtrips(feature_active):
    _write_scope(feature_active, ["t-1"])
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="scope",
        evidence="PRD §3.2 contradicts scope item t-1",
        proposed_rerun_from="scope",
    )
    report = load_build(feature_active / "build.json")
    assert report.blocking is True
    assert report.deviations[0]["diagnosis"]["defective_layer"] == "scope"
    assert report.deviations[0]["diagnosis"]["proposed_rerun_from"] == "scope"


def test_schema_missing_evidence_rejected(feature_active):
    from autodev.artifacts.build import _validate
    _validate_target = {
        "source": "x", "source_hash": "sha256:" + "0" * 64,
        "written": "2026-04-20",
        "test_cmd_run": "pytest", "test_exit_code": 0,
        "test_results": {"passed": 0, "failed": 0, "skipped": 0},
        "files_changed": [],
        "deviations": [{
            "scope_id": "t-1", "severity": "blocking", "blocking": True,
            "detail": "x",
            "diagnosis": {"defective_layer": "scope"},
        }],
    }
    with pytest.raises(SchemaError, match="evidence"):
        _validate(_validate_target)


def test_schema_evidence_too_short_rejected(feature_active):
    from autodev.artifacts.build import _validate
    obj = {
        "source": "x", "source_hash": "sha256:" + "0" * 64,
        "written": "2026-04-20",
        "test_cmd_run": "pytest", "test_exit_code": 0,
        "test_results": {"passed": 0, "failed": 0, "skipped": 0},
        "files_changed": [],
        "deviations": [{
            "scope_id": "t-1", "severity": "blocking", "blocking": True,
            "detail": "x",
            "diagnosis": {"defective_layer": "plan", "evidence": "too short"},
        }],
    }
    with pytest.raises(SchemaError, match="at least"):
        _validate(obj)


def test_schema_unknown_layer_rejected():
    from autodev.artifacts.build import _validate
    obj = {
        "source": "x", "source_hash": "sha256:" + "0" * 64,
        "written": "2026-04-20",
        "test_cmd_run": "pytest", "test_exit_code": 0,
        "test_results": {"passed": 0, "failed": 0, "skipped": 0},
        "files_changed": [],
        "deviations": [{
            "scope_id": "t-1", "severity": "blocking", "blocking": True,
            "detail": "x",
            "diagnosis": {
                "defective_layer": "nonsense",
                "evidence": "a" * 20,
            },
        }],
    }
    with pytest.raises(SchemaError, match="defective_layer"):
        _validate(obj)


def test_orchestrator_rejects_diagnosis_with_unknown_scope_id(feature_active):
    _write_scope(feature_active, ["t-1"])
    _write_build_with_diagnosis(
        feature_active, scope_id="t-999",  # not in scope
        layer="scope", evidence="z" * 20,
    )
    orch = _orch(feature_active.parent.parent.parent.parent)
    logger = JsonlLog(feature_active / "log.jsonl")
    from autodev.errors import PreflightError
    with pytest.raises(PreflightError, match="not in scope.json"):
        orch._enforce_build_blocking(feature_active, logger, "demo")


# ---- g-24b: routing --------------------------------------------------


def test_route_to_scope_invalidates_and_bumps_L(git_repo, feature_active):
    _write_scope(feature_active, ["t-1"])
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="scope",
        evidence="PRD §4 implies constraint absent from scope",
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    decision = orch._enforce_build_blocking(feature_active, logger, "demo")

    assert decision is not None
    assert decision.kind == DecisionKind.LOCAL_REVISE
    assert decision.layer == "scope"
    assert decision.stage_to_rerun == "scope"
    # scope.json invalidated.
    assert not (feature_active / "scope.json").exists()
    # L[architecture-review] bumped.
    s = load_state(feature_active)
    assert s.L["architecture-review"] == 1
    assert s.G == 1


def test_route_to_plan_invalidates_trace_and_test_plan(git_repo, feature_active):
    _write_scope(feature_active, ["t-1"])
    (feature_active / "trace.md").write_text("<!-- stub trace -->\n")
    (feature_active / "test-plan.md").write_text("<!-- stub test-plan -->\n")
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="plan",
        evidence="test-plan.md cases are un-writable against PRD",
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    decision = orch._enforce_build_blocking(feature_active, logger, "demo")

    assert decision.layer == "plan"
    assert decision.stage_to_rerun == "plan"
    assert not (feature_active / "trace.md").exists()
    assert not (feature_active / "test-plan.md").exists()
    s = load_state(feature_active)
    assert s.L["test-plan-review"] == 1


def test_route_test_plan_folds_into_plan(git_repo, feature_active):
    _write_scope(feature_active, ["t-1"])
    (feature_active / "trace.md").write_text("<!-- t -->\n")
    (feature_active / "test-plan.md").write_text("<!-- tp -->\n")
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="test-plan",
        evidence="one test-case cannot be written for the stated invariant",
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    decision = orch._enforce_build_blocking(feature_active, logger, "demo")

    assert decision.layer == "test-plan"
    assert decision.stage_to_rerun == "plan"  # folded
    s = load_state(feature_active)
    assert s.L["test-plan-review"] == 1


def test_halt_on_prd_diagnosis(git_repo, feature_active):
    _write_scope(feature_active, ["t-1"])
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="prd",
        evidence="PRD R4a contradicts PRD §2.3 — irreducible",
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    with pytest.raises(GatePending, match="prd") as exc:
        orch._enforce_build_blocking(feature_active, logger, "demo")

    assert "not auto-routable" in exc.value.detail


def test_halt_on_ambiguous_diagnosis(git_repo, feature_active):
    _write_scope(feature_active, ["t-1"])
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="ambiguous",
        evidence="defect exists but localization unclear despite retries",
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    with pytest.raises(GatePending, match="ambiguous"):
        orch._enforce_build_blocking(feature_active, logger, "demo")


def test_halt_mixing_routable_and_prd_still_halts(git_repo, feature_active):
    """If ANY blocking deviation diagnoses prd/ambiguous, halt (don't
    route on the other). PRD defect dominates."""
    _write_scope(feature_active, ["t-1", "t-2"])
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="scope",
        evidence="scope omits invariant PRD §2 requires",
        extra_deviations=[{
            "scope_id": "t-2", "severity": "blocking", "blocking": True,
            "detail": "prd contradiction",
            "diagnosis": {
                "defective_layer": "prd",
                "evidence": "PRD contradicts itself between §3 and §4",
            },
        }],
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    with pytest.raises(GatePending):
        orch._enforce_build_blocking(feature_active, logger, "demo")
    # No routing happened.
    s = load_state(feature_active)
    assert s.G == 0
    assert (feature_active / "scope.json").exists()


def test_route_prefers_scope_over_plan(git_repo, feature_active):
    """When multiple routable diagnoses co-exist, route to the
    shallowest (closest to PRD)."""
    _write_scope(feature_active, ["t-1", "t-2"])
    (feature_active / "trace.md").write_text("<!-- t -->\n")
    (feature_active / "test-plan.md").write_text("<!-- tp -->\n")
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="plan",
        evidence="trace row contradicts scope — plan layer broken",
        extra_deviations=[{
            "scope_id": "t-2", "severity": "blocking", "blocking": True,
            "detail": "scope gap",
            "diagnosis": {
                "defective_layer": "scope",
                "evidence": "scope item is missing a whole concern PRD §5 requires",
            },
        }],
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    decision = orch._enforce_build_blocking(feature_active, logger, "demo")
    assert decision.layer == "scope"  # shallower wins


# ---- L_MAX / G_MAX halts --------------------------------------------


def test_halt_when_L_at_max(git_repo, feature_active):
    _write_scope(feature_active, ["t-1"])
    # Pre-populate revision-state at L=L_MAX.
    s = RevisionState()
    s.L["architecture-review"] = L_MAX
    write_state(feature_active, s)

    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="scope",
        evidence="third substantive iteration still cannot land — scope is the issue",
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    with pytest.raises(GatePending, match="L"):
        orch._enforce_build_blocking(feature_active, logger, "demo")


def test_halt_when_G_at_effective_max(git_repo, feature_active):
    _write_scope(feature_active, ["t-1"])
    # g_dev_bumped true → G_MAX effective 18.
    s = RevisionState()
    s.g_dev_bumped = True
    s.G = G_MAX_POST_DEV
    write_state(feature_active, s)

    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="scope",
        evidence="long history of iteration with legitimate defects",
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    with pytest.raises(GatePending, match="global revision ceiling"):
        orch._enforce_build_blocking(feature_active, logger, "demo")


def test_g_dev_bumped_idempotent(feature_active):
    """mark_dev_entered on first call returns changed=True; subsequent
    calls return changed=False and leave g_dev_bumped set."""
    _s1, changed1 = mark_dev_entered(feature_active)
    assert changed1 is True
    assert _s1.g_dev_bumped is True
    assert _s1.effective_g_max() == G_MAX_POST_DEV

    _s2, changed2 = mark_dev_entered(feature_active)
    assert changed2 is False
    assert _s2.g_dev_bumped is True


def test_g_dev_bumped_reset_on_amendment(feature_active):
    mark_dev_entered(feature_active)
    s = load_state(feature_active)
    assert s.effective_g_max() == G_MAX_POST_DEV

    s2 = reset_on_amendment(feature_active)
    assert s2.g_dev_bumped is False
    assert s2.effective_g_max() == G_MAX  # back to 12


# ---- Feedback / attribution -----------------------------------------


def test_route_writes_build_path_to_pending_feedback(git_repo, feature_active):
    _write_scope(feature_active, ["t-1"])
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="scope",
        evidence="a real concrete pointer into the PRD here",
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    orch._enforce_build_blocking(feature_active, logger, "demo")

    s = load_state(feature_active)
    fb = s.pending_feedback.get("scope", [])
    assert len(fb) == 1
    assert fb[0].endswith("build.json")
    # Path is absolute so the rerun agent can read it.
    assert Path(fb[0]).is_absolute()


def test_attribution_log_records_source_route(git_repo, feature_active):
    _write_scope(feature_active, ["t-1"])
    _write_build_with_diagnosis(
        feature_active, scope_id="t-1", layer="scope",
        evidence="evidence meeting the 16-char minimum floor",
    )
    orch = _orch(git_repo)
    logger = JsonlLog(feature_active / "log.jsonl")

    orch._enforce_build_blocking(feature_active, logger, "demo")

    events = [json.loads(line) for line in
              (feature_active / "log.jsonl").read_text().splitlines()]
    lbump = [e for e in events if e.get("event") == "l-bump"]
    assert len(lbump) == 1
    assert lbump[0]["detail"]["source"] == "route"
    assert lbump[0]["detail"]["layer"] == "scope"
    assert "build.json#/deviations/0" in lbump[0]["detail"]["trigger_ref"]


# ---- route_to_layer unit tests ---------------------------------------


def test_route_to_layer_halts_on_prd(feature_active):
    d = route_to_layer(feature_active, "prd",
                       trigger_ref="build.json#/deviations/0")
    assert d.kind == DecisionKind.HALT_FOR_HUMAN
    assert d.layer == "prd"


def test_route_to_layer_halts_on_ambiguous(feature_active):
    d = route_to_layer(feature_active, "ambiguous",
                       trigger_ref="build.json#/deviations/0")
    assert d.kind == DecisionKind.HALT_FOR_HUMAN


def test_gate_for_layer_mapping():
    assert gate_for_layer("scope") == "architecture-review"
    assert gate_for_layer("plan") == "test-plan-review"
    assert gate_for_layer("test-plan") == "test-plan-review"


def test_producer_stage_for_layer_mapping():
    assert producer_stage_for_layer("scope") == "scope"
    assert producer_stage_for_layer("plan") == "plan"
    assert producer_stage_for_layer("test-plan") == "plan"
