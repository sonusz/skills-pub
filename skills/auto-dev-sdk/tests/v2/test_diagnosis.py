"""Unit tests — mechanism 2: fingerprint history, rework-mode selector,
counterfactual stall classification, diagnosis outputs."""
from __future__ import annotations

import json

from autodev.artifacts.fingerprint_history import (
    clear_history, compute_fingerprint, load_history, record_declined,
    record_verdict,
)
from autodev.artifacts.verdict import IssueCluster, PanelFinding, PanelVerdict
from autodev.diagnosis import (
    MODE_PATCH, MODE_ROOT_CAUSE, check_and_diagnose, read_rework_mode,
    record_skip_as_declined, select_rework_mode,
)

PRD = """# PRD: demo

## Problem
x

## Users
y

## Requirements

### R1: Alpha
a

### R2: Beta
b

## Constraints
z

## Success Criteria
s

## Out of Scope
o

## Assurance

Default: strict

| Req | Rigor | Rationale |
|---|---|---|
| R2 | core | cli entry; edge failures fixed when they show up |
"""


def _finding(summary="cache corrupts on concurrent write", severity="risk",
             refs=("prd:R1",), targets=("primary_pair.design.md",),
             failure_class="edge", category=None):
    return PanelFinding(
        severity=severity, vendor="claude", summary=summary,
        targets=list(targets), evidence_refs=list(refs),
        failure_class=failure_class, category=category,
    )


def _verdict(
    findings, run_ts, gate="design-review", source_hash="sha256:0",
    issue_clusters=None, release_threshold="P1",
):
    return PanelVerdict(
        gate=gate, verdict="needs_revision", findings=findings,
        source="design-packet.json", source_hash=source_hash,
        prompt_file="p", prompt_hash="sha256:1",
        harness_version="test", run_ts=run_ts,
        issue_clusters=issue_clusters or [],
        release_threshold=release_threshold,
    )


def _feature(tmp_path, prd=PRD):
    active = tmp_path / "docs" / "features" / "demo" / "active"
    active.mkdir(parents=True)
    (active / "prd.md").write_text(prd, encoding="utf-8")
    return active


# ---- fingerprints ----------------------------------------------------------

def test_fingerprint_stability_ignores_summary_but_tracks_structure():
    a = _finding()
    b = _finding()
    assert compute_fingerprint(a) == compute_fingerprint(b)
    c = _finding(summary="a different defect entirely")
    assert compute_fingerprint(a) == compute_fingerprint(c)
    d = _finding(summary="  CACHE   corrupts on concurrent write ")
    assert compute_fingerprint(a) == compute_fingerprint(d)
    e = _finding(category="invented")
    assert compute_fingerprint(a) != compute_fingerprint(e)


def test_record_verdict_idempotent_and_recurrence(tmp_path):
    active = _feature(tmp_path)
    f = _finding()
    r1 = record_verdict(active, "design-review", _verdict([f], "t1"))
    assert r1.new_round and not r1.recurring
    # Same run_ts again — idempotent, still no recurrence.
    r1b = record_verdict(active, "design-review", _verdict([f], "t1"))
    assert not r1b.new_round and not r1b.recurring
    # New round, same finding, CHANGED source hash (producer reran) —
    # recurrence.
    r2 = record_verdict(
        active, "design-review", _verdict([f], "t2", source_hash="sha256:1"),
    )
    assert r2.new_round
    assert r2.recurring == {compute_fingerprint(f)}


def test_reworded_finding_recurs_by_coarse_identity(tmp_path):
    active = _feature(tmp_path)
    first = _finding(summary="cache corrupts on concurrent write")
    second = _finding(summary="concurrent writers can damage the cache")
    record_verdict(active, "design-review", _verdict([first], "t1"))
    report = record_verdict(
        active, "design-review",
        _verdict([second], "t2", source_hash="sha256:1"),
    )
    assert report.recurring == {compute_fingerprint(second)}


def test_panel_rerun_on_unchanged_package_is_not_recurrence(tmp_path):
    """P1 regression (review round 2): a panel re-run without a
    producer change (cache invalidation, consulted-doc drift) replays
    identical findings under a new run_ts — that must NOT fabricate a
    stall diagnosis."""
    active = _feature(tmp_path)
    f = _finding()
    record_verdict(active, "design-review", _verdict([f], "t1"))
    r2 = record_verdict(active, "design-review", _verdict([f], "t2"))
    assert r2.new_round
    assert r2.recurring == set()          # same source_hash → no evidence
    # And check_and_diagnose stays on the normal path.
    diag = check_and_diagnose(active, "design-review", _verdict([f], "t3"))
    assert diag is None


def test_opinion_findings_not_fingerprinted(tmp_path):
    active = _feature(tmp_path)
    f = _finding(severity="opinion")
    r = record_verdict(active, "design-review", _verdict([f], "t1"))
    assert r.blocking == set()


def test_clear_history(tmp_path):
    active = _feature(tmp_path)
    record_verdict(active, "design-review", _verdict([_finding()], "t1"))
    clear_history(active)
    assert load_history(active).rounds == {}


# ---- rework-mode selector --------------------------------------------------

def test_mode_patch_few_fresh_anchored():
    assert select_rework_mode([_finding()], set()) == MODE_PATCH
    assert select_rework_mode([_finding(), _finding(summary="x")], set()) == MODE_PATCH


def test_mode_root_cause_many_findings():
    blocking = [_finding(summary=f"f{i}") for i in range(3)]
    assert select_rework_mode(blocking, set()) == MODE_ROOT_CAUSE


