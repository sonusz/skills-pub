"""Unit tests for autodev.human_feedback (stage A; detail §7 stage-A items).

Covers: input validation per family (§1.2), the three merge functions
(§1.4-§1.6), the §2.1 "current" rules per point family, ralph state
recompute (including regressions), write-then-crash idempotency via
feedback_id, and the rejected path leaving the target file untouched.

Fixtures reuse tests/v2/conftest.py (git_repo, feature_active) and the
full-pipeline seeding helper from tests/v2/test_cascade_full_chain.py
(_seed_all_ten) rather than inventing a parallel fixture system.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from autodev import human_feedback as hf
from autodev import overrides_api as ov
from autodev import ralph
from autodev.artifacts.common import write_markdown_with_hash
from autodev.artifacts.scope import Scope, ScopeItem, write_scope
from autodev.artifacts.verdict import (
    PanelFinding, PanelVerdict, ReviewDecision, load_verdict, write_verdict,
)
from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write, atomic_write_json
from autodev.state.hashing import hash_file
from autodev.state.log import JsonlLog

from .test_cascade_full_chain import _seed_all_ten


def _logger(active: Path) -> JsonlLog:
    return JsonlLog(active / "log.jsonl")


def _events(active: Path) -> list[dict]:
    path = active / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _write_pending(active: Path, point: str, payload: dict) -> "hf.HumanFeedback":
    fb = hf.validate_feedback(active, point, payload)
    hf.write_feedback(active, fb)
    return fb


# =======================================================================
# §1.2 validation
# =======================================================================


class TestValidatePanelFamily:
    def test_valid_minimal(self, feature_active):
        fb = hf.validate_feedback(feature_active, "close-approval", {
            "verdict": "needs_revision",
            "findings": [{"severity": "risk", "summary": "missing timeout handling"}],
        })
        assert fb.review_point == "close-approval"
        assert fb.status == "pending"
        assert fb.feedback_id.startswith("hf-")

    def test_invalid_severity_enum(self, feature_active):
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "close-approval", {
                "verdict": "needs_revision",
                "findings": [{"severity": "bogus", "summary": "x"}],
            })

    def test_invalid_verdict_skipped_not_allowed(self, feature_active):
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "close-approval", {
                "verdict": "skipped",
                "findings": [{"severity": "risk", "summary": "x"}],
            })

    def test_unknown_review_point(self, feature_active):
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "nope", {
                "verdict": "needs_revision",
                "findings": [{"severity": "risk", "summary": "x"}],
            })

    def test_empty_findings_rejected(self, feature_active):
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "close-approval", {
                "verdict": "needs_revision", "findings": [],
            })

    def test_missing_top_level_key(self, feature_active):
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "close-approval", {
                "findings": [{"severity": "risk", "summary": "x"}],
            })

    def test_extra_top_level_key_rejected(self, feature_active):
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "close-approval", {
                "verdict": "needs_revision",
                "findings": [{"severity": "risk", "summary": "x"}],
                "extra": True,
            })

    def test_anchor_precheck_rejects_opinion_single_anchor_target(self, feature_active):
        with pytest.raises(SchemaError, match="anchor"):
            hf.validate_feedback(feature_active, "close-approval", {
                "verdict": "needs_revision",
                "findings": [{
                    "severity": "opinion", "summary": "prd wording is odd",
                    "targets": ["anchor.prd.md"],
                }],
            })

    def test_design_review_rejects_architecture_proposal_anchor_target(self, feature_active):
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "design-review", {
                "verdict": "needs_revision",
                "findings": [{
                    "severity": "risk", "summary": "conflicts with proposal",
                    "targets": ["anchor.architecture-proposal.md"],
                }],
            })


class TestValidateArchReviewFamily:
    def test_valid(self, feature_active):
        fb = hf.validate_feedback(feature_active, "arch-review", {
            "verdict": "needs_revision",
            "findings": [{
                "category": "invented", "prd_ref": None,
                "evidence": "arch-design.md:12", "problem": "adds an unrequested queue",
                "correction": "remove the queue component",
            }],
        })
        assert fb.review_point == "arch-review"

    def test_missing_category_required_prd_ref(self, feature_active):
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "arch-review", {
                "verdict": "needs_revision",
                "findings": [{
                    "category": "missing", "prd_ref": None,
                    "evidence": "e", "problem": "p", "correction": "c",
                }],
            })

    def test_verdict_findings_mismatch(self, feature_active):
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "arch-review", {
                "verdict": "pass",
                "findings": [{
                    "category": "invented", "prd_ref": None,
                    "evidence": "e", "problem": "p", "correction": "c",
                }],
            })


class TestValidateRalphReviewFamily:
    def _seed_scope(self, active: Path, ids: list[str]) -> None:
        scope = Scope(
            source="arch-design.md", source_hash="sha256:" + "0" * 64,
            written="2026-01-01", feature="demo", mode="fresh", diff_base="main",
            in_scope=[ScopeItem(id=i, description="x", prd_ref=["R1"]) for i in ids],
        )
        write_scope(active / "scope.json", scope)

    def test_valid(self, feature_active):
        self._seed_scope(feature_active, ["s-1", "s-2"])
        fb = hf.validate_feedback(feature_active, "ralph-review", {
            "verdict": "Deviated",
            "findings": [{
                "scope_ids": ["s-1"], "design_ref": "design.md:10",
                "evidence": "src/x.py:1", "difference": "not implemented",
                "correction": "implement it",
            }],
        })
        assert fb.review_point == "ralph-review"

    def test_scope_json_missing_rejected(self, feature_active):
        with pytest.raises(SchemaError, match="scope.json"):
            hf.validate_feedback(feature_active, "ralph-review", {
                "verdict": "Deviated",
                "findings": [{
                    "scope_ids": ["s-1"], "design_ref": "d", "evidence": "e",
                    "difference": "x", "correction": "c",
                }],
            })

    def test_unknown_scope_id_rejected(self, feature_active):
        self._seed_scope(feature_active, ["s-1"])
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "ralph-review", {
                "verdict": "Deviated",
                "findings": [{
                    "scope_ids": ["s-does-not-exist"], "design_ref": "d",
                    "evidence": "e", "difference": "x", "correction": "c",
                }],
            })

    def test_verdict_findings_mismatch(self, feature_active):
        self._seed_scope(feature_active, ["s-1"])
        with pytest.raises(SchemaError):
            hf.validate_feedback(feature_active, "ralph-review", {
                "verdict": "Aligned",
                "findings": [{
                    "scope_ids": ["s-1"], "design_ref": "d", "evidence": "e",
                    "difference": "x", "correction": "c",
                }],
            })


# =======================================================================
# §1.1 write_feedback overwrite / archive rules
# =======================================================================


def _fb(point: str, feedback_id: str, *, status: str = "pending") -> "hf.HumanFeedback":
    return hf.HumanFeedback(
        kind="human-feedback", schema_version=1, feedback_id=feedback_id,
        review_point=point, written="2026-01-01T00:00:00Z",
        verdict="needs_revision",
        findings=[{"severity": "risk", "summary": "x"}],
        status=status,
    )


class TestWriteFeedback:
    def test_overwrite_pending(self, feature_active):
        path1 = hf.write_feedback(feature_active, _fb("close-approval", "hf-1"))
        path2 = hf.write_feedback(feature_active, _fb("close-approval", "hf-2"))
        assert path1 == path2
        assert json.loads(path2.read_text())["feedback_id"] == "hf-2"
        # a pending predecessor is overwritten in place -- no archive file.
        archived = list(
            feature_active.glob("human-feedback-close-approval.*.json")
        )
        assert archived == []

    def test_archive_consumed(self, feature_active):
        path1 = hf.write_feedback(
            feature_active, _fb("close-approval", "hf-1", status="consumed"),
        )
        path2 = hf.write_feedback(feature_active, _fb("close-approval", "hf-2"))
        assert path1 == path2
        assert json.loads(path2.read_text())["feedback_id"] == "hf-2"

        archived = list(
            feature_active.glob("human-feedback-close-approval.*.json")
        )
        assert len(archived) == 1
        # filename-safe timestamp per detail §1.1: no colons; the archive
        # is named after the ARCHIVED file's own feedback_id (detail §10,
        # closing-review pin), not a fresh timestamp taken at rename time.
        assert ":" not in archived[0].name
        assert archived[0].name == "human-feedback-close-approval.hf-1.json"
        assert json.loads(archived[0].read_text())["feedback_id"] == "hf-1"

    def test_archive_rejected(self, feature_active):
        hf.write_feedback(
            feature_active, _fb("ralph-review", "hf-1", status="rejected"),
        )
        hf.write_feedback(feature_active, _fb("ralph-review", "hf-2"))

        archived = list(
            feature_active.glob("human-feedback-ralph-review.*.json")
        )
        assert len(archived) == 1
        assert ":" not in archived[0].name
        assert archived[0].name == "human-feedback-ralph-review.hf-1.json"
        assert json.loads(archived[0].read_text())["feedback_id"] == "hf-1"
        assert json.loads(archived[0].read_text())["status"] == "rejected"


class TestFeedbackIdCollisionResistance:
    def test_new_feedback_id_distinct_within_same_second(self, monkeypatch):
        """feedback_id collisions (detail §1.1 / §10, closing-review
        pin): two feedbacks validated within the same wall-clock second
        must still get distinct ids. Freeze the clock ``_new_feedback_id``
        reads so the timestamp component is identical across calls, and
        confirm the 6-hex-char random suffix (secrets.token_hex(3)) is
        what keeps them apart -- without it, every id generated within
        one second would collide."""
        from autodev import human_feedback_validate as hfv

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 1, 12, 0, 0, tzinfo=tz)

        monkeypatch.setattr(hfv, "datetime", _FrozenDatetime)
        ids = [hfv._new_feedback_id() for _ in range(50)]
        assert len(set(ids)) == 50
        assert all(i.startswith("hf-20260101T120000Z-") for i in ids)
        assert all(":" not in i for i in ids)


# =======================================================================
# §1.4 panel merge
# =======================================================================


def _seed_panel_verdict(
    active: Path, point: str, *, verdict: str, findings: list[PanelFinding],
    decision: ReviewDecision | None = None, release_threshold: str = "P1",
) -> Path:
    path = active / f"panel-{point}.json"
    write_verdict(path, PanelVerdict(
        gate=point, verdict=verdict, findings=findings,
        source="design-packet.json", source_hash="sha256:" + "1" * 64,
        prompt_file="p", prompt_hash="sha256:" + "0" * 64,
        harness_version="t", run_ts="2026-01-01T00:00:00Z",
        decision=decision, release_threshold=release_threshold,
    ))
    return path


class TestMergeIntoPanelVerdict:
    def test_pass_becomes_needs_revision_with_blocking_finding(self, feature_active):
        path = _seed_panel_verdict(
            feature_active, "close-approval", verdict="pass", findings=[],
        )
        before_hash = hash_file(path)
        fb = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-1",
            review_point="close-approval", written="2026-01-01T00:00:00Z",
            verdict="needs_revision",
            findings=[{
                "severity": "risk", "summary": "PII leak in export path",
                "priority": "P1",
            }],
        )
        v = load_verdict(path)
        v2, _override = hf.merge_into_panel_verdict(v, fb, active=feature_active, point="close-approval")
        write_verdict(path, v2)

        reread = load_verdict(path)
        assert reread.verdict == "needs_revision"
        human_findings = [f for f in reread.findings if f.vendor == "human"]
        assert len(human_findings) == 1
        assert human_findings[0].finding_id == "human:1"
        # source_hash unchanged by the merge.
        assert reread.source_hash == "sha256:" + "1" * 64
        assert f"human:{fb.feedback_id}" in reread.per_vendor_raw
        # every finding is covered by exactly one issue_cluster.
        covered = [fid for c in reread.issue_clusters for fid in c.finding_ids]
        all_ids = [f.finding_id for f in reread.findings]
        assert sorted(covered) == sorted(all_ids)
        assert len(covered) == len(set(covered))

    def test_opinion_only_does_not_block(self, feature_active):
        path = _seed_panel_verdict(
            feature_active, "close-approval", verdict="pass", findings=[],
        )
        fb = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-2",
            review_point="close-approval", written="2026-01-01T00:00:00Z",
            verdict="needs_revision",  # human's own verdict is NOT weighted
            findings=[{"severity": "opinion", "summary": "naming nit", "priority": "P2"}],
        )
        v = load_verdict(path)
        v2, _override = hf.merge_into_panel_verdict(v, fb, active=feature_active, point="close-approval")
        assert v2.verdict == "pass"

    def test_design_review_decision_override(self, feature_active):
        decision = ReviewDecision(
            node="design_review", outcome="pass", blocking=False,
            severity="opinion", summary="looked fine",
        )
        path = _seed_panel_verdict(
            feature_active, "design-review", verdict="pass", findings=[],
            decision=decision,
        )
        fb = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-3",
            review_point="design-review", written="2026-01-01T00:00:00Z",
            verdict="needs_revision",
            findings=[{
                "severity": "invariant_violation", "summary": "contradicts R1",
                "priority": "P0", "targets": ["primary_pair.design.md"],
            }],
        )
        v = load_verdict(path)
        v2, override_detail = hf.merge_into_panel_verdict(
            v, fb, active=feature_active, point="design-review",
        )

        assert v2.decision is not None
        assert v2.decision.outcome == "retry_design"
        assert v2.decision.blocking is False
        assert v2.verdict == "needs_revision"
        assert v2.decision_overridden_by_policy is not None
        assert v2.decision_overridden_by_policy["reason"] == (
            "human finding overrides pass decision"
        )
        assert v2.decision_overridden_by_policy.get("prior") is None
        # The event itself is no longer emitted by merge_into_panel_verdict
        # (detail §10, closing-review pin -- apply_pending emits it after
        # write_verdict succeeds); what this function returns instead is
        # the detail payload the caller should log.
        assert override_detail == {
            "gate": "design-review", "from_outcome": "pass",
            "to_outcome": "retry_design",
            "release_threshold": v2.release_threshold,
            "source": "human-feedback",
        }
        assert not any(
            e["event"] == "release-policy-decision-override"
            for e in _events(feature_active)
        )

    def test_design_review_decision_override_preserves_prior(self, feature_active):
        # A canonical pass decision that a PRIOR policy relaxation had
        # already overridden once (runner's own policy_override branch:
        # a retry_design decision downgraded to pass because the release
        # threshold was relaxed). A human blocking finding overrides it
        # again; the earlier override record must not be lost.
        decision = ReviewDecision(
            node="design_review", outcome="pass", blocking=False,
            severity="risk", summary="looked fine after threshold relaxation",
        )
        prior_override = {
            "node": "design_review", "outcome": "retry_design",
            "blocking": True, "severity": "risk", "summary": "original finding",
            "release_threshold": "P2",
        }
        path = _seed_panel_verdict(
            feature_active, "design-review", verdict="pass", findings=[],
            decision=decision, release_threshold="P2",
        )
        v = load_verdict(path)
        v.decision_overridden_by_policy = prior_override
        fb = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-3b",
            review_point="design-review", written="2026-01-01T00:00:00Z",
            verdict="needs_revision",
            findings=[{
                "severity": "invariant_violation", "summary": "contradicts R1",
                "priority": "P0", "targets": ["primary_pair.design.md"],
            }],
        )
        v2, _override = hf.merge_into_panel_verdict(v, fb, active=feature_active, point="design-review")
        assert v2.decision.outcome == "retry_design"
        assert v2.decision.blocking is False
        assert v2.decision_overridden_by_policy["prior"] == prior_override

    def test_fail_stays_fail_for_non_decision_gate(self, feature_active):
        # close-approval has no `decision` field; verdict is the only
        # signal. A pre-existing "fail" must not be downgraded to
        # "needs_revision" by a merge (detail §1.4 step 4).
        path = _seed_panel_verdict(
            feature_active, "close-approval", verdict="fail", findings=[],
        )
        fb = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-3c",
            review_point="close-approval", written="2026-01-01T00:00:00Z",
            verdict="needs_revision",
            findings=[{"severity": "risk", "summary": "another concern", "priority": "P1"}],
        )
        v = load_verdict(path)
        v2, _override = hf.merge_into_panel_verdict(v, fb, active=feature_active, point="close-approval")
        assert v2.verdict == "fail"

    def test_cluster_id_matches_detail_recipe(self, feature_active):
        import hashlib

        path = _seed_panel_verdict(
            feature_active, "close-approval", verdict="pass", findings=[],
        )
        fb = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-cid",
            review_point="close-approval", written="2026-01-01T00:00:00Z",
            verdict="needs_revision",
            findings=[{
                "severity": "risk", "summary": "Some Finding TEXT", "priority": "P1",
            }],
        )
        v2, _override = hf.merge_into_panel_verdict(
            load_verdict(path), fb, active=feature_active, point="close-approval",
        )
        human = next(f for f in v2.findings if f.vendor == "human")
        identity = json.dumps(
            {
                "summary": f"close-approval: {human.summary.strip().lower()}",
                "members": [human.finding_id],
            },
            sort_keys=True, separators=(",", ":"),
        )
        expected_id = "issue-" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:16]
        cluster = next(
            c for c in v2.issue_clusters if human.finding_id in c.finding_ids
        )
        assert cluster.cluster_id == expected_id

    def test_two_sequential_feedbacks_both_present(self, feature_active):
        path = _seed_panel_verdict(
            feature_active, "close-approval", verdict="pass", findings=[],
        )
        fb1 = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-aaa",
            review_point="close-approval", written="2026-01-01T00:00:00Z",
            verdict="needs_revision",
            findings=[{"severity": "risk", "summary": "first concern", "priority": "P1"}],
        )
        v1, _override = hf.merge_into_panel_verdict(
            load_verdict(path), fb1, active=feature_active, point="close-approval",
        )
        write_verdict(path, v1)

        fb2 = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-bbb",
            review_point="close-approval", written="2026-01-02T00:00:00Z",
            verdict="needs_revision",
            findings=[{"severity": "opinion", "summary": "second concern", "priority": "P2"}],
        )
        v2, _override = hf.merge_into_panel_verdict(
            load_verdict(path), fb2, active=feature_active, point="close-approval",
        )
        write_verdict(path, v2)

        final = load_verdict(path)
        ids = sorted(f.finding_id for f in final.findings if f.vendor == "human")
        assert ids == ["human:1", "human:2"]
        assert "human:hf-aaa" in final.per_vendor_raw
        assert "human:hf-bbb" in final.per_vendor_raw


# =======================================================================
# §1.5 arch-review merge
# =======================================================================


class TestMergeIntoArchReview:
    def _seed(self, active: Path) -> tuple[Path, Path]:
        prd = active / "prd.md"
        atomic_write(prd, "# PRD\n## Requirements\n### R1: thing\n")
        prd_h = hash_file(prd)
        write_markdown_with_hash(
            active / "arch-design.md", "## Goal\nbody\n",
            source=str(prd), source_hash=prd_h,
        )
        arch_design = active / "arch-design.md"
        arch_design_h = hash_file(arch_design)
        review_path = active / "arch-review.json"
        atomic_write_json(review_path, {
            "kind": "arch-review", "source": str(arch_design),
            "source_hash": arch_design_h, "prd_hash": prd_h,
            "written": "2026-01-01T00:00:00Z", "verdict": "pass", "findings": [],
        })
        return review_path, arch_design

    def test_pass_becomes_needs_revision(self, feature_active):
        review_path, arch_design = self._seed(feature_active)
        fb = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-4",
            review_point="arch-review", written="2026-01-01T00:00:00Z",
            verdict="needs_revision",
            findings=[{
                "category": "invented", "prd_ref": None,
                "evidence": "arch-design.md:5", "problem": "unrequested cache layer",
                "correction": "remove it",
            }],
        )
        review = hf.merge_into_arch_review(
            review_path, fb, arch_design_path=arch_design,
        )
        assert review.verdict == "needs_revision"
        assert len(review.findings) == 1

        raw = json.loads(review_path.read_text())
        assert raw["findings"][0]["vendor"] == "human"
        assert raw["findings"][0]["human_feedback_id"] == "hf-4"


# =======================================================================
# §1.6 ralph-review merge + state recompute
# =======================================================================


class TestMergeIntoRalphReviewAndStateRecompute:
    def _seed_scope(self, active: Path) -> None:
        scope = Scope(
            source="arch-design.md", source_hash="sha256:" + "0" * 64,
            written="2026-01-01", feature="demo", mode="fresh", diff_base="main",
            in_scope=[
                ScopeItem(id="s-1", description="x", prd_ref=["R1"]),
                ScopeItem(id="s-2", description="y", prd_ref=["R1"]),
            ],
        )
        write_scope(active / "scope.json", scope)

    def _seed_review(self, active: Path) -> Path:
        path = active / "ralph-review.json"
        atomic_write_json(path, {
            "classifications": [
                {"req_id": "s-1.r1", "scope_id": "s-1", "classification": "Fully"},
                {"req_id": "s-2.r1", "scope_id": "s-2", "classification": "Fully"},
            ],
            "design_conformance": {"verdict": "Aligned", "findings": []},
        })
        return path

    def test_merge_marks_scope_deviated(self, feature_active):
        self._seed_scope(feature_active)
        path = self._seed_review(feature_active)
        fb = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-5",
            review_point="ralph-review", written="2026-01-01T00:00:00Z",
            verdict="Deviated",
            findings=[{
                "scope_ids": ["s-1"], "design_ref": "design.md:1",
                "evidence": "src/x.py:1", "difference": "missing retry",
                "correction": "add retry",
            }],
        )
        statuses = hf.merge_into_ralph_review(path, fb, active=feature_active)
        assert statuses["s-1"] == "Deviated"
        assert statuses["s-2"] == "Fully"

        raw = json.loads(path.read_text())
        merged_findings = raw["design_conformance"]["findings"]
        assert len(merged_findings) == 1
        assert merged_findings[0]["vendor"] == "human"
        assert merged_findings[0]["human_feedback_id"] == "hf-5"

    def test_state_recompute_including_regression(self, feature_active):
        self._seed_scope(feature_active)
        path = self._seed_review(feature_active)

        # Two prior iterations both fully complete for s-1/s-2.
        state = ralph.RalphState(
            source=str(feature_active / "scope.json"),
            source_hash=hash_file(feature_active / "scope.json"),
            iter=2,
            fully_history=[set(), {"s-1", "s-2"}, {"s-1", "s-2"}],
            statuses_history=[
                {},
                {"s-1": "Fully", "s-2": "Fully"},
                {"s-1": "Fully", "s-2": "Fully"},
            ],
            regressions=[],
        )
        ralph.write_ralph_state(feature_active, state)

        fb = hf.HumanFeedback(
            kind="human-feedback", schema_version=1, feedback_id="hf-6",
            review_point="ralph-review", written="2026-01-01T00:00:00Z",
            verdict="Deviated",
            findings=[{
                "scope_ids": ["s-1"], "design_ref": "design.md:1",
                "evidence": "src/x.py:1", "difference": "regressed",
                "correction": "fix it",
            }],
        )
        statuses = hf.merge_into_ralph_review(path, fb, active=feature_active)
        hf._recompute_ralph_state(feature_active, statuses)

        recomputed = ralph.load_ralph_state(feature_active)
        assert recomputed.iter == 2  # unchanged
        assert recomputed.statuses_history[-1] == {"s-1": "Deviated", "s-2": "Fully"}
        assert recomputed.fully_history[-1] == {"s-2"}
        regs = [r for r in recomputed.regressions if r.iter_index == 2]
        assert len(regs) == 1
        assert regs[0].scope_id == "s-1"
        assert regs[0].prior_status == "Fully"
        assert regs[0].new_status == "Deviated"


# =======================================================================
# §2.1 "current" rules
# =======================================================================


class TestPanelCurrentRules:
    def test_fresh_pipeline_is_current_for_all_three_points(self, feature_active):
        _seed_all_ten(feature_active)
        assert hf._panel_current(feature_active, "design-review") == (True, None)
        assert hf._panel_current(feature_active, "trace-review") == (True, None)
        assert hf._panel_current(feature_active, "close-approval") == (True, None)

    def test_stale_after_arch_design_revision_without_regen(self, feature_active):
        _seed_all_ten(feature_active)
        prd_h = hash_file(feature_active / "prd.md")
        # Revise arch-design.md (new content -> new hash) without
        # regenerating design.md/scope.json/trace.md/test-plan.md, which
        # still record the OLD arch-design hash.
        write_markdown_with_hash(
            feature_active / "arch-design.md", "## Goal\nrevised body\n",
            source=str(feature_active / "prd.md"), source_hash=prd_h,
        )
        assert hf._panel_current(feature_active, "design-review") == (False, "not-current")
        assert hf._panel_current(feature_active, "trace-review") == (False, "not-current")

    def test_skipped_verdict_is_not_current(self, feature_active):
        _seed_all_ten(feature_active)
        path = feature_active / "panel-close-approval.json"
        v = load_verdict(path)
        v.verdict = "skipped"
        v.skip_reason = "manual override"
        write_verdict(path, v)
        assert hf._panel_current(feature_active, "close-approval") == (
            False, "skipped-verdict",
        )

    def test_active_skip_gate_override_is_not_current(self, feature_active):
        _seed_all_ten(feature_active)
        ov.record_skip_gate(feature_active, gate="close-approval", reason="testing")
        assert hf._panel_current(feature_active, "close-approval") == (
            False, "skip-gate-override",
        )

    def test_trace_review_file_missing_is_not_current(self, feature_active):
        _seed_all_ten(feature_active)
        (feature_active / "panel-trace-review.json").unlink()
        assert hf._panel_current(feature_active, "trace-review") == (False, "not-current")


class TestArchReviewCurrentRule:
    def _seed(self, active: Path) -> None:
        prd = active / "prd.md"
        atomic_write(prd, "# PRD\n## Requirements\n### R1: thing\n")
        prd_h = hash_file(prd)
        write_markdown_with_hash(
            active / "arch-design.md", "## Goal\nbody\n",
            source=str(prd), source_hash=prd_h,
        )
        arch_design_h = hash_file(active / "arch-design.md")
        atomic_write_json(active / "arch-review.json", {
            "kind": "arch-review", "source": str(active / "arch-design.md"),
            "source_hash": arch_design_h, "prd_hash": prd_h,
            "written": "2026-01-01T00:00:00Z", "verdict": "pass", "findings": [],
        })

    def test_current(self, feature_active):
        self._seed(feature_active)
        assert hf._arch_review_current(feature_active) == (True, None)

    def test_stale_after_arch_design_changes(self, feature_active):
        self._seed(feature_active)
        prd_h = hash_file(feature_active / "prd.md")
        write_markdown_with_hash(
            feature_active / "arch-design.md", "## Goal\nrevised\n",
            source=str(feature_active / "prd.md"), source_hash=prd_h,
        )
        assert hf._arch_review_current(feature_active) == (False, "not-current")


class TestRalphReviewCurrentRule:
    def _seed_scope_and_state(self, active: Path, *, iteration: int) -> None:
        # Reuses the scope.json _seed_all_ten already wrote (one active
        # item, "s-1") rather than overwriting it -- overwriting would
        # itself break cascade freshness for this test's purposes.
        atomic_write_json(active / "ralph-review.json", {
            "classifications": [
                {"req_id": "s-1.r1", "scope_id": "s-1", "classification": "Fully"},
            ],
            "design_conformance": {"verdict": "Aligned", "findings": []},
        })
        fully_history = [set()] + [{"s-1"}] * iteration
        statuses_history = [{}] + [{"s-1": "Fully"}] * iteration
        state = ralph.RalphState(
            iter=iteration, fully_history=fully_history,
            statuses_history=statuses_history,
        )
        ralph.write_ralph_state(active, state)

    def test_current_when_no_iteration_context_and_pipeline_open(self, feature_active):
        _seed_all_ten(feature_active)
        # Roll the pipeline back to "next stage is build" so the ralph
        # loop counts as still open.
        for name in (
            "build.json", "implementation-index.json",
            "implemented-spec.md", "prd-checklist.json",
            "panel-close-approval.json",
        ):
            (feature_active / name).unlink()
        self._seed_scope_and_state(feature_active, iteration=1)
        assert hf._ralph_review_current(feature_active) == (True, None)

    def test_not_current_when_context_ahead_by_one(self, feature_active):
        _seed_all_ten(feature_active)
        for name in (
            "build.json", "implementation-index.json",
            "implemented-spec.md", "prd-checklist.json",
            "panel-close-approval.json",
        ):
            (feature_active / name).unlink()
        self._seed_scope_and_state(feature_active, iteration=1)
        atomic_write_json(
            feature_active / "ralph-iteration-context.json",
            {"iteration": 2},  # state.iter + 1 -- build landed, review not run
        )
        assert hf._ralph_review_current(feature_active) == (False, "not-current")

    def test_not_current_when_pipeline_done(self, feature_active):
        _seed_all_ten(feature_active)
        self._seed_scope_and_state(feature_active, iteration=1)
        from autodev.state.cascade import StalenessCascade
        assert StalenessCascade(feature_active).next_stage() == "done"
        assert hf._ralph_review_current(feature_active) == (False, "pipeline-done")

    def test_iter_zero_not_current(self, feature_active):
        _seed_all_ten(feature_active)
        for name in (
            "build.json", "implementation-index.json",
            "implemented-spec.md", "prd-checklist.json",
            "panel-close-approval.json",
        ):
            (feature_active / name).unlink()
        # iter == 0: no iteration has been recorded yet, so there is
        # nothing an "at rest" merge could apply to (detail §2.1).
        self._seed_scope_and_state(feature_active, iteration=0)
        assert hf._ralph_review_current(feature_active) == (False, "not-current")


# =======================================================================
# idempotency + rejected path
# =======================================================================


class TestApplyPendingIdempotencyAndRejection:
    def test_write_then_crash_idempotency(self, feature_active):
        path = _seed_panel_verdict(
            feature_active, "close-approval", verdict="pass", findings=[],
        )
        payload = {
            "verdict": "needs_revision",
            "findings": [{"severity": "risk", "summary": "double-write hazard", "priority": "P1"}],
        }
        fb = _write_pending(feature_active, "close-approval", payload)

        # Simulate the merge having already landed on disk before a
        # crash prevented mark_consumed from running: merge once by
        # hand and write it, but leave the feedback file "pending".
        v = load_verdict(path)
        v2, _override = hf.merge_into_panel_verdict(v, fb, active=feature_active, point="close-approval")
        write_verdict(path, v2)
        assert hf.load_pending(feature_active, "close-approval") is not None

        result = hf.apply_pending(
            feature_active, "close-approval", log=_logger(feature_active),
            check_current=False,
        )
        assert result is True
        assert hf.load_pending(feature_active, "close-approval") is None
        final = load_verdict(path)
        human_findings = [f for f in final.findings if f.vendor == "human"]
        assert len(human_findings) == 1  # not duplicated

    def test_rejected_path_leaves_target_byte_identical(self, feature_active):
        # s-1 and s-2 are both active scopes, so the feedback's own
        # scope_ids (targeting s-2) validate cleanly against scope.json
        # at write time.
        scope = Scope(
            source="arch-design.md", source_hash="sha256:" + "0" * 64,
            written="2026-01-01", feature="demo", mode="fresh", diff_base="main",
            in_scope=[
                ScopeItem(id="s-1", description="x", prd_ref=["R1"]),
                ScopeItem(id="s-2", description="y", prd_ref=["R1"]),
            ],
        )
        write_scope(feature_active / "scope.json", scope)
        review_path = feature_active / "ralph-review.json"
        # ralph-review.json's classifications cover only s-1 -- e.g. this
        # review round ran before s-2 was added to scope.json. The
        # feedback targets s-2, a scope not present in the review's own
        # classifications: exactly the case detail §2.2's last bullet
        # describes ("scope_ids 不再是 active scope" after design re-ran).
        atomic_write_json(review_path, {
            "classifications": [
                {"req_id": "s-1.r1", "scope_id": "s-1", "classification": "Fully"},
            ],
            "design_conformance": {"verdict": "Aligned", "findings": []},
        })
        payload = {
            "verdict": "Deviated",
            "findings": [{
                "scope_ids": ["s-2"], "design_ref": "design.md:1",
                "evidence": "src/x.py:1", "difference": "missing retry",
                "correction": "add retry",
            }],
        }
        fb = _write_pending(feature_active, "ralph-review", payload)

        before_bytes = review_path.read_bytes()
        result = hf.apply_pending(
            feature_active, "ralph-review", log=_logger(feature_active),
            check_current=False,
        )
        assert result is False
        after_bytes = review_path.read_bytes()
        assert before_bytes == after_bytes

        rejected = hf.load_feedback(feature_active, "ralph-review")
        assert rejected.status == "rejected"
        assert rejected.rejected_reason
        assert "s-2" in rejected.rejected_reason
        assert "not present in classifications" in rejected.rejected_reason

        events = _events(feature_active)
        assert any(e["event"] == "human-feedback-rejected" for e in events)


# =======================================================================
# apply_pending end-to-end (core R4/R8, detail §2.1)
# =======================================================================


class TestApplyPendingEndToEnd:
    def test_check_current_true_merges_current_close_approval(self, feature_active):
        _seed_all_ten(feature_active)
        payload = {
            "verdict": "needs_revision",
            "findings": [{"severity": "risk", "summary": "PII leak", "priority": "P1"}],
        }
        _write_pending(feature_active, "close-approval", payload)

        result = hf.apply_pending(
            feature_active, "close-approval", log=_logger(feature_active),
            check_current=True,
        )
        assert result is True

        target = feature_active / "panel-close-approval.json"
        reread = load_verdict(target)
        assert reread.verdict == "needs_revision"
        assert any(f.vendor == "human" for f in reread.findings)

        fb_after = hf.load_feedback(feature_active, "close-approval")
        assert fb_after.status == "consumed"
        assert fb_after.consumed_at is not None
        assert fb_after.consumed_into == str(target)

        events = _events(feature_active)
        merged = [e for e in events if e["event"] == "human-feedback-merged"]
        consumed = [e for e in events if e["event"] == "human-feedback-consumed"]
        assert len(merged) == 1
        assert merged[0]["detail"]["review_point"] == "close-approval"
        assert merged[0]["detail"]["into"] == str(target)
        assert merged[0]["detail"]["verdict_before"] == "pass"
        assert merged[0]["detail"]["verdict_after"] == "needs_revision"
        assert len(consumed) == 1
        assert consumed[0]["detail"]["review_point"] == "close-approval"
        assert consumed[0]["detail"]["consumed_at"] == fb_after.consumed_at

    def test_check_current_false_merges_unconditionally(self, feature_active):
        target = _seed_panel_verdict(
            feature_active, "close-approval", verdict="pass", findings=[],
        )
        payload = {
            "verdict": "needs_revision",
            "findings": [{"severity": "risk", "summary": "hook-time merge", "priority": "P1"}],
        }
        _write_pending(feature_active, "close-approval", payload)

        result = hf.apply_pending(
            feature_active, "close-approval", log=_logger(feature_active),
            check_current=False,
        )
        assert result is True

        reread = load_verdict(target)
        assert reread.verdict == "needs_revision"
        assert any(f.vendor == "human" for f in reread.findings)

        fb_after = hf.load_feedback(feature_active, "close-approval")
        assert fb_after.status == "consumed"
        assert fb_after.consumed_at is not None
        assert fb_after.consumed_into == str(target)

        events = _events(feature_active)
        assert any(e["event"] == "human-feedback-merged" for e in events)
        consumed = [e for e in events if e["event"] == "human-feedback-consumed"]
        assert len(consumed) == 1
        assert consumed[0]["detail"]["consumed_at"] == fb_after.consumed_at

    def test_check_current_true_not_current_stays_pending(self, feature_active):
        _seed_all_ten(feature_active)
        prd_h = hash_file(feature_active / "prd.md")
        # Revise arch-design.md without regenerating design.md/scope.json/
        # trace.md/test-plan.md -- panel_design_review goes stale per the
        # cascade (same setup as TestPanelCurrentRules).
        write_markdown_with_hash(
            feature_active / "arch-design.md", "## Goal\nrevised body\n",
            source=str(feature_active / "prd.md"), source_hash=prd_h,
        )
        payload = {
            "verdict": "needs_revision",
            "findings": [{"severity": "risk", "summary": "not current yet", "priority": "P1"}],
        }
        _write_pending(feature_active, "design-review", payload)

        result = hf.apply_pending(
            feature_active, "design-review", log=_logger(feature_active),
            check_current=True,
        )
        assert result is False

        fb_after = hf.load_feedback(feature_active, "design-review")
        assert fb_after.status == "pending"
        assert not any(
            e["event"] in ("human-feedback-merged", "human-feedback-consumed")
            for e in _events(feature_active)
        )

    def test_design_review_override_emits_event_after_write(self, feature_active, monkeypatch):
        decision = ReviewDecision(
            node="design_review", outcome="pass", blocking=False,
            severity="opinion", summary="looked fine",
        )
        target = _seed_panel_verdict(
            feature_active, "design-review", verdict="pass", findings=[],
            decision=decision,
        )
        payload = {
            "verdict": "needs_revision",
            "findings": [{
                "severity": "invariant_violation", "summary": "contradicts R1",
                "priority": "P0", "targets": ["primary_pair.design.md"],
            }],
        }
        _write_pending(feature_active, "design-review", payload)

        # Track the on-disk write's call order against the logged events
        # in a single ordered sequence -- `_events()` alone only proves
        # override/merged/consumed are logged in some order relative to
        # EACH OTHER; it says nothing about whether the write itself
        # (which emits no log event of its own) happened before or after
        # the override event. Wrapping write_verdict records that too.
        call_order: list[str] = []
        real_write_verdict = hf.write_verdict

        def _tracking_write_verdict(path, v):
            call_order.append("write_verdict")
            return real_write_verdict(path, v)

        monkeypatch.setattr(hf, "write_verdict", _tracking_write_verdict)

        logger = _logger(feature_active)
        real_emit = logger.emit

        def _tracking_emit(*, stage, event, feature, detail=None):
            call_order.append(event)
            return real_emit(stage=stage, event=event, feature=feature, detail=detail)

        monkeypatch.setattr(logger, "emit", _tracking_emit)

        result = hf.apply_pending(
            feature_active, "design-review", log=logger, check_current=False,
        )
        assert result is True

        reread = load_verdict(target)
        assert reread.decision.outcome == "retry_design"
        assert reread.decision.blocking is False

        events = _events(feature_active)
        overrides = [
            e for e in events if e["event"] == "release-policy-decision-override"
        ]
        assert len(overrides) == 1
        assert overrides[0]["detail"]["source"] == "human-feedback"
        assert overrides[0]["detail"]["gate"] == "design-review"
        # The merged/consumed events must also be present, and event
        # ORDER must show the override event fires only once the merged
        # verdict has actually been written to disk (detail §10 d10):
        # write_verdict precedes the override event, which in turn
        # precedes the human-feedback-merged/consumed bookkeeping events
        # -- never the other way around.
        assert any(e["event"] == "human-feedback-merged" for e in events)
        assert any(e["event"] == "human-feedback-consumed" for e in events)
        write_idx = call_order.index("write_verdict")
        override_idx = call_order.index("release-policy-decision-override")
        merged_idx = call_order.index("human-feedback-merged")
        consumed_idx = call_order.index("human-feedback-consumed")
        assert write_idx < override_idx < consumed_idx < merged_idx
