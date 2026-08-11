"""Unit tests for fingerprint telemetry and panel rework-mode selection."""
from __future__ import annotations

import json

from autodev.artifacts.fingerprint_history import (
    clear_history, compute_fingerprint, load_history, record_verdict,
)
from autodev.artifacts.verdict import IssueCluster, PanelFinding, PanelVerdict
from autodev.diagnosis import (
    MODE_PATCH, MODE_ROOT_CAUSE, prepare_panel_rework, read_rework_mode,
    select_rework_mode,
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
    # Re-reviewing an unchanged package remains eligible for a narrow patch.
    mode = prepare_panel_rework(
        active, "design-review", _verdict([f], "t3"),
    )
    assert mode == MODE_PATCH


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
    mode = prepare_panel_rework(
        active, "design-review",
        _verdict(findings, "t1", issue_clusters=[cluster]),
    )
    assert mode == MODE_PATCH
    payload = json.loads((active / "rework-mode.json").read_text())
    assert payload["mode"] == MODE_PATCH
    assert payload["blocking_count"] == 1


# ---- prepare_panel_rework --------------------------------------------------

def test_fresh_round_writes_rework_mode(tmp_path):
    active = _feature(tmp_path)
    mode = prepare_panel_rework(
        active, "design-review", _verdict([_finding()], "t1"),
    )
    assert mode == MODE_PATCH
    assert read_rework_mode(active) == MODE_PATCH
    payload = json.loads((active / "rework-mode.json").read_text())
    assert payload["verdict_run_ts"] == "t1"


def test_recurrence_selects_root_cause_without_diagnosis(tmp_path):
    active = _feature(tmp_path)
    f = _finding(refs=("prd:R1",))
    assert prepare_panel_rework(
        active, "design-review", _verdict([f], "t1"),
    ) == MODE_PATCH
    mode = prepare_panel_rework(
        active, "design-review", _verdict([f], "t2", source_hash="sha256:1"),
    )
    assert mode == MODE_ROOT_CAUSE
    assert read_rework_mode(active) == MODE_ROOT_CAUSE
    assert not (active / "diagnosis.json").exists()


def test_invariant_recurrence_never_halts_early(tmp_path):
    active = _feature(tmp_path)
    f = _finding(category="invented", refs=("prd:R1",))
    prepare_panel_rework(active, "design-review", _verdict([f], "t1"))
    mode = prepare_panel_rework(
        active, "design-review", _verdict([f], "t2", source_hash="sha256:1"),
    )
    assert mode == MODE_ROOT_CAUSE
    assert not (active / "diagnosis.json").exists()


def test_repeated_recurrence_keeps_using_revision_loop(tmp_path):
    active = _feature(tmp_path)
    f = _finding()
    prepare_panel_rework(active, "design-review", _verdict([f], "t1"))
    assert prepare_panel_rework(
        active, "design-review", _verdict([f], "t2", source_hash="sha256:1"),
    ) == MODE_ROOT_CAUSE
    assert prepare_panel_rework(
        active, "design-review", _verdict([f], "t3", source_hash="sha256:2"),
    ) == MODE_ROOT_CAUSE
    assert not (active / "diagnosis.json").exists()