def test_mode_root_cause_unanchored():
    assert select_rework_mode([_finding(targets=())], set()) == MODE_ROOT_CAUSE


def test_mode_root_cause_on_empty():
    assert select_rework_mode([], set()) == MODE_ROOT_CAUSE


def test_clustered_duplicate_findings_count_as_one_patch_ticket(tmp_path):
    active = _feature(tmp_path)
    findings = [_finding(summary=f"wording {i}") for i in range(3)]
    for i, finding in enumerate(findings, 1):
        finding.finding_id = f"reviewer:{i}"
    cluster = IssueCluster(
        cluster_id="issue-cache", finding_ids=[f.finding_id for f in findings],
        summary="cache concurrency defect", priority="P1",
    )
    diag = check_and_diagnose(
        active, "design-review",
        _verdict(findings, "t1", issue_clusters=[cluster]),
    )
    assert diag is None
    payload = json.loads((active / "rework-mode.json").read_text())
    assert payload["mode"] == MODE_PATCH
    assert payload["blocking_count"] == 1


# ---- check_and_diagnose ----------------------------------------------------

def test_fresh_round_writes_rework_mode_and_no_diagnosis(tmp_path):
    active = _feature(tmp_path)
    diag = check_and_diagnose(active, "design-review", _verdict([_finding()], "t1"))
    assert diag is None
    assert read_rework_mode(active) == MODE_PATCH
    payload = json.loads((active / "rework-mode.json").read_text())
    assert payload["verdict_run_ts"] == "t1"


def test_recurrence_rigor_pivotal(tmp_path):
    active = _feature(tmp_path)
    # R1 is strict; edge risk on R1 blocks. One level down (core) an
    # edge risk downgrades → rigor-pivotal.
    f = _finding(refs=("prd:R1",))
    assert check_and_diagnose(active, "design-review", _verdict([f], "t1")) is None
    diag = check_and_diagnose(
        active, "design-review", _verdict([f], "t2", source_hash="sha256:1"),
    )
    assert diag is not None
    assert diag.classification == "rigor-pivotal"
    assert diag.pivot_rs == ["R1"]
    d = json.loads((active / "diagnosis.json").read_text())
    assert "re_audit_question" in d
    assert "R1" in d["re_audit_question"]


def test_recurrence_coherence_when_downgrade_cannot_unblock(tmp_path):
    active = _feature(tmp_path)
    # invented category blocks at every level → counterfactual still
    # blocks → coherence stall.
    f = _finding(category="invented", refs=("prd:R1",))
    assert check_and_diagnose(active, "design-review", _verdict([f], "t1")) is None
    diag = check_and_diagnose(
        active, "design-review", _verdict([f], "t2", source_hash="sha256:1"),
    )
    assert diag is not None
    assert diag.classification == "coherence"
    d = json.loads((active / "diagnosis.json").read_text())
    assert "amendment_guidance" in d


def test_recurrence_unresolvable_refs_is_coherence(tmp_path):
    active = _feature(tmp_path)
    f = _finding(refs=())
    check_and_diagnose(active, "design-review", _verdict([f], "t1"))
    diag = check_and_diagnose(
        active, "design-review", _verdict([f], "t2", source_hash="sha256:1"),
    )
    assert diag is not None
    assert diag.classification == "coherence"
    assert diag.pivot_rs == []


def test_declined_pairs_become_accepted_known_blocker(tmp_path):
    active = _feature(tmp_path)
    f = _finding(refs=("prd:R1",))
    check_and_diagnose(active, "design-review", _verdict([f], "t1"))
    diag = check_and_diagnose(
        active, "design-review", _verdict([f], "t2", source_hash="sha256:1"),
    )
    assert diag.classification == "rigor-pivotal"
    # Human keeps the tolerance via skip-gate → declined recorded.
    n = record_skip_as_declined(active, "design-review")
    assert n == 1
    # Same stall recurs → no re-ask, accepted-known-blocker.
    diag2 = check_and_diagnose(
        active, "design-review", _verdict([f], "t3", source_hash="sha256:2"),
    )
    assert diag2.classification == "accepted-known-blocker"
    d = json.loads((active / "diagnosis.json").read_text())
    assert "re_audit_question" not in d


def test_record_skip_as_declined_ignores_other_gate(tmp_path):
    active = _feature(tmp_path)
    f = _finding(refs=("prd:R1",))
    check_and_diagnose(active, "design-review", _verdict([f], "t1"))
    check_and_diagnose(
        active, "design-review", _verdict([f], "t2", source_hash="sha256:1"),
    )
    assert record_skip_as_declined(active, "close-approval") == 0


def test_counterfactual_restores_reported_severity(tmp_path):
    # A finding the real filter already downgraded on core must be
    # restored to its reported severity inside the counterfactual, or
    # the test would trivially "unblock".
    active = _feature(tmp_path)
    f = _finding(refs=("prd:R2",))   # R2 core; edge risk downgraded IRL
    f.severity_reported = "risk"
    f.severity = "opinion"
    # Not blocking → never fingerprinted, no diagnosis path at all.
    r = record_verdict(active, "design-review", _verdict([f], "t1"))
    assert r.blocking == set()


def test_declined_record_roundtrip(tmp_path):
    active = _feature(tmp_path)
    record_declined(active, {("abc", "R1"), ("abc", "R2")})
    record_declined(active, {("abc", "R1")})   # dedup
    h = load_history(active)
    assert h.declined_pairs() == {("abc", "R1"), ("abc", "R2")}
