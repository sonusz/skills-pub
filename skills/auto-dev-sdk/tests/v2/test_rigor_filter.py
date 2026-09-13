"""Unit tests — autodev/panel/rigor_filter.py (processing matrix,
Rules A/B, severity-slot rewrite)."""
from __future__ import annotations

import json

from autodev.artifacts.scope import Scope, ScopeItem
from autodev.artifacts.verdict import (
    PanelFinding, PanelVerdict, ReviewDecision, load_verdict, write_verdict,
)
from autodev.assurance import AssuranceMap
from autodev.panel.rigor_filter import (
    apply_rigor_filter, has_effective_blocking, one_level_down, resolve_rs,
)


def _assurance(default="loose", per_r=None):
    return AssuranceMap(present=True, default=default, per_r=per_r or {})


def _finding(severity="risk", category=None, refs=None, failure_class=None,
             missized_direction=None, summary="finding"):
    return PanelFinding(
        severity=severity, vendor="claude", summary=summary,
        category=category, evidence_refs=refs or [],
        failure_class=failure_class, missized_direction=missized_direction,
    )


def _scope():
    return Scope(
        source="prd.md", source_hash="sha256:0", written="2026-08-05",
        feature="demo", mode="fresh", diff_base="main",
        in_scope=[
            ScopeItem(id="s-1", description="d", prd_ref=["R1", "Constraints"]),
            ScopeItem(id="s-2", description="d", prd_ref=["R2"]),
        ],
    )


# ---- no-op guard ----------------------------------------------------------

def test_absent_assurance_is_noop():
    f = _finding(severity="risk", failure_class="edge", refs=["prd:R1"])
    result = apply_rigor_filter([f], AssuranceMap())
    assert result.examined == 0
    assert f.severity == "risk"


# ---- matrix cells ---------------------------------------------------------

def test_strict_r_edge_risk_blocks():
    f = _finding(failure_class="edge", refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "strict"}))
    assert f.severity == "risk"
    assert f.severity_reported is None


def test_core_r_edge_risk_downgrades():
    f = _finding(failure_class="edge", refs=["prd:R1"])
    r = apply_rigor_filter([f], _assurance(per_r={"R1": "core"}))
    assert f.severity == "opinion"
    assert f.severity_reported == "risk"
    assert r.downgraded == 1


def test_core_r_mainline_risk_blocks():
    f = _finding(failure_class="mainline", refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "core"}))
    assert f.severity == "risk"


def test_loose_r_mainline_risk_downgrades():
    f = _finding(failure_class="mainline", refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}))
    assert f.severity == "opinion"


def test_loose_r_iv_missing_downgrades():
    f = _finding(severity="invariant_violation", category="missing",
                 refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}))
    assert f.severity == "opinion"
    assert f.severity_reported == "invariant_violation"


def test_core_r_iv_missing_blocks():
    f = _finding(severity="invariant_violation", category="missing",
                 refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "core"}))
    assert f.severity == "invariant_violation"


def test_loose_r_iv_contradiction_blocks():
    f = _finding(severity="invariant_violation", category="undelivered",
                 refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}))
    assert f.severity == "invariant_violation"


def test_loose_r_iv_uncategorized_blocks():
    f = _finding(severity="invariant_violation", refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}))
    assert f.severity == "invariant_violation"


def test_invented_blocks_on_loose():
    f = _finding(category="invented", refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}))
    assert f.severity == "risk"


def test_redundant_blocks_at_every_rigor_and_respects_release_threshold():
    for level in ("strict", "core", "loose"):
        f = _finding(category="redundant", refs=["prd:R1"])
        f.priority = "P1"
        apply_rigor_filter([f], _assurance(per_r={"R1": level}))
        assert f.severity == "risk", level
        assert has_effective_blocking([f], "P1")
        assert not has_effective_blocking([f], "P0")


def test_missized_fine_downgrades_on_core_and_loose():
    for level in ("core", "loose"):
        f = _finding(category="missized", missized_direction="fine",
                     refs=["prd:R1"])
        apply_rigor_filter([f], _assurance(per_r={"R1": level}))
        assert f.severity == "opinion", level


def test_missized_fine_blocks_on_strict():
    f = _finding(category="missized", missized_direction="fine",
                 refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "strict"}))
    assert f.severity == "risk"


def test_missized_coarse_blocks_on_loose():
    f = _finding(category="missized", missized_direction="coarse",
                 refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}))
    assert f.severity == "risk"


def test_missized_absent_direction_fail_closed_to_coarse():
    f = _finding(category="missized", refs=["prd:R1"])
    r = apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}))
    assert f.severity == "risk"
    assert any(e.kind == "fail-closed" and "missized" in e.reason
               for e in r.events)


