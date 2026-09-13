"""g-22 / phase-5b — ralph loop primitives.

The orchestrator-side wiring (inner build→review iteration) lands in a
follow-up; this file pins the contract of the pure functions in
``autodev/ralph.py``: the review.md status parser, stall detection,
regression detection, RalphState round-trip, and the end-to-end
progression helpers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev import ralph
from autodev.artifacts.scope import Scope, ScopeItem, write_scope
from autodev.errors import SchemaError


# ---- ralph-review.json parser (v3-core) ----------------------------


def _write_ralph_review(
    path: Path,
    classifications: list[dict],
    *,
    design_conformance: dict | None = None,
) -> None:
    path.write_text(json.dumps({
        "classifications": classifications,
        "summary": {},
        "design_conformance": design_conformance or {
            "verdict": "Aligned",
            "findings": [],
        },
    }))


def test_parser_json_per_scope_rollup(tmp_path):
    """Per-trace-row → per-scope rollup takes the worst rank."""
    p = tmp_path / "ralph-review.json"
    _write_ralph_review(p, [
        {"req_id": "t-1.r1", "scope_id": "t-1",
         "classification": "Fully", "evidence": "x"},
        {"req_id": "t-1.r2", "scope_id": "t-1",
         "classification": "Missing", "evidence": "x"},
        {"req_id": "t-2.r1", "scope_id": "t-2",
         "classification": "Fully", "evidence": "x"},
    ])
    got = ralph.parse_review_statuses(p)
    # t-1 has Fully + Missing → rollup takes worst (Missing)
    # t-2 only Fully → Fully
    assert got == {"t-1": "Missing", "t-2": "Fully"}


def test_parser_json_unknown_status_rejects(tmp_path):
    p = tmp_path / "ralph-review.json"
    _write_ralph_review(p, [
        {"req_id": "t-1.r1", "scope_id": "t-1",
         "classification": "Maybe", "evidence": "x"},
    ])
    with pytest.raises(SchemaError, match="unknown classification"):
        ralph.parse_review_statuses(p)


def test_parser_json_duplicate_req_rejects(tmp_path):
    p = tmp_path / "ralph-review.json"
    _write_ralph_review(p, [
        {"req_id": "t-1.r1", "scope_id": "t-1",
         "classification": "Fully", "evidence": "x"},
        {"req_id": "t-1.r1", "scope_id": "t-1",
         "classification": "Fully", "evidence": "x"},
    ])
    with pytest.raises(SchemaError, match="duplicate req_id"):
        ralph.parse_review_statuses(p)


def test_parser_invalid_json_rejects(tmp_path):
    p = tmp_path / "ralph-review.json"
    p.write_text("not json")
    with pytest.raises(SchemaError, match="invalid JSON"):
        ralph.parse_review_statuses(p)


def test_parser_missing_classifications_rejects(tmp_path):
    p = tmp_path / "ralph-review.json"
    p.write_text('{"summary": {}}')
    with pytest.raises(SchemaError, match="classifications"):
        ralph.parse_review_statuses(p)


def test_parser_missing_design_conformance_rejects(tmp_path):
    p = tmp_path / "ralph-review.json"
    p.write_text(json.dumps({
        "classifications": [{
            "req_id": "t-1.r1", "scope_id": "t-1",
            "classification": "Fully", "evidence": "src/x.py:1",
        }],
        "summary": {},
    }))
    with pytest.raises(SchemaError, match="design_conformance"):
        ralph.parse_review_statuses(p)


def test_blocking_design_drift_caps_affected_scope_at_deviated(tmp_path):
    p = tmp_path / "ralph-review.json"
    _write_ralph_review(p, [{
        "req_id": "t-1.r1", "scope_id": "t-1",
        "classification": "Fully", "evidence": "src/x.py:1",
    }], design_conformance={
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["t-1"],
            "design_ref": "design.md:10-14",
            "evidence": "src/x.py:1-9",
            "difference": "Uses a second authority path",
            "correction": "Remove the unaccepted path",
        }],
    })
    assert ralph.parse_review_statuses(p) == {"t-1": "Deviated"}


def test_redundancy_correction_caps_only_affected_fully_scope_until_cleared(
    tmp_path,
):
    p = tmp_path / "ralph-review.json"
    rows = [
        {"req_id": "t-1.r1", "scope_id": "t-1",
         "classification": "Fully", "evidence": "src/x.py:1"},
        {"req_id": "t-2.r1", "scope_id": "t-2",
         "classification": "Fully", "evidence": "src/y.py:1"},
    ]
    _write_ralph_review(p, rows, design_conformance={
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["t-1"],
            "design_ref": "design.md:10-14",
            "evidence": "src/x.py:1-9",
            "difference": "Wrapper duplicates the existing helper",
            "correction": "Delete the wrapper and reuse the helper",
        }],
    })
    assert ralph.parse_review_statuses(p) == {
        "t-1": "Deviated", "t-2": "Fully",
    }

    _write_ralph_review(p, rows)
    assert ralph.parse_review_statuses(p) == {
        "t-1": "Fully", "t-2": "Fully",
    }


def test_design_conformance_rejects_inconsistent_verdict(tmp_path):
    p = tmp_path / "ralph-review.json"
    _write_ralph_review(p, [{
        "req_id": "t-1.r1", "scope_id": "t-1",
        "classification": "Fully", "evidence": "src/x.py:1",
    }], design_conformance={
        "verdict": "Aligned",
        "findings": [{
            "scope_ids": ["t-1"],
            "design_ref": "design.md:10-14",
            "evidence": "src/x.py:1-9",
            "difference": "Uses method B instead of method A",
            "correction": "Realign the implementation",
        }],
    })
    with pytest.raises(SchemaError, match="inconsistent"):
        ralph.parse_review_statuses(p)


def test_design_conformance_rejects_unknown_scope(tmp_path):
    p = tmp_path / "ralph-review.json"
    _write_ralph_review(p, [{
        "req_id": "t-1.r1", "scope_id": "t-1",
        "classification": "Fully", "evidence": "src/x.py:1",
    }], design_conformance={
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["t-ghost"],
            "design_ref": "design.md:10-14",
            "evidence": "src/x.py:1-9",
            "difference": "Uses method B instead of method A",
            "correction": "Realign the implementation",
        }],
    })
    with pytest.raises(SchemaError, match="not present in classifications"):
        ralph.parse_review_statuses(p)


def test_parser_missing_file(tmp_path):
    with pytest.raises(SchemaError, match="not found"):
        ralph.parse_review_statuses(tmp_path / "nope.json")


# ---- active_scope_ids ----------------------------------------------


def test_active_scope_ids(tmp_path):
    p = tmp_path / "scope.json"
    scope = Scope(
        source="prd.md", source_hash="sha256:" + "0" * 64,
        written="2026-04-20", feature="demo", mode="fresh", diff_base="main",
        in_scope=[
            ScopeItem(id="t-1", description="a", prd_ref=["§1"], status="active"),
            ScopeItem(id="t-2", description="b", prd_ref=["§2"], status="active"),
            ScopeItem(id="t-3", description="c", prd_ref=["§3"], status="removed"),
            ScopeItem(id="t-4", description="d", prd_ref=["§4"],
                      status="superseded", superseded_by="t-1"),
        ],
        excluded=[],
    )
    write_scope(p, scope)
    assert ralph.active_scope_ids(p) == {"t-1", "t-2"}


# ---- stall detector ------------------------------------------------


def test_stall_when_k_consecutive_non_progress():
    hist = [set(), {"t-1"}, {"t-1"}, {"t-1"}, {"t-1"}]
    # iter 1 added t-1; iter 2,3,4 added nothing → 3 consecutive non-progress.
    assert ralph.detect_stall(hist, k=3) is True


def test_no_stall_with_recent_progress():
    hist = [set(), {"t-1"}, {"t-1"}, {"t-1", "t-2"}]
    # Last iter added t-2 → not a stall.
    assert ralph.detect_stall(hist, k=3) is False


def test_not_enough_history_for_stall():
    hist = [set(), {"t-1"}, {"t-1"}]
    # Only 2 completed iters; can't fire a K=3 stall yet.
    assert ralph.detect_stall(hist, k=3) is False


def test_shrink_counts_as_non_progress():
    hist = [set(), {"t-1", "t-2"}, {"t-1"}, set(), set()]
    # Shrinking is non-progress by the ⊆ rule.
    assert ralph.detect_stall(hist, k=3) is True


def test_stall_k_equals_one():
    """Pathological but allowed: K=1 means any single non-progress iter halts."""
    hist = [set(), {"t-1"}, {"t-1"}]
    assert ralph.detect_stall(hist, k=1) is True


# ---- regression detector ------------------------------------------


def test_regression_fully_to_partial():
    hist = [
        {},
        {"t-1": "Fully"},
        {"t-1": "Partial"},
    ]
    regs = ralph.detect_regressions(hist)
    assert len(regs) == 1
    assert regs[0].scope_id == "t-1"
    assert regs[0].prior_status == "Fully"
    assert regs[0].new_status == "Partial"
    assert regs[0].iter_index == 2


def test_regression_disappeared_items_treated_as_missing():
    hist = [
        {},
        {"t-1": "Fully", "t-2": "Fully"},
        {"t-1": "Fully"},  # t-2 vanished
    ]
    regs = ralph.detect_regressions(hist)
    assert len(regs) == 1
    assert regs[0].scope_id == "t-2"
    assert regs[0].new_status == "Missing"


def test_no_regression_across_partial_to_fully():
    hist = [
        {},
        {"t-1": "Partial"},
        {"t-1": "Fully"},
    ]
    assert ralph.detect_regressions(hist) == []


def test_regression_partial_to_missing_not_tracked():
    """detect_regressions only tracks Fully → lesser, not any decay.
    Partial → Missing is a non-progress (caught by stall detector) but
    NOT a regression in the alert sense."""
    hist = [
        {},
        {"t-1": "Partial"},
        {"t-1": "Missing"},
    ]
    assert ralph.detect_regressions(hist) == []


# ---- RalphState round-trip -----------------------------------------


def test_ralph_state_roundtrip(tmp_path):
    s = ralph.RalphState(
        source="scope.json", source_hash="sha256:abc",
        iter=2,
        fully_history=[set(), {"t-1"}, {"t-1", "t-2"}],
        statuses_history=[{}, {"t-1": "Fully"},
                          {"t-1": "Fully", "t-2": "Fully"}],
        regressions=[],
        trace_hash="sha256:trace",
        test_plan_hash="sha256:testplan",
        design_hash="sha256:design",
        started_at="2026-04-20T00:00:00Z",
        last_iter_at="2026-04-20T00:05:00Z",
    )
    ralph.write_ralph_state(tmp_path, s)
    loaded = ralph.load_ralph_state(tmp_path)
    assert loaded.iter == 2
    assert loaded.fully_history[-1] == {"t-1", "t-2"}
    assert loaded.statuses_history[-1] == {"t-1": "Fully", "t-2": "Fully"}
    assert loaded.trace_hash == "sha256:trace"
    assert loaded.test_plan_hash == "sha256:testplan"
    assert loaded.design_hash == "sha256:design"


def test_ralph_state_absent_returns_empty(tmp_path):
    s = ralph.load_ralph_state(tmp_path)
    assert s.iter == 0
    assert s.fully_history == [set()]
    assert s.started_at  # populated with "now"


def test_ralph_state_clear(tmp_path):
    s = ralph.RalphState(iter=1, fully_history=[set(), {"t-1"}])
    ralph.write_ralph_state(tmp_path, s)
    assert (tmp_path / "ralph-state.json").exists()
    ralph.clear_ralph_state(tmp_path)
    assert not (tmp_path / "ralph-state.json").exists()


def test_ralph_state_regression_roundtrip(tmp_path):
    s = ralph.RalphState(
        iter=2,
        fully_history=[set(), {"t-1"}, set()],
        statuses_history=[{}, {"t-1": "Fully"}, {"t-1": "Partial"}],
        regressions=[ralph.RegressionEvent(
            scope_id="t-1", iter_index=2,
            prior_status="Fully", new_status="Partial",
        )],
    )
    ralph.write_ralph_state(tmp_path, s)
    loaded = ralph.load_ralph_state(tmp_path)
    assert len(loaded.regressions) == 1
    assert loaded.regressions[0].scope_id == "t-1"
    assert loaded.regressions[0].iter_index == 2


# ---- record_iter high-level ----------------------------------------


def test_record_iter_progresses_state(tmp_path):
    s = ralph.RalphState()
    s, new_regs = ralph.record_iter(s, statuses={"t-1": "Fully"})
    assert s.iter == 1
    assert s.fully_history == [set(), {"t-1"}]
    assert new_regs == []


def test_record_iter_detects_new_regression():
    s = ralph.RalphState(
        iter=1,
        fully_history=[set(), {"t-1"}],
        statuses_history=[{}, {"t-1": "Fully"}],
    )
    s, new_regs = ralph.record_iter(s, statuses={"t-1": "Partial"})
    assert len(new_regs) == 1
    assert new_regs[0].scope_id == "t-1"
    assert s.iter == 2
    assert s.fully_history[-1] == set()
    assert len(s.regressions) == 1


# ---- is_complete ---------------------------------------------------


def test_is_complete_true_when_all_active_fully():
    s = ralph.RalphState(fully_history=[set(), {"t-1", "t-2"}])
    assert ralph.is_complete(s, {"t-1", "t-2"}) is True


def test_is_complete_false_when_missing_one():
    s = ralph.RalphState(fully_history=[set(), {"t-1"}])
    assert ralph.is_complete(s, {"t-1", "t-2"}) is False


def test_is_complete_superset_ok():
    """Review.md may list Fully for items not in active (e.g. deferred).
    is_complete is satisfied as long as active ⊆ fully."""
    s = ralph.RalphState(fully_history=[set(), {"t-1", "t-2", "t-ghost"}])
    assert ralph.is_complete(s, {"t-1", "t-2"}) is True


def test_validate_active_review_coverage_rejects_missing_active_item():
    with pytest.raises(SchemaError, match="missing active scope classifications"):
        ralph.validate_active_review_coverage({"t-1": "Fully"}, {"t-1", "t-2"})


# ---- stall message ---------------------------------------------


def test_build_stall_message_lists_incomplete_and_fully():
    s = ralph.RalphState(
        iter=5,
        fully_history=[set(), set(), {"t-1"}, {"t-1"}, {"t-1"}, {"t-1"}],
        statuses_history=[{}] * 6,
    )
    msg = ralph.build_stall_message(s, {"t-1", "t-2", "t-3"})
    assert "stalled" in msg
    assert "t-1" in msg
    assert "t-2" in msg
    assert "t-3" in msg
    assert "iter 3" in msg and "iter 5" in msg or "3→5" in msg


# ---- integration: fake dev progression ----------------------------


def test_fake_dev_converges_in_3_iters(tmp_path):
    """Multi-iter convergence scenario: 2 items/iter → done at iter 3.

    Mimics how the orchestrator would call record_iter after each
    review.md parse."""
    active = {"t-1", "t-2", "t-3", "t-4", "t-5", "t-6"}
    progressions = [
        {"t-1": "Fully", "t-2": "Fully", "t-3": "Missing", "t-4": "Missing",
         "t-5": "Missing", "t-6": "Missing"},
        {"t-1": "Fully", "t-2": "Fully", "t-3": "Fully", "t-4": "Fully",
         "t-5": "Missing", "t-6": "Missing"},
        {"t-1": "Fully", "t-2": "Fully", "t-3": "Fully", "t-4": "Fully",
         "t-5": "Fully", "t-6": "Fully"},
    ]
    s = ralph.RalphState()
    for i, statuses in enumerate(progressions, 1):
        s, _regs = ralph.record_iter(s, statuses=statuses)
        if i < 3:
            assert not ralph.is_complete(s, active)
            assert not ralph.detect_stall(s.fully_history)
    assert ralph.is_complete(s, active)
    assert s.iter == 3


def test_fake_dev_stalls_after_3_non_progress_iters():
    """Stall scenario: same 2-item Fully set across 4 iters → stall at iter 4."""
    active = {"t-1", "t-2", "t-3"}
    stalled_statuses = {"t-1": "Fully", "t-2": "Fully", "t-3": "Missing"}
    s = ralph.RalphState()
    # Iter 1: progress (0 → 2)
    s, _ = ralph.record_iter(
        s, statuses={"t-1": "Fully", "t-2": "Fully", "t-3": "Missing"},
    )
    assert not ralph.detect_stall(s.fully_history, k=3)
    # Iters 2, 3, 4: no Fully growth
    for _ in range(3):
        s, _ = ralph.record_iter(s, statuses=dict(stalled_statuses))
    assert ralph.detect_stall(s.fully_history, k=3)
    assert not ralph.is_complete(s, active)


def test_fake_dev_regression_logged_but_no_stall():
    """Regression alarm fires; next iter recovers; no halt."""
    s = ralph.RalphState()
    s, _ = ralph.record_iter(s, statuses={"t-1": "Fully", "t-2": "Partial"})
    s, regs = ralph.record_iter(
        s, statuses={"t-1": "Partial", "t-2": "Partial"},
    )
    assert len(regs) == 1
    assert regs[0].scope_id == "t-1"
    s, _ = ralph.record_iter(s, statuses={"t-1": "Fully", "t-2": "Fully"})
    assert ralph.is_complete(s, {"t-1", "t-2"})
    assert len(s.regressions) == 1  # the one earlier regression persists in log
