"""Orchestration-level tests for human-feedback injection (stage B).

Companion to ``tests/v2/test_human_feedback.py`` (stage A: the
``autodev.human_feedback`` module in isolation). This file exercises the
FOUR real orchestrator hook sites (detail §2.2 / §2.1 entry A') and the
``autodev feedback`` CLI verb through the real ``Orchestrator``/``cli.main``
entry points, reusing the fixture patterns of
``tests/v2/test_pipeline_e2e_fakes.py`` and
``tests/v2/test_arch_design_loop_orchestrator.py`` (FakeCLI + fake panel
invoker -- no live LLM) and the fully-seeded pipeline helper from
``tests/v2/test_cascade_full_chain.py`` (``_seed_all_ten``).

Detail §7 describes two kinds of test material at the orchestration
level: a lettered (a)-(k) "两种时机" matrix (functions named
``test_<letter>_...`` below, one section per letter, matching that list
exactly -- no other test in this file uses an a-k letter prefix), and a
handful of separately-described, UNLETTERED items (the arch-review
family, the ralph-review "pending merged at the hook" case, one-time
consumption, verb preconditions/malformed-input rejections, and the
``status`` surface) which are also covered here but intentionally do not
carry an a-k letter.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodev import human_feedback as hf
from autodev import overrides_api as ov
from autodev import ralph
from autodev.artifacts.verdict import load_verdict, write_verdict
from autodev.cli import main as cli_main
from autodev.errors import GatePending, SchemaError
from autodev.orchestrator import Orchestrator, OrchestratorConfig
from autodev.state.atomic import atomic_write_json
from autodev.state.cascade import StalenessCascade
from autodev.state.log import JsonlLog
from autodev.vendors.config import (
    PanelConfig,
    PanelReviewerSpec,
    PanelSynthesizerSpec,
    ProbeConfig,
    STAGES,
    StageSpec,
    VendorsConfig,
)

from tests.v2.test_arch_design_loop_orchestrator import (
    _commit,
    _count_events,
    _feature_dir,
    _log_events,
    _write_prd,
)
from tests.v2.test_cascade_full_chain import _seed_all_ten

FAKE_VENDOR = Path(__file__).resolve().parent / "fakes" / "fake_vendor_cli_auto.py"
FAKE_PANEL = Path(__file__).resolve().parent / "fakes" / "fake_panel_invoker.sh"


def _vendors_fake_everywhere(repo_root: Path) -> VendorsConfig:
    return VendorsConfig(
        path=repo_root / "vendors.yml",
        stages={
            s: StageSpec(stage=s, vendor="claude", model="fake-model", probe_interval_sec=30)
            for s in STAGES
        },
        panel=PanelConfig(
            reviewers=(
                PanelReviewerSpec(vendor="claude", model="fake-panel-claude"),
                PanelReviewerSpec(vendor="agy", model="fake-panel-agy"),
                PanelReviewerSpec(vendor="codex", model="fake-panel-codex"),
            ),
            synthesizer=PanelSynthesizerSpec(vendor="claude", model="fake-panel-synth"),
        ),
        probe=ProbeConfig(vendor="claude", model="fake-probe"),
    )


def _wire_fakes(monkeypatch, feature: str, *, behavior: str = "reviewers_all_pass") -> None:
    monkeypatch.setenv("AUTODEV_VENDOR_BIN_CLAUDE", str(FAKE_VENDOR))
    monkeypatch.setenv("AUTODEV_FAKE_FEATURE", feature)
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_INVOKER", str(FAKE_PANEL))
    monkeypatch.setenv("AUTODEV_PANEL_FAKE_BEHAVIOR", behavior)


def _drive_to_design_packet_ready(git_repo, feature: str, monkeypatch) -> tuple[Path, Orchestrator]:
    """Seed a PRD and drive the fake pipeline through arch-design/
    arch-review (pass) and design/scope/trace/test_plan up to (and
    including) design-packet.json, with the panel wired to pass
    everything -- stopping right before the design-review gate runs."""
    active = _feature_dir(git_repo, feature)
    _write_prd(active)
    (git_repo / "docs" / "architecture-proposal.md").write_text(
        "# Architecture Proposal\n", encoding="utf-8",
    )
    (active / "architecture.md").write_text(
        "# Architecture Input\n\n- `docs/architecture-proposal.md`\n",
        encoding="utf-8",
    )
    _commit(git_repo, f"seed prd {feature}")

    _wire_fakes(monkeypatch, feature, behavior="reviewers_all_pass")
    ov.record_acknowledge_dirty(active, reason="test setup", who="pytest")

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id=f"seed-{feature}",
    ))
    # 1) arch_design + arch_review (compound single call, passes).
    # 2) design (+ scope/trace/test_plan, single manifest-driven call).
    # 3) design_packet (harness-authored artifact).
    for _ in range(3):
        result = orch.advance_one(feature)
        assert result.success, result.detail

    assert (active / "design-packet.json").exists()
    assert not (active / "panel-design-review.json").exists()
    return active, orch


def _pending_design_review_payload(*, blocking: bool) -> dict:
    if blocking:
        return {
            "verdict": "needs_revision",
            "findings": [{
                "severity": "invariant_violation",
                "summary": "human: contradicts a hard requirement",
                "priority": "P0",
                "targets": ["primary_pair.design.md"],
            }],
        }
    return {
        "verdict": "needs_revision",
        "findings": [{
            "severity": "opinion",
            "summary": "human: naming nit",
            "priority": "P2",
            "targets": ["primary_pair.design.md"],
        }],
    }


def _write_pending(active: Path, point: str, payload: dict) -> hf.HumanFeedback:
    fb = hf.validate_feedback(active, point, payload)
    hf.write_feedback(active, fb)
    return fb


def _blocking_close_approval_payload() -> dict:
    # close-approval has no fallback producer for indeterminate targets
    # (revision_state.py: GATE_FALLBACK_PRODUCER omits close-approval) --
    # an untargeted blocking finding halts for human rather than routing.
    # Target design.md, which close-approval's own filename map routes to
    # "arch-design" -- the shortest real rerun path (no build/ralph loop
    # involved, so it does not depend on scope.json's scope id matching
    # what the ralph-review fake hard-codes).
    return {
        "verdict": "needs_revision",
        "findings": [{
            "severity": "risk", "summary": "PII leak in export path",
            "priority": "P1", "targets": ["primary_pair.design.md"],
        }],
    }


# =======================================================================
# (a) pending feedback merged by the hook on a fake design-review run --
# no extra panel-start/reviewer dispatch vs. baseline, and the NEXT round
# (after the forced design revision) does not carry the human finding
# forward (one consumption). FIRST-round variant (no design-round-outcome
# event exists yet when the pending feedback is written).
# =======================================================================


def test_a_pending_feedback_merged_by_hook_no_extra_dispatch(git_repo, monkeypatch):
    feature = "hf-a"
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)
    logger = JsonlLog(active / "log.jsonl")

    _write_pending(active, "design-review", _pending_design_review_payload(blocking=True))

    panel_starts_before = _count_events(active, "gate", "panel-start")
    result = orch.advance_one(feature)
    assert result.success

    # Exactly one panel-start for this round -- the hook merges into the
    # verdict the round already produced, it does not cause a second
    # reviewer/synthesizer dispatch.
    assert _count_events(active, "gate", "panel-start") == panel_starts_before + 1

    v = load_verdict(active / "panel-design-review.json")
    assert any(f.vendor == "human" for f in v.findings)
    assert v.verdict == "needs_revision"

    fb_after = hf.load_feedback(active, "design-review")
    assert fb_after.status == "consumed"

    # Blocking human finding routed straight to a design revision.
    # design.md's finding-target maps to the "arch-design" producer
    # (revision_state.GATE_FILENAME_TO_PRODUCER), which forces a fresh
    # arch-design/arch-review/design/design_packet chain -- core R5's
    # "design 重跑" is this arch-design-first chain in this codebase.
    triggered = [
        r for r in _log_events(active) if r.get("event") == "revision-loop-triggered"
    ]
    assert triggered
    assert triggered[-1]["detail"]["stage_to_rerun"] == "arch-design"

    # Drive the loop forward: the fake arch-design output is byte-
    # identical every time (it always writes the same fixed body), so
    # the packet's hash never actually changes here -- the design-review
    # gate is simply re-entered on the SAME packet because the round
    # pairing was not satisfied (the merged round recorded "not passed").
    # Either way, the NEXT design-review round's verdict must not carry
    # the (already-consumed) human finding forward.
    for _ in range(3):
        r = orch.advance_one(feature)
        assert r.success, r.detail

    v2 = load_verdict(active / "panel-design-review.json")
    assert not any(f.vendor == "human" for f in v2.findings)


# =======================================================================
# (a), NON-first-round variant: the pending feedback is written only
# after a first (coverage) round has already passed cleanly -- log.jsonl
# already carries a design-round-outcome event by the time the hook
# merges the feedback into round 2's (budget) verdict.
# =======================================================================


def test_a_pending_feedback_merged_by_hook_non_first_round(git_repo, monkeypatch):
    feature = "hf-a2"
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)

    r1 = orch.advance_one(feature)  # round 1 (coverage), passes, no feedback yet
    assert r1.success, r1.detail
    outcomes_before = _count_events(active, "gate", "design-round-outcome")
    assert outcomes_before >= 1  # confirms round 2 below is NOT the first round

    _write_pending(active, "design-review", _pending_design_review_payload(blocking=True))

    panel_starts_before = _count_events(active, "gate", "panel-start")
    r2 = orch.advance_one(feature)  # round 2 (budget); hook merges at timing B
    assert r2.success, r2.detail

    assert _count_events(active, "gate", "panel-start") == panel_starts_before + 1
    v = load_verdict(active / "panel-design-review.json")
    assert any(f.vendor == "human" for f in v.findings)
    assert hf.load_feedback(active, "design-review").status == "consumed"

    triggered = [
        r for r in _log_events(active) if r.get("event") == "revision-loop-triggered"
    ]
    assert triggered
    assert triggered[-1]["detail"]["stage_to_rerun"] == "arch-design"


# =======================================================================
# (b) close-approval verdict produced -> pause -> verb merges -> resume
# routes with no new panel-start.
# =======================================================================


def test_b_close_approval_pause_verb_merge_resume_routes_no_new_panel_start(
    feature_active, monkeypatch,
):
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name

    # pause
    (active / ".pause").write_text("paused\n", encoding="utf-8")

    payload = _blocking_close_approval_payload()
    exit_code = cli_main([
        "feedback", feature, "close-approval", "--text", json.dumps(payload),
        "--repo-root", str(active.parent.parent.parent.parent),
    ])
    assert exit_code == 0

    fb = hf.load_feedback(active, "close-approval")
    assert fb.status == "consumed"  # merged at once: panel_close_approval was fresh

    # resume -- driven through a REAL Orchestrator's advance_one (not a
    # bare object.__new__ instance calling the private routing helper
    # directly), so "no new panel-start" is a meaningful end-to-end
    # assertion about the actual `autodev next` path, not just about the
    # routing-decision function in isolation.
    (active / ".pause").unlink()
    git_repo = active.parent.parent.parent.parent
    _wire_fakes(monkeypatch, feature)

    panel_starts_before = _count_events(active, "gate", "panel-start")
    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id=feature,
    ))
    result = orch.advance_one(feature)  # `autodev next`'s real entry point
    assert result.success, result.detail

    # No panel dispatch was needed to discover/route the (already merged)
    # blocking verdict -- it routed straight to an arch-design rerun.
    assert _count_events(active, "gate", "panel-start") == panel_starts_before
    triggered = [
        r for r in _log_events(active) if r.get("event") == "revision-loop-triggered"
    ]
    assert triggered
    assert triggered[-1]["detail"]["stage_to_rerun"] == "arch-design"


# =======================================================================
# (c) design-review coverage-only round -> pending; merged once the
# budget round completes the packet (real fake dispatch, both rounds).
# =======================================================================


def test_c_coverage_only_pending_merged_after_budget_round(git_repo, monkeypatch):
    feature = "hf-c"
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)

    # Round 1: coverage. Passes (all-pass fake).
    r1 = orch.advance_one(feature)
    assert r1.success, r1.detail

    assert StalenessCascade(active).fresh().get("panel_design_review") is False

    _write_pending(active, "design-review", _pending_design_review_payload(blocking=False))
    _write_pending(active, "trace-review", _pending_design_review_payload(blocking=False))

    # Not current yet (budget round outstanding) -- stays pending. Assert
    # this through apply_pending(check_current=True) itself (the same
    # call the verb/loop-top entry A' make), not just via load_pending,
    # so the "stays pending because not current" reason is exercised
    # directly rather than inferred.
    logger = JsonlLog(active / "log.jsonl")
    assert hf.apply_pending(
        active, "design-review", log=logger, check_current=True,
    ) is False
    assert hf.apply_pending(
        active, "trace-review", log=logger, check_current=True,
    ) is False
    assert hf.load_pending(active, "design-review") is not None
    assert hf.load_pending(active, "trace-review") is not None

    # Round 2: budget. Passes -- this completes the packet's pairing, and
    # the hook (timing B, unconditional) merges both pending feedbacks
    # into the round's verdict files.
    r2 = orch.advance_one(feature)
    assert r2.success, r2.detail

    assert hf.load_pending(active, "design-review") is None
    assert hf.load_pending(active, "trace-review") is None
    v_design = load_verdict(active / "panel-design-review.json")
    v_trace = load_verdict(active / "panel-trace-review.json")
    assert any(f.vendor == "human" for f in v_design.findings)
    assert any(f.vendor == "human" for f in v_trace.findings)


# =======================================================================
# (d) both rounds already passed (fully-seeded, "current") -> a blocking
# design-review injection is merged at once and expires accepted-design.
# =======================================================================


def test_d_both_rounds_done_inject_expires_accepted_design(feature_active, monkeypatch):
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name
    logger = JsonlLog(active / "log.jsonl")

    from autodev.artifacts.design_packet import accepted_design_fresh
    assert accepted_design_fresh(active / "accepted-design.json") is True

    _write_pending(active, "design-review", _pending_design_review_payload(blocking=True))
    merged = hf.apply_pending(active, "design-review", log=logger, check_current=True)
    assert merged is True

    assert accepted_design_fresh(active / "accepted-design.json") is False

    # Drive a REAL Orchestrator.advance_one and confirm the merge that just
    # happened above (via apply_pending, i.e. entry A'/verb timing, on an
    # already-fully-current pipeline) is what routes the revision loop --
    # NOT a fresh panel re-run. The merge came from the fresh verdict
    # already on disk, so no reviewer/synthesizer dispatch should occur.
    git_repo = active.parent.parent.parent.parent
    _wire_fakes(monkeypatch, feature)

    panel_starts_before = _count_events(active, "gate", "panel-start")
    panel_dones_before = _count_events(active, "gate", "panel-done")
    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id=feature,
    ))
    result = orch.advance_one(feature)  # `autodev next`'s real entry point
    assert result.success, result.detail

    assert _count_events(active, "gate", "panel-start") == panel_starts_before
    # No new panel-done either -- the round was neither rerun nor
    # resynthesized; this file's routing came from a plain re-read of
    # the already-merged verdict on disk.
    assert _count_events(active, "gate", "panel-done") == panel_dones_before
    triggered = [
        r for r in _log_events(active) if r.get("event") == "revision-loop-triggered"
    ]
    assert triggered
    assert triggered[-1]["detail"]["stage_to_rerun"] == "arch-design"
    # Pins the routing to _enforce_pending_blocking_verdicts's own
    # "pending-blocking-verdict" emit (detail: the merge above already
    # landed a fresh blocking verdict on disk BEFORE advance_one ran, so
    # this call's routing decision must come from that cold read, not
    # from _advance_gate re-running/re-caching the panel and emitting
    # its own revision-loop-triggered event, whose detail carries no
    # "source" key at all).
    assert triggered[-1]["detail"]["source"] == "pending-blocking-verdict"


# =======================================================================
# (e) pause mid-panel on the FIRST design-review round -> resume ->
# merged once via entry A' (loop top) and routed.
# =======================================================================


def test_e_pause_mid_panel_first_round_resume_merges_via_loop_top(git_repo, monkeypatch):
    feature = "hf-e"
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)

    _write_pending(active, "design-review", _pending_design_review_payload(blocking=True))

    # Simulate ".pause" being written WHILE the panel runs (detail §2.2):
    # wrap run_panel_gate so its result lands on disk as usual, then the
    # pause sentinel appears before _advance_gate's own pause checkpoint
    # (which runs immediately after run_panel_gate returns).
    import autodev.orchestrator as orch_mod
    real_run_panel_gate = orch_mod.run_panel_gate

    def _run_panel_gate_then_pause(**kwargs):
        v = real_run_panel_gate(**kwargs)
        (active / ".pause").write_text("paused\n", encoding="utf-8")
        return v

    monkeypatch.setattr(orch_mod, "run_panel_gate", _run_panel_gate_then_pause)

    with pytest.raises(GatePending) as excinfo:
        orch.advance_one(feature)
    assert excinfo.value.gate == "pause"

    # No round-outcome event yet (first round, pause hit before it) --
    # this is the FIRST-round case: the hook in _advance_gate never ran,
    # so the feedback is still pending, waiting for entry A' at the next
    # loop top.
    assert hf.load_pending(active, "design-review") is not None
    v = load_verdict(active / "panel-design-review.json")
    assert not any(f.vendor == "human" for f in v.findings)

    # resume
    (active / ".pause").unlink()
    monkeypatch.setattr(orch_mod, "run_panel_gate", real_run_panel_gate)

    panel_starts_before = _count_events(active, "gate", "panel-start")
    panel_dones_before = _count_events(active, "gate", "panel-done")
    result = orch.advance_one(feature)
    assert result.success

    # The merge happened via entry A' (loop-top apply_pending), NOT a
    # panel re-run: no new panel-start/panel-done for this call.
    assert _count_events(active, "gate", "panel-start") == panel_starts_before
    assert _count_events(active, "gate", "panel-done") == panel_dones_before

    assert hf.load_pending(active, "design-review") is None
    v2 = load_verdict(active / "panel-design-review.json")
    human = [f for f in v2.findings if f.vendor == "human"]
    assert len(human) == 1
    triggered = [
        r for r in _log_events(active) if r.get("event") == "revision-loop-triggered"
    ]
    assert triggered


# =======================================================================
# (e), NON-first-round variant: pause hits mid-panel on a round that is
# NOT the first (log already carries a design-round-outcome event from a
# prior passing round). Existing (non-injection-related) resume behavior
# re-runs that round's synthesizer, so the hook (timing B, unconditional)
# merges once the re-run round's verdict lands -- not entry A'.
# =======================================================================


def test_e_pause_mid_panel_non_first_round_resume_merges_via_hook(git_repo, monkeypatch):
    feature = "hf-e2"
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)

    r1 = orch.advance_one(feature)  # round 1 (coverage), passes, no feedback yet
    assert r1.success, r1.detail
    assert _count_events(active, "gate", "design-round-outcome") >= 1

    _write_pending(active, "design-review", _pending_design_review_payload(blocking=True))

    import autodev.orchestrator as orch_mod
    real_run_panel_gate = orch_mod.run_panel_gate

    def _run_panel_gate_then_pause(**kwargs):
        v = real_run_panel_gate(**kwargs)
        (active / ".pause").write_text("paused\n", encoding="utf-8")
        return v

    monkeypatch.setattr(orch_mod, "run_panel_gate", _run_panel_gate_then_pause)

    with pytest.raises(GatePending) as excinfo:
        orch.advance_one(feature)  # round 2 (budget), interrupted mid-panel
    assert excinfo.value.gate == "pause"

    # Round 2's verdict was written to disk before the pause hit, but the
    # checkpoint precedes the hook -- feedback stays pending.
    assert hf.load_pending(active, "design-review") is not None

    # resume
    (active / ".pause").unlink()
    monkeypatch.setattr(orch_mod, "run_panel_gate", real_run_panel_gate)

    result = orch.advance_one(feature)
    assert result.success, result.detail

    assert hf.load_pending(active, "design-review") is None
    v2 = load_verdict(active / "panel-design-review.json")
    human = [f for f in v2.findings if f.vendor == "human"]
    assert len(human) == 1
    triggered = [
        r for r in _log_events(active) if r.get("event") == "revision-loop-triggered"
    ]
    assert triggered


# =======================================================================
# (f) close-approval pending BEFORE the panel ran -> resume routes via
# loop-top entry A' (no new panel-start).
# =======================================================================


def test_f_close_approval_pending_before_panel_resume_routes_via_loop_top(feature_active):
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name
    logger = JsonlLog(active / "log.jsonl")

    _write_pending(active, "close-approval", _blocking_close_approval_payload())

    panel_starts_before = _count_events(active, "gate", "panel-start")
    from autodev.orchestrator import Orchestrator as _Orch
    result = _Orch._enforce_pending_blocking_verdicts(
        object.__new__(_Orch), active, logger, feature,
    )
    assert result is not None
    assert result.stage_to_rerun == "arch-design"
    assert _count_events(active, "gate", "panel-start") == panel_starts_before
    assert hf.load_feedback(active, "close-approval").status == "consumed"


# =======================================================================
# (g) `autodev next` variant of (f).
# =======================================================================


def test_g_next_variant_of_close_approval_loop_top_merge(git_repo, monkeypatch):
    feature = "hf-g"
    active = git_repo / "docs" / "features" / feature / "active"
    active.mkdir(parents=True)
    _seed_all_ten(active)
    _wire_fakes(monkeypatch, feature)
    ov.record_acknowledge_dirty(active, reason="test setup", who="pytest")

    _write_pending(active, "close-approval", _blocking_close_approval_payload())

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="hf-g",
    ))
    panel_starts_before = _count_events(active, "gate", "panel-start")
    result = orch.advance_one(feature)  # `autodev next`'s entry point
    assert result.success
    assert _count_events(active, "gate", "panel-start") == panel_starts_before
    assert hf.load_feedback(active, "close-approval").status == "consumed"


# =======================================================================
# arch-review family (detail §7's separate "arch-review" bullet -- NOT
# part of the (a)-(k) two-timing matrix, so this test does not carry an
# a-k letter): pass + verb inject -> verdict flips -> next run dispatches
# arch-design with arch-review.json in CONTEXT_ARTIFACTS.
# =======================================================================


def test_arch_review_inject_flips_verdict_and_next_run_dispatches_arch_design(
    git_repo, monkeypatch,
):
    feature = "hf-arch"
    active = _feature_dir(git_repo, feature)
    _write_prd(active)
    _commit(git_repo, "seed prd")
    _wire_fakes(monkeypatch, feature)

    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id="hf-arch",
    ))
    result = orch.advance_one(feature)
    assert result.success
    review = json.loads((active / "arch-review.json").read_text())
    assert review["verdict"] == "pass"

    _write_pending(active, "arch-review", {
        "verdict": "needs_revision",
        "findings": [{
            "category": "invented", "prd_ref": None,
            "evidence": "arch-design.md:5", "problem": "unrequested cache layer",
            "correction": "remove it",
        }],
    })

    logger = JsonlLog(active / "log.jsonl")
    merged = hf.apply_pending(active, "arch-review", log=logger, check_current=True)
    assert merged is True
    review2 = json.loads((active / "arch-review.json").read_text())
    assert review2["verdict"] == "needs_revision"

    import autodev.prompts_loader as prompts_loader
    real_render = prompts_loader.render_stage_prompt
    arch_design_calls: list[dict] = []

    def _spy(*, stage, **kwargs):
        if stage == "arch-design":
            arch_design_calls.append(
                {"context_artifacts": list(kwargs.get("context_artifacts") or [])}
            )
        return real_render(stage=stage, **kwargs)

    monkeypatch.setattr(prompts_loader, "render_stage_prompt", _spy)

    result2 = orch.advance_one(feature)
    assert result2.success
    assert arch_design_calls
    assert str(active / "arch-review.json") in arch_design_calls[-1]["context_artifacts"]


# =======================================================================
# ralph-review family (detail §7's separate "ralph-review" bullet -- NOT
# part of the (a)-(k) two-timing matrix, so this test does not carry an
# a-k letter): pending merged at the hook -> scope not complete -> next
# build sees it.
# =======================================================================


def test_ralph_pending_merged_at_hook_scope_not_complete(git_repo, monkeypatch):
    feature = "hf-ralph"
    # Drive the REAL fake pipeline from scratch (rather than
    # _seed_all_ten + roll-back) so scope.json's scope id is whatever
    # the design-stage fake naturally writes ("t-1", hard-coded in both
    # its scope.json AND ralph-review.json output) -- keeping the two
    # fakes' outputs mutually consistent instead of hand-editing
    # scope.json (which would desync design-packet.json's recorded
    # scope.json hash and force an unwanted design_packet regen).
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)

    # Drive through design-review (coverage + budget rounds) and
    # accepted-design -- right up to (but not including) the build/ralph
    # loop, which is a single `advance_one` call that internally loops
    # build->ralph-review iterations to convergence.
    for _ in range(3):
        result = orch.advance_one(feature)
        assert result.success, result.detail
        if result.stage_name == "accepted_design":
            break
    assert (active / "accepted-design.json").exists()
    assert not (active / "build.json").exists()

    _write_pending(active, "ralph-review", {
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["t-1"], "design_ref": "design.md:1",
            "evidence": "src/x.py:1", "difference": "missing retry",
            "correction": "add retry",
        }],
    })

    # Spy on the "build" stage's rendered CONTEXT_ARTIFACTS, snapshotting
    # ralph-review.json's content at the moment each build round's
    # prompt is built -- the harness loops build->ralph-review to
    # convergence within a single advance_one() call (scope "t-1" isn't
    # complete after the merge, so a second build round runs
    # automatically and its context construction is exactly where core
    # completion-criterion #3 requires the human-merged ralph-review.json
    # to appear as CONTEXT_ARTIFACTS -- by the time the SECOND round's
    # own ralph-review dispatch overwrites the file, that snapshot is
    # long gone from the final on-disk state).
    import autodev.prompts_loader as prompts_loader
    real_render = prompts_loader.render_stage_prompt
    build_calls: list[dict] = []

    def _spy(*, stage, **kwargs):
        if stage == "build":
            rr_path = active / "ralph-review.json"
            snapshot = (
                json.loads(rr_path.read_text()) if rr_path.exists() else None
            )
            build_calls.append({
                "context_artifacts": list(kwargs.get("context_artifacts") or []),
                "ralph_review_snapshot": snapshot,
            })
        return real_render(stage=stage, **kwargs)

    monkeypatch.setattr(prompts_loader, "render_stage_prompt", _spy)

    result = orch.advance_one(feature)  # the whole build/ralph-review loop
    assert result.success

    assert hf.load_feedback(active, "ralph-review").status == "consumed"
    # Each build round renders TWO prompts (prompt + resume_prompt), and
    # ralph-review.json only becomes context once a first ralph-review
    # has actually run -- so the first call(s) with it present are from
    # the SECOND build round.
    with_ralph_review = [
        c for c in build_calls
        if str(active / "ralph-review.json") in c["context_artifacts"]
    ]
    assert with_ralph_review, (
        "expected the ralph loop to need a second build round after the "
        "merged human finding kept scope 't-1' incomplete, and for that "
        "round's CONTEXT_ARTIFACTS to include ralph-review.json"
    )
    human = [
        f for f in with_ralph_review[0]["ralph_review_snapshot"]
        ["design_conformance"]["findings"]
        if f.get("vendor") == "human"
    ]
    assert len(human) == 1

    from autodev import ralph
    state = ralph.load_ralph_state(active)
    # fully_history[0] is the pre-iter-1 baseline; fully_history[1] is
    # iter 1 (the merged round), which must show "t-1" as NOT fully
    # covered -- the merge is not overwritten by construction, only the
    # LATER (already-consumed-feedback) round completes it.
    assert "t-1" not in state.fully_history[1]


# =======================================================================
# ralph "at rest" recompute (fix 3, detail §10 closing-review pin,
# unlettered per §7): apply_pending(check_current=True) on an
# ALREADY-RECORDED ralph round (ralph-state.iter >= 1, iteration-context
# iteration == state.iter, pipeline not yet fully done) must recompute
# ralph-state.json's last-round statuses/fully/regressions, not just
# rewrite ralph-review.json on disk.
# =======================================================================


def test_ralph_at_rest_recompute_caps_completed_round_and_reopens_build(
    feature_active,
):
    """Two ralph rounds are already recorded, both fully covering the
    sole active scope item "s-1" -- so BEFORE injection the ralph loop
    reads as complete and the harness is poised to move on to
    close-approval. A human feedback capping "s-1" back to Deviated is
    injected at rest (check_current=True, matching the verb / loop-top
    entry, NOT the producer hook) against this already-recorded round.

    Two rounds (not one) are seeded so a genuine Fully -> Deviated
    regression has a real predecessor to regress from: with only one
    recorded round, the "prior" state _recompute_ralph_state rebuilds
    from is the empty pre-iter-1 baseline, where "s-1" was never
    classified Fully in the first place, so no regression could ever be
    observed -- that would exercise the merge but not prove the
    regression-recompute rule.

    Why this fails without ``_recompute_ralph_state``: ``_ralph_loop_complete``
    determines completeness from ``ralph-state.json``'s OWN
    ``fully_history[-1]`` (via ``ralph.is_complete``), not from a fresh
    re-parse of ``ralph-review.json``. If the merge only rewrote
    ralph-review.json and never recomputed ralph-state.json, that stale
    ``fully_history[-1]`` would still contain "s-1" post-merge, so
    ``_ralph_loop_complete`` would still return True and
    ``_next_stage_name`` would still return "panel_close_approval" --
    exactly the two assertions this test makes on the OTHER side of the
    merge, below."""
    _seed_all_ten(feature_active)
    active = feature_active
    repo_root = active.parent.parent.parent.parent

    # build.json / implementation-index / implemented-spec / prd-checklist
    # stay in place (build already landed); only close-approval is
    # dropped, so the pipeline is not fully done and cascade.next_stage()
    # lands on "panel_close_approval" -- a _POST_BUILD_STAGES member.
    (active / "panel-close-approval.json").unlink()

    atomic_write_json(active / "ralph-review.json", {
        "classifications": [
            {"req_id": "s-1.r1", "scope_id": "s-1", "classification": "Fully"},
        ],
        "design_conformance": {"verdict": "Aligned", "findings": []},
    })
    state = ralph.RalphState(
        iter=2,
        fully_history=[set(), {"s-1"}, {"s-1"}],
        statuses_history=[{}, {"s-1": "Fully"}, {"s-1": "Fully"}],
    )
    ralph.write_ralph_state(active, state)
    atomic_write_json(active / "ralph-iteration-context.json", {
        "schema": 1, "iteration": 2, "status": "ready_for_review",
    })

    orch = Orchestrator(OrchestratorConfig(
        repo_root=repo_root, vendors=_vendors_fake_everywhere(repo_root),
        session_id="ralph-at-rest-recompute",
    ))

    # Preconditions: an already-recorded round that _is_current accepts,
    # a loop that reads as complete, and a pipeline poised for
    # close-approval next -- all BEFORE the human finding is merged.
    assert hf._ralph_review_current(active) == (True, None)
    assert orch._ralph_loop_complete(active) is True
    assert orch._next_stage_name(active) == "panel_close_approval"

    fb = hf.validate_feedback(active, "ralph-review", {
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["s-1"], "design_ref": "design.md:1",
            "evidence": "src/x.py:1", "difference": "missing retry",
            "correction": "add retry",
        }],
    })
    hf.write_feedback(active, fb)

    logger = JsonlLog(active / "log.jsonl")
    merged = hf.apply_pending(
        active, "ralph-review", log=logger, check_current=True,
    )
    assert merged is True

    recomputed = ralph.load_ralph_state(active)
    assert recomputed.iter == 2  # unchanged -- capped in place, not a new round
    assert recomputed.statuses_history[-1] == {"s-1": "Deviated"}
    assert recomputed.fully_history[-1] == set()
    regs = [r for r in recomputed.regressions if r.iter_index == 2]
    assert len(regs) == 1
    assert regs[0].scope_id == "s-1"
    assert regs[0].prior_status == "Fully"
    assert regs[0].new_status == "Deviated"

    # The scope no longer reads as fully covered, so the loop is no
    # longer complete and the harness must re-open "build" rather than
    # proceeding to close-approval.
    assert orch._ralph_loop_complete(active) is False
    assert orch._next_stage_name(active) == "build"


# =======================================================================
# (h) cascade-stale verdict: arch-design is revised but design.md (and
# everything downstream of it) is not regenerated -> the design-review
# node goes stale even though panel-design-review.json's own recorded
# source_hash/consulted_docs still narrowly match design-packet.json --
# exactly the upstream case verdict_exists_and_valid does not see
# (detail §2.1 D9). apply_pending(check_current=True) must stay pending.
# =======================================================================


def test_h_cascade_stale_verdict_stays_pending(feature_active):
    _seed_all_ten(feature_active)
    active = feature_active
    logger = JsonlLog(active / "log.jsonl")

    assert StalenessCascade(active).fresh().get("panel_design_review") is True

    # arch-design revised but design.md (etc.) not regenerated.
    (active / "arch-design.md").write_text(
        "## 1. Goal\nrevised body\n## 5. PRD coverage\nbody\n", encoding="utf-8",
    )
    assert StalenessCascade(active).fresh().get("panel_design_review") is False

    _write_pending(active, "design-review", _pending_design_review_payload(blocking=True))
    merged = hf.apply_pending(active, "design-review", log=logger, check_current=True)
    assert merged is False
    assert hf.load_pending(active, "design-review") is not None


# =======================================================================
# (i) a pending ralph-review feedback whose scope_ids are no longer
# covered by the on-disk ralph-review.json's own classifications (as if
# a design re-run had moved the pipeline on since the human wrote the
# feedback) -> the SAME apply_pending(check_current=False) call
# `_run_ralph_review`'s hook makes marks it rejected, logs
# human-feedback-rejected, and leaves ralph-review.json byte-identical --
# the run is not halted. Reuses the exact scope/ralph-review mismatch
# setup as tests/v2/test_human_feedback.py's
# TestApplyPendingIdempotencyAndRejection
# .test_rejected_path_leaves_target_byte_identical, reproduced here
# inside a real (build-loop-open) pipeline state.
#
# NOTE on the "realistic" full-loop variant (mutate scope.json's active
# scope-id set, then drive the real build/ralph-review loop so a live
# `_run_ralph_review` hits the rejection): this is NOT practical with the
# existing fixtures and is intentionally NOT attempted here. scope.json's
# full content is hash-pinned by design-packet.json (StalenessCascade),
# so ANY edit to it -- including just flipping which scope id is active
# -- invalidates design_packet and everything downstream of it
# (panel_design_review, accepted_design, build, ...; see
# test_cascade_scope_mutation_invalidates_downstream_only in
# test_cascade_full_chain.py). Driving `advance_one`/`_run_ralph_review`
# afterwards would therefore force a full design-phase regeneration
# instead of exercising an isolated ralph-review-round merge rejection,
# which is a different (and much heavier) scenario than what this item
# is actually about. The direct-call variant below -- constructing the
# on-disk classification/scope mismatch by hand and calling
# `apply_pending(check_current=False)` exactly as the hook does -- is
# kept, with the rejected-reason assertion tightened per review.
# =======================================================================


def test_i_ralph_pending_scope_ids_no_longer_covered_hook_rejects(feature_active):
    active = feature_active
    _seed_all_ten(active)
    # Roll back to "next stage is build" so the ralph loop counts as
    # still open (mirrors tests/v2/test_human_feedback.py's
    # TestRalphReviewCurrentRule._seed_scope_and_state pattern).
    for name in (
        "build.json", "implementation-index.json", "implemented-spec.md",
        "prd-checklist.json", "panel-close-approval.json",
    ):
        (active / name).unlink()

    # scope.json (from _seed_all_ten) has one active scope, "s-1". The
    # on-disk ralph-review.json's classifications cover a DIFFERENT id
    # ("s-1-other") -- as if this round's review ran before "s-1" existed
    # in its current form -- so the feedback's own scope_ids validate
    # cleanly against scope.json at write time (core R5) but the merge
    # itself fails against the round's actual classifications.
    review_path = active / "ralph-review.json"
    atomic_write_json(review_path, {
        "classifications": [
            {"req_id": "s-1-other.r1", "scope_id": "s-1-other", "classification": "Fully"},
        ],
        "design_conformance": {"verdict": "Aligned", "findings": []},
    })
    ralph.write_ralph_state(active, ralph.RalphState(
        iter=1, fully_history=[set(), set()],
        statuses_history=[{}, {"s-1-other": "Fully"}],
    ))

    _write_pending(active, "ralph-review", {
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["s-1"], "design_ref": "design.md:1",
            "evidence": "src/x.py:1", "difference": "missing retry",
            "correction": "add retry",
        }],
    })
    # Confirms the failure below is the real merge-time rejection, not a
    # "not current -> stays pending" no-op.
    assert hf._ralph_review_current(active) == (True, None)

    before_bytes = review_path.read_bytes()
    logger = JsonlLog(active / "log.jsonl")
    merged = hf.apply_pending(active, "ralph-review", log=logger, check_current=False)
    assert merged is False

    assert review_path.read_bytes() == before_bytes  # run continues untouched

    fb = hf.load_feedback(active, "ralph-review")
    assert fb.status == "rejected"
    assert fb.rejected_reason is not None
    assert "not present in classifications" in fb.rejected_reason
    assert "s-1" in fb.rejected_reason

    events = _log_events(active)
    assert any(e.get("event") == "human-feedback-rejected" for e in events)

    # Downstream consumption (what _run_ralph_review does right after
    # its hook call) proceeds normally on the untouched file.
    assert ralph.parse_review_statuses(review_path) == {"s-1-other": "Fully"}


# =======================================================================
# (i), live-hook variant: the SAME rejection-on-merge-failure path, but
# reached through the REAL _run_ralph_review hook (timing B,
# check_current=False) during an actual build/ralph loop, by forcing
# merge_into_ralph_review itself to raise -- rather than constructing an
# on-disk scope/classification mismatch by hand -- to prove apply_pending's
# SchemaError catch (and the hook's use of it) tolerates an ARBITRARY
# merge failure, not just the one specific mismatch shape exercised
# above.
# =======================================================================


def test_i_ralph_pending_live_hook_merge_failure_rejects_run_continues(
    git_repo, monkeypatch,
):
    feature = "hf-i-live"
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)

    for _ in range(3):
        result = orch.advance_one(feature)
        assert result.success, result.detail
        if result.stage_name == "accepted_design":
            break
    assert (active / "accepted-design.json").exists()
    assert not (active / "build.json").exists()

    _write_pending(active, "ralph-review", {
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["t-1"], "design_ref": "design.md:1",
            "evidence": "src/x.py:1", "difference": "missing retry",
            "correction": "add retry",
        }],
    })

    def _forced_merge(*args, **kwargs):
        raise SchemaError("forced")

    # apply_pending's `merge_into_ralph_review(...)` call resolves this
    # name against autodev.human_feedback's own module globals at call
    # time (late binding) -- patching the module attribute here (the
    # same module object `hf` already is) intercepts it exactly like
    # test_e's `monkeypatch.setattr(orch_mod, "run_panel_gate", ...)`
    # intercepts a same-module direct call.
    monkeypatch.setattr(hf, "merge_into_ralph_review", _forced_merge)

    result = orch.advance_one(feature)  # the whole build/ralph-review loop
    assert result.success, result.detail  # the run does not raise

    fb = hf.load_feedback(active, "ralph-review")
    assert fb.status == "rejected"
    assert fb.rejected_reason == "forced"

    events = _log_events(active)
    assert any(e.get("event") == "human-feedback-rejected" for e in events)

    # ralph-review.json is untouched by any merge attempt -- byte-
    # identical to fake_vendor_cli_auto.py's tgt_ralph_review branch,
    # which is a fixed literal with no feature-/hash-dependent content
    # (unlike every other artifact this fake writes), so this is a
    # direct byte-for-byte comparison rather than a re-parse.
    expected = (
        '{\n'
        '  "classifications": [\n'
        '    {"req_id": "t-1.r1", "scope_id": "t-1", "classification": '
        '"Fully", "evidence": "fake_vendor output"}\n'
        '  ],\n'
        '  "summary": {"Fully": 1, "Partial": 0, "Missing": 0, "Deviated": 0, '
        '"Deferred": 0},\n'
        '  "design_conformance": {"verdict": "Aligned", "findings": []}\n'
        '}\n'
    )
    assert (
        active / "ralph-review.json"
    ).read_text(encoding="utf-8") == expected


# =======================================================================
# (j) two sequential feedbacks merged into the SAME current
# close-approval verdict -> both present (human:1, human:2) with two
# distinct per_vendor_raw keys.
# =======================================================================


def test_j_two_sequential_feedbacks_both_present_distinct_per_vendor_raw(feature_active):
    _seed_all_ten(feature_active)
    active = feature_active
    logger = JsonlLog(active / "log.jsonl")

    def _write(payload: dict) -> str:
        fb = hf.validate_feedback(active, "close-approval", payload)
        hf.write_feedback(active, fb)
        return fb.feedback_id

    id1 = _write({
        "verdict": "needs_revision",
        "findings": [{"severity": "opinion", "summary": "first", "priority": "P2"}],
    })
    assert hf.apply_pending(
        active, "close-approval", log=logger, check_current=True,
    ) is True

    id2 = _write({
        "verdict": "needs_revision",
        "findings": [{"severity": "opinion", "summary": "second", "priority": "P2"}],
    })
    # feedback_id collisions (detail §10, closing-review pin): the two
    # feedbacks above were written back-to-back, easily within the same
    # wall-clock second, yet must not collide -- the 6-hex-char random
    # suffix is what guarantees this, not the timestamp alone.
    assert id1 != id2
    assert hf.apply_pending(
        active, "close-approval", log=logger, check_current=True,
    ) is True

    v = load_verdict(active / "panel-close-approval.json")
    human_ids = sorted(f.finding_id for f in v.findings if f.vendor == "human")
    assert human_ids == ["human:1", "human:2"]
    per_vendor_human_keys = [k for k in v.per_vendor_raw if k.startswith("human:")]
    assert len(per_vendor_human_keys) == 2


# =======================================================================
# one-time consumption (detail §7's separate, unlettered "一次消费"
# bullet -- NOT part of the (a)-(k) two-timing matrix): rerunning the
# SAME point does not re-merge.
# =======================================================================


def test_one_consumption_rerun_same_point_no_second_merge(feature_active):
    _seed_all_ten(feature_active)
    active = feature_active
    logger = JsonlLog(active / "log.jsonl")

    _write_pending(active, "close-approval", {
        "verdict": "needs_revision",
        "findings": [{"severity": "opinion", "summary": "nit", "priority": "P2"}],
    })
    assert hf.apply_pending(active, "close-approval", log=logger, check_current=True) is True
    v1 = load_verdict(active / "panel-close-approval.json")
    human_count_1 = sum(1 for f in v1.findings if f.vendor == "human")
    assert human_count_1 == 1

    # No pending feedback left -- a second call is a pure no-op.
    assert hf.apply_pending(active, "close-approval", log=logger, check_current=True) is False
    v2 = load_verdict(active / "panel-close-approval.json")
    human_count_2 = sum(1 for f in v2.findings if f.vendor == "human")
    assert human_count_2 == 1


# =======================================================================
# verb preconditions (detail §7's separate, unlettered "verb 前置" bullet
# -- NOT part of the (a)-(k) two-timing matrix): refusals exit 1 and
# write nothing.
# =======================================================================


def test_verb_refusals_exit_1_write_nothing(feature_active, monkeypatch):
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent
    payload = json.dumps({
        "verdict": "needs_revision",
        "findings": [{"severity": "risk", "summary": "x", "priority": "P1"}],
    })

    # unpaused
    code = cli_main([
        "feedback", feature, "close-approval", "--text", payload,
        "--repo-root", str(repo_root),
    ])
    assert code == 1
    assert not (active / "human-feedback-close-approval.json").exists()

    # unknown review point (still unpaused, but point-check comes first)
    code = cli_main([
        "feedback", feature, "nope", "--text", payload,
        "--repo-root", str(repo_root),
    ])
    assert code == 1
    assert not (active / "human-feedback-nope.json").exists()

    (active / ".pause").write_text("paused\n", encoding="utf-8")

    # malformed JSON
    code = cli_main([
        "feedback", feature, "close-approval", "--text", "{not json",
        "--repo-root", str(repo_root),
    ])
    assert code == 1
    assert not (active / "human-feedback-close-approval.json").exists()

    # live lock
    from autodev.state.lock import Lock
    with Lock(active, session_id="someone-else", verb="run"):
        code = cli_main([
            "feedback", feature, "close-approval", "--text", payload,
            "--repo-root", str(repo_root),
        ])
        assert code == 1
        assert not (active / "human-feedback-close-approval.json").exists()

    # now succeeds (paused, no lock, valid JSON)
    code = cli_main([
        "feedback", feature, "close-approval", "--text", payload,
        "--repo-root", str(repo_root),
    ])
    assert code == 0
    assert (active / "human-feedback-close-approval.json").exists()


def test_verb_schema_invalid_payload_exit_1_nothing_written(feature_active):
    """Well-formed JSON, but an invalid finding structure (unknown
    severity enum) -- validate_feedback's family-specific validator
    (reusing verdict._validate) rejects it, surfaced by the verb as
    exit 1 with nothing written (core R4, detail §3.1)."""
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent
    (active / ".pause").write_text("paused\n", encoding="utf-8")

    payload = json.dumps({
        "verdict": "needs_revision",
        "findings": [{"severity": "not-a-real-severity", "summary": "x"}],
    })
    code = cli_main([
        "feedback", feature, "close-approval", "--text", payload,
        "--repo-root", str(repo_root),
    ])
    assert code == 1
    assert not (active / "human-feedback-close-approval.json").exists()


@pytest.mark.parametrize("bad_payload", [
    # non-object finding item
    {"verdict": "needs_revision", "findings": ["not-an-object"]},
    # list-valued verdict (not a string, would raise TypeError deep in a
    # family validator's `in {...}` membership check if unguarded)
    {
        "verdict": ["needs_revision"],
        "findings": [{"severity": "risk", "summary": "x"}],
    },
    # non-dict cited_artifact_span
    {
        "verdict": "needs_revision",
        "findings": [{
            "severity": "risk", "summary": "x",
            "cited_artifact_span": ["not", "a", "dict"],
        }],
    },
], ids=["non_object_finding", "list_valued_verdict", "non_dict_cited_artifact_span"])
def test_verb_malformed_input_surfaces_as_schema_error_exit_1(
    feature_active, bad_payload,
):
    """The three malformed-input shapes detail §10 d10 calls out must
    surface as SchemaError (verb exit 1), not an uncaught TypeError/
    ValueError escaping from deep inside a merge function."""
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent
    (active / ".pause").write_text("paused\n", encoding="utf-8")

    code = cli_main([
        "feedback", feature, "close-approval", "--text", json.dumps(bad_payload),
        "--repo-root", str(repo_root),
    ])
    assert code == 1
    assert not (active / "human-feedback-close-approval.json").exists()


def test_verb_rejected_ralph_output_exit_1(feature_active):
    """The verb's new `rejected: <reason>` / exit-1 path (detail §10 d10
    B-period pin), reached via the SAME ralph stale-scope_ids setup as
    tests/v2/test_human_feedback.py's
    TestApplyPendingIdempotencyAndRejection
    .test_rejected_path_leaves_target_byte_identical and this file's
    test_i_ralph_pending_scope_ids_no_longer_covered_hook_rejects, but
    driven end-to-end through `autodev feedback` itself."""
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent
    _seed_all_ten(active)
    for name in (
        "build.json", "implementation-index.json", "implemented-spec.md",
        "prd-checklist.json", "panel-close-approval.json",
    ):
        (active / name).unlink()

    review_path = active / "ralph-review.json"
    atomic_write_json(review_path, {
        "classifications": [
            {"req_id": "s-1-other.r1", "scope_id": "s-1-other", "classification": "Fully"},
        ],
        "design_conformance": {"verdict": "Aligned", "findings": []},
    })
    ralph.write_ralph_state(active, ralph.RalphState(
        iter=1, fully_history=[set(), set()],
        statuses_history=[{}, {"s-1-other": "Fully"}],
    ))
    before_bytes = review_path.read_bytes()

    (active / ".pause").write_text("paused\n", encoding="utf-8")
    payload = json.dumps({
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["s-1"], "design_ref": "design.md:1",
            "evidence": "src/x.py:1", "difference": "missing retry",
            "correction": "add retry",
        }],
    })
    code = cli_main([
        "feedback", feature, "ralph-review", "--text", payload,
        "--repo-root", str(repo_root),
    ])
    assert code == 1

    assert review_path.read_bytes() == before_bytes
    fb = hf.load_feedback(active, "ralph-review")
    assert fb.status == "rejected"
    assert fb.rejected_reason is not None
    assert "not present in classifications" in fb.rejected_reason
    assert "s-1" in fb.rejected_reason


# =======================================================================
# (k) skip-gate override active -> injection stays pending, run does not
# crash.
# =======================================================================


def test_k_skip_gate_override_active_stays_pending(feature_active, monkeypatch):
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name
    ov.record_skip_gate(active, gate="close-approval", reason="testing")

    _write_pending(active, "close-approval", {
        "verdict": "needs_revision",
        "findings": [{"severity": "risk", "summary": "x", "priority": "P1"}],
    })
    logger = JsonlLog(active / "log.jsonl")
    merged = hf.apply_pending(active, "close-approval", log=logger, check_current=True)
    assert merged is False
    assert hf.load_pending(active, "close-approval") is not None

    # Drive a real Orchestrator.advance_one and confirm it neither raises
    # nor rewrites accepted-design.json even with the blocking human
    # feedback sitting pending behind the active skip-gate override: the
    # already-fresh pipeline (from _seed_all_ten) has nothing left to do
    # (StalenessCascade.next_stage() == "done"), and the pending feedback
    # must not force any rework.
    accepted_before = (active / "accepted-design.json").read_bytes()
    git_repo = active.parent.parent.parent.parent
    _wire_fakes(monkeypatch, feature)
    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id=feature,
    ))
    result = orch.advance_one(feature)
    assert result.success, result.detail
    assert (active / "accepted-design.json").read_bytes() == accepted_before


# =======================================================================
# (k), close-approval rule-4 variant restated for design-review: an
# active skip-gate override for the design-review GATE (not close-
# approval) must equally keep a pending BLOCKING design-review feedback
# from merging against the fully-current, real `pass` verdicts
# _seed_all_ten seeds -- rule 4 (has_active_skip_gate("design-review"))
# is the only thing keeping it pending here (the panel_design_review
# cascade node is fresh, load_verdict succeeds, and the verdict is a
# real "pass", not "skipped").
# =======================================================================


def test_k_skip_gate_override_design_review_rule4_stays_pending(
    feature_active, monkeypatch,
):
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name

    ov.record_skip_gate(active, gate="design-review", reason="testing")
    v = load_verdict(active / "panel-design-review.json")
    assert v.verdict == "pass"  # untouched -- a genuinely-current verdict

    _write_pending(active, "design-review", _pending_design_review_payload(blocking=True))
    logger = JsonlLog(active / "log.jsonl")
    merged = hf.apply_pending(active, "design-review", log=logger, check_current=True)
    assert merged is False
    assert hf.load_pending(active, "design-review") is not None

    # Same "does not crash" drive as the close-approval variant above: a
    # real Orchestrator.advance_one must neither raise nor rewrite
    # accepted-design.json.
    accepted_before = (active / "accepted-design.json").read_bytes()
    git_repo = active.parent.parent.parent.parent
    _wire_fakes(monkeypatch, feature)
    orch = Orchestrator(OrchestratorConfig(
        repo_root=git_repo, vendors=_vendors_fake_everywhere(git_repo),
        session_id=feature,
    ))
    result = orch.advance_one(feature)
    assert result.success, result.detail
    assert (active / "accepted-design.json").read_bytes() == accepted_before


# =======================================================================
# (k), design-review variant: a skip-gate override active for the
# design-review gate makes _advance_gate write SYNTHETIC "skipped"
# verdicts for both panel-design-review.json and panel-trace-review.json
# instead of running the panel; a pending BLOCKING design-review feedback
# must stay pending through that (verdict=="skipped" is never "current"),
# and driving far enough to cross accepted-design creation (which accepts
# a "skipped" verdict as non-blocking) must not raise even with the
# blocking feedback sitting pending.
# =======================================================================


def test_k_skip_gate_override_design_review_stays_pending(git_repo, monkeypatch):
    feature = "hf-k2"
    active, orch = _drive_to_design_packet_ready(git_repo, feature, monkeypatch)

    ov.record_skip_gate(active, gate="design-review", reason="testing")
    _write_pending(active, "design-review", _pending_design_review_payload(blocking=True))

    result = None
    for _ in range(3):
        result = orch.advance_one(feature)
        assert result.success, result.detail
        if result.stage_name == "accepted_design":
            break
    assert (active / "accepted-design.json").exists()

    assert hf.load_pending(active, "design-review") is not None

    from autodev.cli import _pending_feedback_message
    assert _pending_feedback_message(active, "design-review") == (
        "gate design-review is covered by a skip-gate override; "
        "feedback stays pending"
    )


# =======================================================================
# status surfaces pending feedback (detail §3.3).
# =======================================================================


def test_status_json_shows_human_feedback_block(feature_active, monkeypatch):
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent

    _write_pending(active, "close-approval", {
        "verdict": "needs_revision",
        "findings": [{"severity": "risk", "summary": "x", "priority": "P1"}],
    })

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main([
            "status", feature, "--json", "--repo-root", str(repo_root),
        ])
    assert code == 0
    report = json.loads(buf.getvalue())
    assert report["human_feedback"]["close-approval"]["status"] == "pending"
    assert report["human_feedback"]["close-approval"]["verdict"] == "needs_revision"
    assert report["human_feedback"]["close-approval"]["finding_count"] == 1
    assert report["requirement"] is False


def test_status_text_shows_written_timestamp(feature_active):
    """Text-mode status's feedback line must include the ``written``
    timestamp (core R8: "有/无、写入时间、Verdict"; detail §10,
    closing-review pin), not just status/verdict/finding_count."""
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent

    fb = _write_pending(active, "close-approval", {
        "verdict": "needs_revision",
        "findings": [{"severity": "risk", "summary": "x", "priority": "P1"}],
    })

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main(["status", feature, "--repo-root", str(repo_root)])
    assert code == 0
    out = buf.getvalue()
    assert (
        f"feedback: close-approval pending (needs_revision, 1 findings, "
        f"written {fb.written})"
    ) in out


# =======================================================================
# verb pending-message wording (detail §10 d10, B-period pin): two
# specific hints _pending_feedback_message gives beyond the generic
# "pending; will merge when <point> next produces its output" line.
# =======================================================================


def test_ralph_pending_message_when_pipeline_already_done(feature_active):
    """ralph-review feedback written once the whole pipeline is
    StalenessCascade-done: _ralph_review_current excludes "done" from its
    "not_done" stage set, so the merge stays pending and the verb must
    surface the specific "pipeline already done" hint (detail §2.1
    ralph-review bullet), not the generic pending message.

    ``_seed_all_ten`` alone leaves no ralph-review.json/ralph-state.json
    at all, so _ralph_review_current would already return False at its
    file-existence check -- the "done" guard would never be reached and
    this test would pass even if that guard were deleted. Seed a valid
    at-rest ralph-review.json (classifying the real active scope "s-1",
    Aligned), a ralph-state.json at iter=1, and a matching
    ralph-iteration-context.json (iteration == state.iter, i.e. the
    round is NOT superseded by a later build) so every OTHER precondition
    _ralph_review_current checks is satisfied and only the
    next_stage()=="done" check is what keeps the merge pending: without
    it, _ralph_review_current would return True, apply_pending would
    actually merge (scope.json's only active id, "s-1", is exactly what
    the feedback and the seeded classifications both name), and the
    feedback would end up consumed rather than pending, with a "merged"
    message instead of the "pipeline already done" one."""
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent

    atomic_write_json(active / "ralph-review.json", {
        "classifications": [
            {"req_id": "s-1.r1", "scope_id": "s-1", "classification": "Fully"},
        ],
        "design_conformance": {"verdict": "Aligned", "findings": []},
    })
    ralph.write_ralph_state(active, ralph.RalphState(
        iter=1, fully_history=[set(), {"s-1"}],
        statuses_history=[{}, {"s-1": "Fully"}],
    ))
    atomic_write_json(active / "ralph-iteration-context.json", {"iteration": 1})

    assert StalenessCascade(active).next_stage() == "done"
    assert hf._ralph_review_current(active) == (False, "pipeline-done")

    (active / ".pause").write_text("paused\n", encoding="utf-8")
    payload = json.dumps({
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["s-1"], "design_ref": "design.md:1",
            "evidence": "src/x.py:1", "difference": "missing retry",
            "correction": "add retry",
        }],
    })

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main([
            "feedback", feature, "ralph-review", "--text", payload,
            "--repo-root", str(repo_root),
        ])
    assert code == 0
    out = buf.getvalue()
    assert (
        "pipeline already done; choose close-approval or "
        "re-open with autodev update; feedback stays pending"
    ) in out
    assert hf.load_pending(active, "ralph-review") is not None


def test_close_approval_pending_message_when_skipped_verdict_no_override(
    feature_active,
):
    """A close-approval verdict that was written by the skip-gate path
    itself (verdict=="skipped") is never "current" via _panel_current's
    rule 3 alone (detail §2.1: skip_reason can be an empty string, so
    _panel_current/_pending_feedback_message key off verdict=="skipped"
    instead) -- with NO active skip-gate override record, so rule 4
    (has_active_skip_gate) plays no part and cannot be what is keeping
    this pending. If rule 3 were deleted, _panel_current would see a
    fresh, successfully-loaded, non-skip-gated verdict and return True,
    the feedback would merge at once, and both assertions below
    (pending / the skipped-verdict hint) would fail."""
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent

    v = load_verdict(active / "panel-close-approval.json")
    v.verdict = "skipped"
    write_verdict(active / "panel-close-approval.json", v)
    assert ov.load(active).has_active_skip_gate("close-approval") is False

    (active / ".pause").write_text("paused\n", encoding="utf-8")
    payload = json.dumps({
        "verdict": "needs_revision",
        "findings": [{"severity": "risk", "summary": "x", "priority": "P1"}],
    })

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main([
            "feedback", feature, "close-approval", "--text", payload,
            "--repo-root", str(repo_root),
        ])
    assert code == 0
    out = buf.getvalue()
    assert (
        "verdict for gate close-approval is skipped; feedback stays pending"
    ) in out
    assert hf.load_pending(active, "close-approval") is not None


def test_close_approval_pending_message_when_skip_gate_override_active(
    feature_active,
):
    """An active skip-gate override record for close-approval, with a
    genuine ``pass`` verdict at rest (verdict != "skipped") -- exercises
    _panel_current's rule 4 (has_active_skip_gate) alone; rule 3 plays no
    part since the on-disk verdict was never touched. If rule 4 were
    deleted, _panel_current would see a fresh, current, non-skipped
    ``pass`` verdict and return True, the feedback would merge at once,
    and both assertions below (pending / the skip-gate hint) would
    fail."""
    _seed_all_ten(feature_active)
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent

    ov.record_skip_gate(active, gate="close-approval", reason="testing")
    v = load_verdict(active / "panel-close-approval.json")
    assert v.verdict == "pass"  # untouched -- a genuinely-current verdict

    (active / ".pause").write_text("paused\n", encoding="utf-8")
    payload = json.dumps({
        "verdict": "needs_revision",
        "findings": [{"severity": "risk", "summary": "x", "priority": "P1"}],
    })

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main([
            "feedback", feature, "close-approval", "--text", payload,
            "--repo-root", str(repo_root),
        ])
    assert code == 0
    out = buf.getvalue()
    assert (
        "gate close-approval is covered by a skip-gate override; "
        "feedback stays pending"
    ) in out
    assert hf.load_pending(active, "close-approval") is not None


def test_status_json_shows_rejected_feedback_with_reason(feature_active):
    """status's human_feedback block must surface rejected_reason for a
    rejected feedback (detail §10 d10, B-period pin), driven end-to-end
    through the same ralph stale-scope_ids setup as
    test_verb_rejected_ralph_output_exit_1."""
    active = feature_active
    feature = active.parent.name
    repo_root = active.parent.parent.parent.parent
    _seed_all_ten(active)
    for name in (
        "build.json", "implementation-index.json", "implemented-spec.md",
        "prd-checklist.json", "panel-close-approval.json",
    ):
        (active / name).unlink()

    review_path = active / "ralph-review.json"
    atomic_write_json(review_path, {
        "classifications": [
            {"req_id": "s-1-other.r1", "scope_id": "s-1-other", "classification": "Fully"},
        ],
        "design_conformance": {"verdict": "Aligned", "findings": []},
    })
    ralph.write_ralph_state(active, ralph.RalphState(
        iter=1, fully_history=[set(), set()],
        statuses_history=[{}, {"s-1-other": "Fully"}],
    ))

    (active / ".pause").write_text("paused\n", encoding="utf-8")
    payload = json.dumps({
        "verdict": "Deviated",
        "findings": [{
            "scope_ids": ["s-1"], "design_ref": "design.md:1",
            "evidence": "src/x.py:1", "difference": "missing retry",
            "correction": "add retry",
        }],
    })
    code = cli_main([
        "feedback", feature, "ralph-review", "--text", payload,
        "--repo-root", str(repo_root),
    ])
    assert code == 1  # rejected -> verb exit 1 (test_verb_rejected_ralph_output_exit_1)

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main([
            "status", feature, "--json", "--repo-root", str(repo_root),
        ])
    assert code == 0
    report = json.loads(buf.getvalue())
    entry = report["human_feedback"]["ralph-review"]
    assert entry["status"] == "rejected"
    assert entry["rejected_reason"] is not None
    assert "not present in classifications" in entry["rejected_reason"]