def test_opinion_untouched():
    f = _finding(severity="opinion", refs=["prd:R1"])
    r = apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}))
    assert f.severity == "opinion"
    assert r.examined == 0


# ---- Rule B (fail closed) -------------------------------------------------

def test_risk_absent_failure_class_treated_mainline_on_core():
    f = _finding(refs=["prd:R1"])          # no failure_class
    r = apply_rigor_filter([f], _assurance(per_r={"R1": "core"}))
    assert f.severity == "risk"            # mainline blocks on core
    assert any("failure-class-absent" in e.reason for e in r.events)


def test_risk_absent_failure_class_still_downgrades_on_loose():
    f = _finding(refs=["prd:R1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}))
    assert f.severity == "opinion"         # mainline downgrades on loose


def test_unresolvable_refs_fail_closed_to_strict():
    f = _finding(failure_class="edge", refs=["design:2. Primitives"])
    r = apply_rigor_filter([f], _assurance(default="loose"))
    assert f.severity == "risk"
    assert any("unresolvable" in e.reason for e in r.events)


def test_empty_refs_fail_closed():
    f = _finding(failure_class="edge")
    r = apply_rigor_filter([f], _assurance(default="loose"))
    assert f.severity == "risk"
    assert r.fail_closed == 1


# ---- Rule A (max across cited Rs) -----------------------------------------

def test_rule_a_max_across_rs():
    f = _finding(failure_class="edge", refs=["prd:R1", "prd:R2"])
    apply_rigor_filter(
        [f], _assurance(per_r={"R1": "loose", "R2": "strict"}),
    )
    assert f.severity == "risk"            # strict wins


def test_scope_resolution_via_prd_ref():
    f = _finding(failure_class="mainline", refs=["scope:s-1"])
    apply_rigor_filter([f], _assurance(per_r={"R1": "loose"}), _scope())
    assert f.severity == "opinion"


def test_trace_ref_resolves_through_scope():
    f = _finding(failure_class="edge", refs=["trace:s-2.r3"])
    apply_rigor_filter([f], _assurance(per_r={"R2": "core"}), _scope())
    assert f.severity == "opinion"


def test_reviewer_cited_r_beats_scope_mapping():
    # Goodhart guard: scope item cites only loose R1, but the reviewer
    # cites strict R2 directly — max wins, finding blocks.
    f = _finding(failure_class="edge", refs=["scope:s-1", "prd:R2"])
    apply_rigor_filter(
        [f], _assurance(per_r={"R1": "loose", "R2": "strict"}), _scope(),
    )
    assert f.severity == "risk"


def test_resolve_rs_shapes():
    f = _finding(refs=["prd:R2", "scope:s-1", "trace:s-2.r1",
                       "test-plan:s-2.t4", "code:foo.py:3"])
    assert resolve_rs(f, _scope()) == ["R1", "R2"]


# ---- counterfactual mode ---------------------------------------------------

def test_override_level_counterfactual():
    f = _finding(failure_class="edge", refs=["prd:R1"])
    apply_rigor_filter(
        [f], _assurance(per_r={"R1": "strict"}), override_level={"R1": "core"},
    )
    assert f.severity == "opinion"


def test_one_level_down():
    assert one_level_down("strict") == "core"
    assert one_level_down("core") == "loose"
    assert one_level_down("loose") == "loose"


# ---- helpers / serialization ----------------------------------------------

def test_has_effective_blocking():
    a = _finding(severity="opinion")
    b = _finding(severity="risk")
    assert not has_effective_blocking([a])
    assert has_effective_blocking([a, b])


def test_release_threshold_defers_p1_but_not_p0():
    p1 = _finding(severity="risk")
    p1.priority = "P1"
    p0 = _finding(severity="risk")
    p0.priority = "P0"
    assert not has_effective_blocking([p1], "P0")
    assert has_effective_blocking([p0, p1], "P0")
    # Historical default remains P1.
    assert has_effective_blocking([p1])


def test_verdict_p0_policy_retains_deferred_finding_without_blocking():
    finding = _finding(severity="invariant_violation")
    finding.priority = "P1"
    v = PanelVerdict(
        gate="design-review", verdict="pass", findings=[finding],
        source="x", source_hash="sha256:0", prompt_file="p",
        prompt_hash="sha256:1", harness_version="test", run_ts="t",
        release_threshold="P0",
    )
    assert v.findings == [finding]
    assert v.blocking_findings() == []
    assert not v.effectively_blocks()


def test_unreconciled_halt_for_human_remains_fail_closed():
    finding = _finding(severity="opinion")
    v = PanelVerdict(
        gate="design-review", verdict="fail", findings=[finding],
        source="x", source_hash="sha256:0", prompt_file="p",
        prompt_hash="sha256:1", harness_version="test", run_ts="t",
        decision=ReviewDecision(
            node="design_review", outcome="halt_for_human", blocking=True,
            severity="risk", summary="human decision required",
        ),
    )
    assert v.effectively_blocks()


def test_verdict_roundtrip_with_rigor_fields(tmp_path):
    f = _finding(failure_class="edge", category="missized",
                 missized_direction="fine", refs=["prd:R1"])
    f.severity_reported = "risk"
    f.severity = "opinion"
    v = PanelVerdict(
        gate="design-review", verdict="pass", findings=[f],
        source="design-packet.json", source_hash="sha256:0",
        prompt_file="p", prompt_hash="sha256:1",
        harness_version="test", run_ts="2026-08-05T00:00:00+00:00",
        decision_overridden_by_rigor={"outcome": "retry_design"},
    )
    path = tmp_path / "panel-design-review.json"
    write_verdict(path, v)
    raw = json.loads(path.read_text())
    assert raw["findings"][0]["severity"] == "opinion"
    assert raw["findings"][0]["severity_reported"] == "risk"
    assert raw["decision_overridden_by_rigor"]["outcome"] == "retry_design"
    loaded = load_verdict(path)
    lf = loaded.findings[0]
    assert lf.severity == "opinion"
    assert lf.severity_reported == "risk"
    assert lf.category == "missized"
    assert lf.missized_direction == "fine"
    assert lf.evidence_refs == ["prd:R1"]
    assert not loaded.effectively_blocks()


# ---- P1 regressions (review round 1) ---------------------------------------

def test_finding_from_synth_preserves_rigor_fields():
    """P1-1: dropping category/evidence_refs/failure_class/
    missized_direction at synth→PanelFinding conversion silently
    fail-closes the rigor filter on every live finding."""
    from autodev.panel.runner import _finding_from_synth
    f = _finding_from_synth("claude", {
        "severity": "risk",
        "summary": "cache corrupts",
        "targets": ["primary_pair.design.md"],
        "category": "missized",
        "evidence_refs": ["prd:R5", "scope:s-1"],
        "failure_class": "edge",
        "missized_direction": "fine",
    })
    assert f.category == "missized"
    assert f.evidence_refs == ["prd:R5", "scope:s-1"]
    assert f.failure_class == "edge"
    assert f.missized_direction == "fine"


# ---- P1 regressions (review round 2) ---------------------------------------

def test_hallucinated_r_token_fails_closed():
    """P1: a reviewer-cited `prd:R99` that doesn't exist in the PRD
    must NOT resolve to the Assurance Default level (fail open) — it
    drops, and with no other resolvable refs the finding lands in the
    unresolvable-refs fail-closed branch with its loud event."""
    from autodev.assurance import parse_assurance
    prd = (
        "# PRD: demo\n\n## Problem\nx\n\n## Users\ny\n\n## Requirements\n\n"
        "### R1: Alpha\na\n\n## Constraints\nz\n\n## Success Criteria\ns\n\n"
        "## Out of Scope\no\n\n## Assurance\n\nDefault: loose\n\n"
        "| Req | Rigor | Rationale |\n|---|---|---|\n| R1 | strict | core |\n"
    )
    assurance, errors = parse_assurance(prd)
    assert errors == []
    assert assurance.known_rs == {"R1"}
    f = _finding(failure_class="mainline", refs=["prd:R99"])
    r = apply_rigor_filter([f], assurance)
    assert f.severity == "risk"                      # NOT downgraded
    assert r.downgraded == 0
    assert any("unresolvable" in e.reason for e in r.events)


def test_known_rs_none_keeps_direct_map_behavior():
    """Direct-constructed maps (known_rs=None) don't reject R tokens —
    only PRD-parsed maps carry the marker set."""
    f = _finding(failure_class="mainline", refs=["prd:R99"])
    apply_rigor_filter([f], _assurance(default="loose"))
    assert f.severity == "opinion"


def test_valid_r_token_still_resolves_with_known_rs():
    from autodev.assurance import parse_assurance
    prd = (
        "# PRD: demo\n\n## Requirements\n\n### R1: Alpha\na\n\n"
        "## Assurance\n\nDefault: loose\n"
    )
    assurance, _ = parse_assurance(prd)
    f = _finding(failure_class="mainline", refs=["prd:R1"])
    r = apply_rigor_filter([f], assurance)
    assert f.severity == "opinion"                   # legit downgrade
    assert r.fail_closed == 0
