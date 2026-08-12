"""Orchestrator — state machine driving the pipeline. No LLM.

The orchestrator reads filesystem state, decides what stage to run next,
checks all gates, and invokes the vendor subprocess runner.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from autodev import __version__ as HARNESS_VERSION
from autodev import ralph
from autodev.artifacts.design_packet import (
    write_accepted_design,
    write_design_packet,
)
from autodev.artifacts.design_package_history import archive_design_package
from autodev.artifacts.implementation_index import write_implementation_index
from autodev.artifacts.prd_checklist import write_prd_checklist
from autodev.artifacts.revision_state import (
    load_state,
    reset_prd_target_streak,
    write_state,
)
from autodev.artifacts.verdict import (
    PanelVerdict,
    panel_verdict_transport_incomplete,
)
from autodev.errors import (
    DirtyWorkspace, GateFailed, GatePending, LockConflict, PreflightError,
    SchemaError, StageOutputInvalid,
)
from autodev import overrides_api as ov
from autodev.panel import run_panel_gate, verdict_exists_and_valid
from autodev.preflight import preflight_feature, preflight_repo_root
from autodev.revision_loop import (
    Decision, DecisionKind, RouteDecision,
    consume_pending_feedback, handle_panel_verdict, route_to_layer,
)
from autodev.state.cascade import StalenessCascade
from autodev.state.hashing import hash_file
from autodev.state.lock import Lock
from autodev.state.log import JsonlLog
from autodev.vendors.config import VendorsConfig
from autodev.workspace import snapshot, user_visible_changes

# Mapping from cascade artifact names → gate names (R4 / R4e).
ARTIFACT_TO_GATE = {
    "panel_design_review": "design-review",
    "panel_close_approval": "close-approval",
}

# Bounded retry for "agent exited 0 but produced a deficient artifact"
# (StageOutputInvalid). A small fixed cap distinct from the panel
# revision-loop L budget: this is a transport/format/coverage retry —
# the agent is re-dispatched with the specific deficiency described and
# its prior artifact handed back for in-place amendment, NOT a semantic
# design revision. After this many attempts the failure propagates.
STAGE_OUTPUT_RETRY_MAX = 3

# A build diagnostic may route back to design, but route invalidation must
# delete build.json so the later build cannot be mistaken for fresh output.
# Preserve the selected diagnostic in this harness-internal snapshot before
# deleting downstream artifacts.
ROUTE_FEEDBACK_FILENAME = ".route-feedback.json"
BUILD_CHALLENGES_FILENAME = "build-challenges.md"
RALPH_ITERATION_CONTEXT_FILENAME = "ralph-iteration-context.json"
RALPH_PREVIOUS_REVIEW_FILENAME = "ralph-review.previous.json"

# Position of each cascade artifact, used to express "stop the `run`
# loop before the pipeline crosses into a later phase" (the --until
# flag). Derived from the canonical ARTIFACTS order so it cannot drift
# from the cascade graph.
from autodev.state.cascade import ARTIFACTS as _CASCADE_ARTIFACTS  # noqa: E402

_ARTIFACT_ORDER: dict[str, int] = {
    ref.name: i for i, ref in enumerate(_CASCADE_ARTIFACTS)
}

# --until <phase> → the first artifact of the NEXT phase. The `run`
# loop stops as soon as the stage it would advance to is at or beyond
# this boundary. "spec" (and the default) run to completion.
#   design phase ends once accepted_design is sealed → stop before build
#   build phase ends once implementation_index is sealed → stop before spec
PHASE_STOP_BEFORE: dict[str, str | None] = {
    "design": "build",
    "build": "spec",
    "spec": None,
}


def _stage_order_index(stage_name: str) -> int:
    """Order index of a stage/artifact name; unknown names sort last so
    a name the cascade does not track never trips a boundary stop."""
    return _ARTIFACT_ORDER.get(stage_name, len(_ARTIFACT_ORDER) + 1)


_VERDICT_ORDER = {"fail": 3, "needs_revision": 2, "pass": 1, "skipped": 0}


def _worst_verdict(a: str, b: str) -> str:
    """Return the more-severe of two verdict labels."""
    return a if _VERDICT_ORDER.get(a, 0) >= _VERDICT_ORDER.get(b, 0) else b

# Stage artifacts the orchestrator produces via vendor subprocess.
# (Panel-review artifacts are produced by the panel subsystem, not by
# a vendor subprocess.)
CODING_STAGES = {"design", "build", "spec"}
CODING_STAGE_ARTIFACT = {
    "design": "design.md",
    "build": "build.json",
    "spec": "implemented-spec.md",
}

# Semantic inputs that remain immutable while a stage runs. The write
# allowlist catches everything outside a narrow stage workspace; this second
# list matters for stages with a deliberately broad writable directory (build
# needs the repository, spec may emit implemented-spec envelope files).
_STAGE_PROTECTED_NAMES: dict[str, tuple[str, ...]] = {
    "design": (
        "prd.md",
        "panel-design-review.json",
        "panel-trace-review.json",
        "panel-close-approval.json",
        "build.json",
    ),
    "build": (
        "prd.md",
        "design.md",
        "scope.json",
        "trace.md",
        "test-plan.md",
        "design-changelog.json",
        "design-packet.json",
        "accepted-design.json",
        "panel-design-review.json",
        "panel-trace-review.json",
    ),
    "ralph-review": (
        "design-packet.json",
        "accepted-design.json",
        "design.md",
        "scope.json",
        "trace.md",
        "ralph-iteration-context.json",
        "ralph-review.previous.json",
    ),
    "spec": (
        "implementation-index.json",
        "prd.md",
        "design.md",
        "scope.json",
        "trace.md",
        "test-plan.md",
        "build.json",
    ),
}


def _stage_write_contract(
    *,
    repo_root: Path,
    active: Path,
    stage: str,
    primary_target: Path,
    extra_targets: list[Path],
) -> tuple[list[Path], list[Path]]:
    """Return the writable and protected paths shown to and enforced on a stage.

    Design and Ralph have fixed output sets, so grant only their concrete
    targets plus scratch. Build must discover and change product files, so it
    gets the repository with its reviewed inputs carved out as protected.
    Spec may create optional ``implemented-spec-*.md`` envelope files, so it
    gets the feature directory with its intent/evidence inputs protected.
    """
    owned = [primary_target, *extra_targets]
    scratch = active / "scratch"
    if stage == "build":
        writable = [repo_root]
    elif stage == "spec":
        writable = [active]
    else:
        writable = [*owned, scratch]

    owned_resolved = {path.resolve() for path in owned}
    protected = [
        active / name
        for name in _STAGE_PROTECTED_NAMES.get(stage, ())
        if (active / name).resolve() not in owned_resolved
    ]
    return writable, protected


def _protect_context_inputs(
    protected: list[Path],
    *,
    context_artifacts: list[str],
    owned_outputs: list[Path],
) -> list[Path]:
    """Add every concrete context input while keeping owned outputs writable."""
    owned = {path.resolve() for path in owned_outputs}
    merged: list[Path] = []
    seen: set[Path] = set()
    for path in [*protected, *(Path(raw) for raw in context_artifacts)]:
        resolved = path.resolve()
        if resolved in owned or resolved in seen:
            continue
        seen.add(resolved)
        merged.append(path)
    return merged


@dataclass
class OrchestratorConfig:
    repo_root: Path
    vendors: VendorsConfig
    session_id: str = "autodev-orchestrator"
    yes: bool = False  # non-interactive (for close)


@dataclass
class AdvanceResult:
    stage_name: str
    success: bool
    detail: str = ""


class Orchestrator:
    def __init__(self, cfg: OrchestratorConfig) -> None:
        self.cfg = cfg

    # ---- entry points -----------------------------------------------

    def run(
        self, feature: str, *, max_stages: int = 20,
        stop_before: str | None = None,
    ) -> None:
        """Advance through whatever stages are reachable.

        On each iteration, _enforce_pending_blocking_verdicts runs FIRST
        — before checking ``next_stage_name == "done"`` — so a blocking
        panel verdict on disk always trumps cascade's "everything is
        fresh, nothing to do" judgment. Without this ordering, a build
        rerun whose output is byte-identical (e.g. same test_results +
        same files_changed) leaves the cascade chain undisturbed; if
        the panel verdict is still needs_revision, cascade returns
        next="done" and pipeline-done emits while the verdict stays
        unresolved on disk. enforce-first turns that into a normal
        revision-loop dispatch.

        ``stop_before`` (a cascade artifact name, e.g. ``"build"``)
        bounds the run to a phase: the loop returns cleanly the moment
        the stage it would advance to is at or beyond that boundary.
        This is how ``autodev run --until design`` advances through the
        whole design phase — including the design-review gate and any
        in-design revision reruns — then stops before build, rather than
        either running end-to-end or stepping one stage at a time. A
        blocking verdict that routes to a producer at/beyond the
        boundary also stops here (design-phase reruns stay in design, so
        this only matters for later boundaries).
        """
        boundary = _stage_order_index(stop_before) if stop_before else None
        active = self._feature_active(feature)
        self._check_prerequisites_once(active)
        with self._lock(active, verb="run") as lock:
            self._active_lock = lock
            logger = JsonlLog(active / "log.jsonl")
            for i in range(max_stages):
                self._heartbeat(logger, feature)
                if self._check_pause_sentinel(active):
                    logger.emit(stage="orchestrator", event="paused", feature=feature)
                    raise GatePending("pause", "run `autodev resume` to continue")
                # enforce-first: any blocking verdict on disk triggers a
                # revision-loop dispatch BEFORE we ask cascade whether
                # we're done. HALT_FOR_HUMAN raises inside enforce.
                pending = self._enforce_pending_blocking_verdicts(active, logger, feature)
                if pending is not None:
                    if (
                        boundary is not None
                        and pending.stage_to_rerun is not None
                        and _stage_order_index(pending.stage_to_rerun) >= boundary
                    ):
                        self._emit_boundary_stop(
                            logger, feature, stop_before, pending.stage_to_rerun,
                        )
                        return
                    self._check_dirty_blocks(active)
                    if pending.stage_to_rerun is not None:
                        self._advance_coding(
                            feature, active, pending.stage_to_rerun, logger,
                        )
                        continue
                    # LOCAL_REVISE with no stage_to_rerun shouldn't happen
                    # (enforce raises GatePending for HALT_FOR_HUMAN), but
                    # be defensive — log and exit.
                    logger.emit(
                        stage="orchestrator", event="revision-loop-no-producer",
                        feature=feature, detail={"reason": pending.reason},
                    )
                    return
                next_name = self._next_stage_name(active)
                if next_name == "done":
                    logger.emit(stage="orchestrator", event="pipeline-done", feature=feature)
                    return
                if boundary is not None and _stage_order_index(next_name) >= boundary:
                    self._emit_boundary_stop(logger, feature, stop_before, next_name)
                    return
                result = self._advance_one(feature, active, logger)
                if not result.success:
                    return

    def _emit_boundary_stop(
        self, logger: JsonlLog, feature: str, until: str | None, next_stage: str,
    ) -> None:
        logger.emit(
            stage="orchestrator", event="stopped-at-boundary", feature=feature,
            detail={"until": until, "next_stage": next_stage},
        )

    def advance_one(self, feature: str) -> AdvanceResult:
        """Run exactly the next stage."""
        active = self._feature_active(feature)
        self._check_prerequisites_once(active)
        with self._lock(active, verb="next") as lock:
            self._active_lock = lock
            logger = JsonlLog(active / "log.jsonl")
            self._heartbeat(logger, feature)
            return self._advance_one(feature, active, logger)

    def _heartbeat(self, logger: JsonlLog, feature: str) -> None:
        """Self-validation: stop the moment we no longer hold the feature lock
        (it was released, deleted, or reclaimed by another orchestrator).
        Prevents an orphaned orchestrator from advancing the pipeline
        concurrently with the new lock owner — the failure mode where an abort
        freed the lock but this process kept running."""
        lock = getattr(self, "_active_lock", None)
        if lock is not None and not lock.validate():
            logger.emit(
                stage="orchestrator", event="lock-lost", feature=feature,
                detail={"reason": "owner.json missing or token mismatch; "
                                  "another process owns the lock — self-terminating"},
            )
            raise LockConflict(
                "feature lock lost (released or reclaimed by another process); "
                "orchestrator self-terminating to avoid concurrent runs"
            )

    # ---- guts -------------------------------------------------------

    def _next_stage_name(self, active: Path) -> str:
        next_name = StalenessCascade(active).next_stage()
        if next_name in (
            "implementation_index", "spec", "prd_checklist", "panel_close_approval",
        ) and not self._ralph_loop_complete(active):
            return "build"
        return next_name

    def _feature_active(self, feature: str) -> Path:
        return self.cfg.repo_root / "docs" / "features" / feature / "active"

    def _check_prerequisites_once(self, active: Path) -> None:
        preflight_repo_root(self.cfg.repo_root)
        pre = preflight_feature(active, reject_on_lock=False)
        # We don't reject on lock here; _lock() does.

    def _lock(self, active: Path, *, verb: str) -> Lock:
        return Lock(active, session_id=self.cfg.session_id, verb=verb)

    def _check_pause_sentinel(self, active: Path) -> bool:
        return (active / ".pause").exists()

    def _enforce_pending_blocking_verdicts(
        self, active: Path, logger: JsonlLog, feature: str,
    ) -> Decision | None:
        """Scan all panel-*.json verdicts and route fresh blocking ones.

        Cascade-stale verdicts are about to be re-run naturally —
        skipping them here avoids a false halt on verdicts whose
        underlying artifact has since changed.

        Skip-gate overrides are respected — a gate with an active
        skip_gate override is not halted on.

        For the design-review gate: the panel runner writes TWO verdict
        files (panel-design-review.json + panel-trace-review.json) from
        a single panel run. This method merges them into one
        PanelVerdict before calling handle_panel_verdict, so the
        revision loop sees both groups' findings in one decision.
        """
        from autodev.artifacts.verdict import load_verdict
        cascade = StalenessCascade(active)
        fresh_by_name = cascade.fresh()
        # Map panel file name → cascade artifact name. trace-review is
        # not a cascade artifact in its own right — it is pulled in by
        # the design-review merge below.
        panel_name_by_file = {
            "panel-design-review.json": "panel_design_review",
            "panel-close-approval.json": "panel_close_approval",
        }
        overrides = ov.load(active)
        for p in sorted(active.glob("panel-*.json")):
            if p.name.endswith(".docs.json"):
                continue
            # Skip trace-review here — it's merged into design-review.
            if p.name == "panel-trace-review.json":
                continue
            cascade_name = panel_name_by_file.get(p.name)
            if cascade_name is not None and not fresh_by_name.get(cascade_name, False):
                continue  # cascade-stale → will be re-run naturally
            try:
                v = load_verdict(p)
            except Exception:
                continue
            if panel_verdict_transport_incomplete(v):
                logger.emit(
                    stage="gate",
                    event="panel-transport-verdict-ignored",
                    feature=feature,
                    detail={"path": p.name, "gate": v.gate},
                )
                continue
            if overrides.has_active_skip_gate(v.gate):
                continue

            # For design-review, attempt to merge the parallel
            # trace-review verdict into a single PanelVerdict.
            merged_feedback_paths: list[str] | None = None
            if v.gate == "design-review":
                v_merged, paths = self._merge_trace_into_design(active, v)
                if v_merged is not None:
                    v = v_merged
                    merged_feedback_paths = paths

            if v.effectively_blocks():
                logger.emit(
                    stage="orchestrator", event="blocking-verdict-enforced",
                    feature=feature,
                    detail={"gate": v.gate, "verdict": v.verdict,
                            "finding_count": len(v.findings)},
                )
                # Fingerprints tune the next correction's trust region but
                # never halt. The normal revision loop/L_MAX owns stopping.
                self._prepare_panel_rework(active, v.gate, v, logger, feature)
                decision = handle_panel_verdict(active, v.gate, v)
                # When merging, override feedback_paths so the rerun
                # agent reads BOTH verdict files.
                if (
                    merged_feedback_paths is not None
                    and decision.kind == DecisionKind.LOCAL_REVISE
                ):
                    decision.feedback_paths = list(merged_feedback_paths)
                logger.emit(stage="gate", event="revision-loop-triggered",
                            feature=feature, detail={
                                "gate": v.gate,
                                "decision": decision.kind.value,
                                "stage_to_rerun": decision.stage_to_rerun,
                                "would_rerun": decision.would_rerun,
                                "feedback_paths": decision.feedback_paths,
                                "reason": decision.reason,
                                "source": "pending-blocking-verdict",
                            })
                if decision.kind == DecisionKind.LOCAL_REVISE:
                    return decision
                if decision.kind == DecisionKind.HALT_FOR_HUMAN:
                    logger.emit(stage="gate", event="revision-loop-halt",
                                feature=feature, detail={
                                    "gate": v.gate, "reason": decision.reason,
                                    "source": "pending-blocking-verdict",
                                })
                    raise GatePending(v.gate, decision.reason)
                raise GatePending(
                    v.gate,
                    f"blocking panel verdict on disk (verdict={v.verdict}, "
                    f"{len(v.findings)} findings) — resolve before advancing"
                )
        return None

    def _check_dirty_blocks(self, active: Path) -> None:
        state = snapshot(self.cfg.repo_root)
        if not state.is_dirty:
            return
        o = ov.load(active)
        if o.has_active_dirty_ack():
            return
        raise DirtyWorkspace(
            "workspace dirty — run `autodev acknowledge-dirty` "
            "with a reason or clean via `git restore --worktree . && git clean -fd`"
        )

    def _advance_one(self, feature: str, active: Path, logger: JsonlLog) -> AdvanceResult:
        if self._check_pause_sentinel(active):
            logger.emit(stage="orchestrator", event="paused", feature=feature)
            raise GatePending("pause", "run `autodev resume` to continue")

        # v3-core: defense-in-depth for stale-verdict-skips-enforcement.
        # Even if cascade considers a panel verdict fresh, a blocking
        # verdict on disk must halt the pipeline. Cascade layer-1a
        # catches most of these by invalidating on consulted-doc hash
        # changes; this layer catches the rest.
        pending_decision = self._enforce_pending_blocking_verdicts(
            active, logger, feature,
        )
        if pending_decision is not None:
            self._check_dirty_blocks(active)
            if pending_decision.stage_to_rerun is not None:
                return self._advance_coding(
                    feature, active, pending_decision.stage_to_rerun, logger,
                )
            return AdvanceResult(
                stage_name=f"panel-{pending_decision.gate}",
                success=True,
                detail=pending_decision.reason,
            )

        next_name = self._next_stage_name(active)
        if next_name == "done":
            logger.emit(stage="orchestrator", event="pipeline-done", feature=feature)
            return AdvanceResult(stage_name="done", success=True)

        self._check_dirty_blocks(active)

        # Gate branches
        if next_name in ARTIFACT_TO_GATE:
            gate = ARTIFACT_TO_GATE[next_name]
            return self._advance_gate(feature, active, gate, logger)

        # Harness-authored design-loop artifacts
        if next_name == "design_packet":
            path = write_design_packet(active)
            logger.emit(stage="design-packet", event="artifact-written",
                        feature=feature, detail={"artifact": str(path)})
            return AdvanceResult(stage_name="design_packet", success=True)
        if next_name == "accepted_design":
            path = write_accepted_design(
                active,
                active / "panel-design-review.json",
                active / "panel-trace-review.json",
            )
            logger.emit(stage="accepted-design", event="artifact-written",
                        feature=feature, detail={"artifact": str(path)})
            return AdvanceResult(stage_name="accepted_design", success=True)
        if next_name == "implementation_index":
            path = write_implementation_index(active, repo_root=self.cfg.repo_root)
            logger.emit(stage="implementation-index", event="artifact-written",
                        feature=feature, detail={"artifact": str(path)})
            return AdvanceResult(stage_name="implementation_index", success=True)
        if next_name == "prd_checklist":
            path = write_prd_checklist(active)
            logger.emit(stage="prd-checklist", event="artifact-written",
                        feature=feature, detail={"artifact": str(path)})
            return AdvanceResult(stage_name="prd_checklist", success=True)

        # Coding stage branches
        if next_name in ("design", "scope", "trace", "test_plan"):
            return self._advance_coding(feature, active, "design", logger)
        if next_name in CODING_STAGES:
            return self._advance_coding(feature, active, next_name, logger)

        raise PreflightError(f"orchestrator does not know how to advance to {next_name!r}")

    def _advance_gate(
        self, feature: str, active: Path, gate: str, logger: JsonlLog,
    ) -> AdvanceResult:
        overrides = ov.load(active)
        if overrides.has_active_skip_gate(gate):
            logger.emit(stage="gate", event="skipped-by-override", feature=feature,
                        detail={"gate": gate})
            # Write a synthetic "skipped" verdict for cascade freshness.
            self._write_skip_verdict(feature, active, gate, overrides)
            return AdvanceResult(stage_name=f"panel-{gate}", success=True,
                                 detail="skipped by override")

        primary = self._gate_primary_artifact(active, gate)

        current_hash = hash_file(primary)
        existing = verdict_exists_and_valid(
            feature_active=active, gate=gate, current_source_hash=current_hash,
        )
        if existing is None:
            logger.emit(stage="gate", event="panel-start", feature=feature,
                        detail={"gate": gate})
            v = run_panel_gate(
                gate=gate,
                feature_active=active,
                repo_root=self.cfg.repo_root,
                feature=feature,
                primary_artifact=primary,
                panel_config=self.cfg.vendors.panel,
                probe_config=self.cfg.vendors.probe,
                log_emit=lambda d: logger.emit(
                    stage=d.get("stage", "gate"),
                    event=d.get("event", "panel"),
                    feature=feature,
                    detail=d,
                ),
            )
        else:
            v = existing
        logger.emit(stage="gate", event="panel-done", feature=feature,
                    detail={"gate": gate, "verdict": v.verdict,
                            "invariant": v.has_invariant_violation()})

        # Pause checkpoint: honor a `.pause` set WHILE the panel was
        # running. The verdict is now durably on disk (run_panel_gate
        # wrote it), but we have not yet called handle_panel_verdict —
        # which bumps and persists L[gate] — nor dispatched a producer
        # rerun. Halting here means a pause set mid-panel takes effect
        # the moment the panel finishes, before the design agent revises,
        # instead of one round later (the only earlier checks are at
        # loop-top and _advance_one entry, both of which precede the
        # panel run). On resume, _enforce_pending_blocking_verdicts
        # re-reads this fresh verdict and dispatches the revision exactly
        # once, so L[gate] is bumped exactly once — no double-count.
        if self._check_pause_sentinel(active):
            logger.emit(stage="orchestrator", event="paused", feature=feature)
            raise GatePending("pause", "run `autodev resume` to continue")

        if v.effectively_blocks():
                logger.emit(
                    stage="orchestrator", event="blocking-verdict-enforced",
                    feature=feature,
                    detail={"gate": v.gate, "verdict": v.verdict,
                            "finding_count": len(v.findings)},
                )
                # Fingerprints tune the next correction's trust region but
                # never halt. The normal revision loop/L_MAX owns stopping.
                self._prepare_panel_rework(active, v.gate, v, logger, feature)
                decision = handle_panel_verdict(active, v.gate, v)
                # When merging, override feedback_paths so the rerun
                # agent reads BOTH verdict files.
                if (
                    merged_feedback_paths is not None
                    and decision.kind == DecisionKind.LOCAL_REVISE
                ):
                    decision.feedback_paths = list(merged_feedback_paths)
                logger.emit(stage="gate", event="revision-loop-triggered",
                            feature=feature, detail={
                                "gate": v.gate,
                                "decision": decision.kind.value,
                                "stage_to_rerun": decision.stage_to_rerun,
                                "would_rerun": decision.would_rerun,
                                "feedback_paths": decision.feedback_paths,
                                "reason": decision.reason,
                                "source": "pending-blocking-verdict",
                            })
                if decision.kind == DecisionKind.LOCAL_REVISE:
                    return decision
                if decision.kind == DecisionKind.HALT_FOR_HUMAN:
                    logger.emit(stage="gate", event="revision-loop-halt",
                                feature=feature, detail={
                                    "gate": v.gate, "reason": decision.reason,
                                    "source": "pending-blocking-verdict",
                                })
                    raise GatePending(v.gate, decision.reason)
                raise GatePending(
                    v.gate,
                    f"blocking panel verdict on disk (verdict={v.verdict}, "
                    f"{len(v.findings)} findings) — resolve before advancing"
                )
        return None

    def _check_dirty_blocks(self, active: Path) -> None:
        state = snapshot(self.cfg.repo_root)
        if not state.is_dirty:
            return
        o = ov.load(active)
        if o.has_active_dirty_ack():
            return
        raise DirtyWorkspace(
            "workspace dirty — run `autodev acknowledge-dirty` "
            "with a reason or clean via `git restore --worktree . && git clean -fd`"
        )

    def _advance_one(self, feature: str, active: Path, logger: JsonlLog) -> AdvanceResult:
        if self._check_pause_sentinel(active):
            logger.emit(stage="orchestrator", event="paused", feature=feature)
            raise GatePending("pause", "run `autodev resume` to continue")

        # v3-core: defense-in-depth for stale-verdict-skips-enforcement.
        # Even if cascade considers a panel verdict fresh, a blocking
        # verdict on disk must halt the pipeline. Cascade layer-1a
        # catches most of these by invalidating on consulted-doc hash
        # changes; this layer catches the rest.
        pending_decision = self._enforce_pending_blocking_verdicts(
            active, logger, feature,
        )
        if pending_decision is not None:
            self._check_dirty_blocks(active)
            if pending_decision.stage_to_rerun is not None:
                return self._advance_coding(
                    feature, active, pending_decision.stage_to_rerun, logger,
                )
            return AdvanceResult(
                stage_name=f"panel-{pending_decision.gate}",
                success=True,
                detail=pending_decision.reason,
            )

        next_name = self._next_stage_name(active)
        if next_name == "done":
            logger.emit(stage="orchestrator", event="pipeline-done", feature=feature)
            return AdvanceResult(stage_name="done", success=True)

        self._check_dirty_blocks(active)

        # Gate branches
        if next_name in ARTIFACT_TO_GATE:
            gate = ARTIFACT_TO_GATE[next_name]
            return self._advance_gate(feature, active, gate, logger)

        # Harness-authored design-loop artifacts
        if next_name == "design_packet":
            path = write_design_packet(active)
            logger.emit(stage="design-packet", event="artifact-written",
                        feature=feature, detail={"artifact": str(path)})
            return AdvanceResult(stage_name="design_packet", success=True)
        if next_name == "accepted_design":
            path = write_accepted_design(
                active,
                active / "panel-design-review.json",
                active / "panel-trace-review.json",
            )
            logger.emit(stage="accepted-design", event="artifact-written",
                        feature=feature, detail={"artifact": str(path)})
            return AdvanceResult(stage_name="accepted_design", success=True)
        if next_name == "implementation_index":
            path = write_implementation_index(active, repo_root=self.cfg.repo_root)
            logger.emit(stage="implementation-index", event="artifact-written",
                        feature=feature, detail={"artifact": str(path)})
            return AdvanceResult(stage_name="implementation_index", success=True)
        if next_name == "prd_checklist":
            path = write_prd_checklist(active)
            logger.emit(stage="prd-checklist", event="artifact-written",
                        feature=feature, detail={"artifact": str(path)})
            return AdvanceResult(stage_name="prd_checklist", success=True)

        # Coding stage branches
        if next_name in ("design", "scope", "trace", "test_plan"):
            return self._advance_coding(feature, active, "design", logger)
        if next_name in CODING_STAGES:
            return self._advance_coding(feature, active, next_name, logger)

        raise PreflightError(f"orchestrator does not know how to advance to {next_name!r}")

    def _advance_gate(
        self, feature: str, active: Path, gate: str, logger: JsonlLog,
    ) -> AdvanceResult:
        overrides = ov.load(active)
        if overrides.has_active_skip_gate(gate):
            logger.emit(stage="gate", event="skipped-by-override", feature=feature,
                        detail={"gate": gate})
            # Write a synthetic "skipped" verdict for cascade freshness.
            self._write_skip_verdict(feature, active, gate, overrides)
            return AdvanceResult(stage_name=f"panel-{gate}", success=True,
                                 detail="skipped by override")

        primary = self._gate_primary_artifact(active, gate)

        current_hash = hash_file(primary)
        existing = verdict_exists_and_valid(
            feature_active=active, gate=gate, current_source_hash=current_hash,
        )
        if existing is None:
            logger.emit(stage="gate", event="panel-start", feature=feature,
                        detail={"gate": gate})
            v = run_panel_gate(
                gate=gate,
                feature_active=active,
                repo_root=self.cfg.repo_root,
                feature=feature,
                primary_artifact=primary,
                panel_config=self.cfg.vendors.panel,
                probe_config=self.cfg.vendors.probe,
                log_emit=lambda d: logger.emit(
                    stage=d.get("stage", "gate"),
                    event=d.get("event", "panel"),
                    feature=feature,
                    detail=d,
                ),
            )
        else:
            v = existing
        logger.emit(stage="gate", event="panel-done", feature=feature,
                    detail={"gate": gate, "verdict": v.verdict,
                            "invariant": v.has_invariant_violation()})

        # Pause checkpoint: honor a `.pause` set WHILE the panel was
        # running. The verdict is now durably on disk (run_panel_gate
        # wrote it), but we have not yet called handle_panel_verdict —
        # which bumps and persists L[gate] — nor dispatched a producer
        # rerun. Halting here means a pause set mid-panel takes effect
        # the moment the panel finishes, before the design agent revises,
        # instead of one round later (the only earlier checks are at
        # loop-top and _advance_one entry, both of which precede the
        # panel run). On resume, _enforce_pending_blocking_verdicts
        # re-reads this fresh verdict and dispatches the revision exactly
        # once, so L[gate] is bumped exactly once — no double-count.
        if self._check_pause_sentinel(active):
            logger.emit(stage="orchestrator", event="paused", feature=feature)
            raise GatePending("pause", "run `autodev resume` to continue")

        # For design-review, merge in the parallel trace-review verdict
        # so blocking findings from either group route through one
        # revision-loop decision.
        merged_feedback_paths: list[str] | None = None
        if gate == "design-review":
            v_merged, merged_feedback_paths = self._merge_trace_into_design(active, v)
            if v_merged is not None:
                v = v_merged
            # Coverage/budget alternation bookkeeping: one outcome event
            # per (packet, round type), consumed by
            # autodev.budget.design_phase to decide the next round type
            # and gate completion. Emitted on the MERGED verdict (trace
            # findings count toward pass/fail), and deliberately BEFORE
            # the pause checkpoint below: a first-round pause must not
            # leave the feature with zero outcome events, which the
            # legacy escape in design_gate_satisfied would read as
            # pre-alternation and accept the gate without a budget
            # round. Re-emission on a cached revisit is harmless — the
            # replay dedups first-wins.
            if v.skip_reason is None:
                try:
                    logger.emit(
                        stage="gate", event="design-round-outcome",
                        feature=feature, detail={
                            "gate": gate,
                            "round_key": v.source_hash,
                            "round_type": getattr(v, "round_type", "coverage"),
                            "passed": not v.effectively_blocks(),
                        },
                    )
                except Exception:
                    pass

        if v.effectively_blocks():
            # Fingerprints tune the next correction's trust region but never
            # halt. The normal revision loop/L_MAX owns stopping.
            self._prepare_panel_rework(active, gate, v, logger, feature)
            # v3-core R4: revision loop picks a producer rerun based on
            # reviewer-emitted filename-qualified targets, or halts for
            # human when not auto-rerunnable / L_MAX reached.
            decision = handle_panel_verdict(active, gate, v)
            if (
                merged_feedback_paths is not None
                and decision.kind == DecisionKind.LOCAL_REVISE
            ):
                decision.feedback_paths = list(merged_feedback_paths)
            logger.emit(stage="gate", event="revision-loop-triggered",
                        feature=feature, detail={
                            "gate": gate, "decision": decision.kind.value,
                            "stage_to_rerun": decision.stage_to_rerun,
                            "would_rerun": decision.would_rerun,
                            "feedback_paths": decision.feedback_paths,
                            "reason": decision.reason,
                        })
            if decision.kind == DecisionKind.LOCAL_REVISE:
                # v3-core: orchestrator DIRECTLY dispatches the producer
                # stage in the same advance cycle. No delete-then-regen
                # cycle via cascade; the stage agent reads the existing
                # artifact + panel verdicts via CONTEXT_ARTIFACTS and
                # revises in place.
                if decision.stage_to_rerun is not None:
                    return self._advance_coding(
                        feature, active, decision.stage_to_rerun, logger,
                    )
                return AdvanceResult(
                    stage_name=f"panel-{gate}", success=True,
                    detail=decision.reason,
                )
            if decision.kind == DecisionKind.HALT_FOR_HUMAN:
                logger.emit(stage="gate", event="revision-loop-halt",
                            feature=feature, detail={
                                "gate": gate, "reason": decision.reason,
                            })
                raise GatePending(gate, decision.reason)
            # OUT_OF_SCOPE fallback.
            if v.verdict == "fail" or v.has_invariant_violation():
                raise GateFailed(gate, v.verdict,
                                 "invariant_violation finding" if v.has_invariant_violation()
                                 else "panel verdict fail")
            raise GatePending(gate, f"verdict={v.verdict}; revise or skip-gate")
        reset_prd_target_streak(active, gate)
        # The gate passed — any rework mode computed from its blocking
        # verdict is now stale and must not leak into a later design
        # rerun dispatched for an unrelated reason (P1: route_to_layer
        # rerun inheriting `patch` from an already-fixed verdict).
        (active / "rework-mode.json").unlink(missing_ok=True)
        return AdvanceResult(stage_name=f"panel-{gate}", success=True)

    def _prepare_panel_rework(
        self, active: Path, gate: str, v: PanelVerdict,
        logger: JsonlLog, feature: str,
    ) -> None:
        """Record finding telemetry and select the next rework mode.

        Never raises or halts. A bookkeeping failure degrades to the normal
        revision loop without fingerprint-informed trust-region selection.
        """
        try:
            from autodev.diagnosis import prepare_panel_rework
            mode = prepare_panel_rework(active, gate, v)
        except Exception as e:  # pragma: no cover - defensive
            logger.emit(stage="gate", event="rework-mode-error",
                        feature=feature,
                        detail={"gate": gate, "error": str(e)[:300]})
            return
        logger.emit(stage="gate", event="rework-mode-selected",
                    feature=feature, detail={"gate": gate, "mode": mode})

    def _merge_trace_into_design(
        self, active: Path, v_design: PanelVerdict,
    ) -> tuple[PanelVerdict | None, list[str] | None]:
        """Return a merged design-review PanelVerdict if a fresh
        panel-trace-review.json exists referencing the same source as
        ``v_design``. Returns (merged, feedback_paths) or (None, None).
        """
        from autodev.artifacts.verdict import load_verdict
        trace_path = active / "panel-trace-review.json"
        if not trace_path.exists():
            return None, None
        try:
            v_trace = load_verdict(trace_path)
        except Exception:
            return None, None
        if panel_verdict_transport_incomplete(v_trace):
            return None, None
        if v_trace.source != v_design.source or v_trace.source_hash != v_design.source_hash:
            return None, None
        # Decision merge: design-review group's synthesizer is the only one
        # that emits a `decision` object today (trace-review group always has
        # decision=None). If design's decision says "pass" but trace has
        # blocking findings, the merged decision MUST NOT be "pass" — the
        # design-review group's verdict is not authoritative over the whole
        # design evaluation. Drop the decision in that case so handle_panel_verdict
        # falls back to filename-based dispatch on the merged findings list.
        trace_has_blocking = bool(v_trace.blocking_findings())
        merged_decision = v_design.decision
        if (
            merged_decision is not None
            and merged_decision.outcome == "pass"
            and trace_has_blocking
        ):
            merged_decision = None
        trace_findings = copy.deepcopy(v_trace.findings)
        trace_id_map: dict[str, str] = {}
        for index, finding in enumerate(trace_findings):
            if finding.finding_id:
                new_id = f"trace-review/{finding.finding_id}"
                trace_id_map[finding.finding_id] = new_id
                finding.finding_id = new_id
            elif v_trace.issue_clusters:
                # A clustered verdict should always carry IDs; preserve a
                # conservative unique identity if loading older mixed data.
                finding.finding_id = f"trace-review/legacy-{index}"
        trace_clusters = copy.deepcopy(v_trace.issue_clusters)
        for cluster in trace_clusters:
            cluster.finding_ids = [
                trace_id_map.get(i, f"trace-review/{i}")
                for i in cluster.finding_ids
            ]
        merged_clusters = copy.deepcopy(v_design.issue_clusters)
        by_cluster_id = {c.cluster_id: c for c in merged_clusters}
        priority_rank = {"P0": 0, "P1": 1, "P2": 2}
        for cluster in trace_clusters:
            existing = by_cluster_id.get(cluster.cluster_id)
            if existing is None:
                merged_clusters.append(cluster)
                by_cluster_id[cluster.cluster_id] = cluster
                continue
            existing.finding_ids.extend(cluster.finding_ids)
            if priority_rank[cluster.priority] < priority_rank[existing.priority]:
                existing.priority = cluster.priority
        merged = PanelVerdict(
            gate="design-review",
            verdict=_worst_verdict(v_design.verdict, v_trace.verdict),  # type: ignore[arg-type]
            findings=list(v_design.findings) + trace_findings,
            source=v_design.source,
            source_hash=v_design.source_hash,
            prompt_file=v_design.prompt_file,
            prompt_hash=v_design.prompt_hash,
            harness_version=v_design.harness_version,
            run_ts=v_design.run_ts,
            consulted_docs=v_design.consulted_docs,
            per_vendor_raw={**v_design.per_vendor_raw, **v_trace.per_vendor_raw},
            dropped_findings=list(v_design.dropped_findings) + list(v_trace.dropped_findings),
            coverage_map={**v_design.coverage_map, **v_trace.coverage_map},
            decision=merged_decision,
            issue_clusters=merged_clusters,
            release_threshold=v_design.release_threshold,
            decision_overridden_by_rigor=v_design.decision_overridden_by_rigor,
            decision_overridden_by_policy=v_design.decision_overridden_by_policy,
            round_type=v_design.round_type,
        )
        return merged, ["panel-design-review.json", "panel-trace-review.json"]

    def _apply_revision_invalidation(self, active: Path, decision: Decision) -> None:
        """Delete artifacts so cascade re-runs ``decision.stage_to_rerun``.

        design → design.md + scope.json + trace.md + test-plan.md
        spec  → implemented-spec.md
        """
        stage = decision.stage_to_rerun
        if stage == "design":
            (active / "design.md").unlink(missing_ok=True)
            (active / "scope.json").unlink(missing_ok=True)
            (active / "test-plan.md").unlink(missing_ok=True)
            (active / "trace.md").unlink(missing_ok=True)
            (active / "design-packet.json").unlink(missing_ok=True)
            (active / "panel-design-review.json").unlink(missing_ok=True)
            (active / "panel-trace-review.json").unlink(missing_ok=True)
            (active / "accepted-design.json").unlink(missing_ok=True)
        elif stage == "spec":
            (active / "implemented-spec.md").unlink(missing_ok=True)
            (active / "panel-close-approval.json").unlink(missing_ok=True)
        else:
            raise PreflightError(
                f"revision loop cannot invalidate unknown stage {stage!r}"
            )

    def _gate_primary_artifact(self, active: Path, gate: str) -> Path:
        mapping = {
            "design-review": active / "design-packet.json",
            # trace-review reviews the design packet too (same subject);
            # the consulted docs (trace.md, test-plan.md, prd.md) are
            # hash-pinned separately in run_panel_gate.
            "trace-review":  active / "design-packet.json",
            # close-approval reviews the implemented spec directly.
            # PRD and checklist are hash-pinned consulted docs.
            "close-approval": active / "implemented-spec.md",
        }
        return mapping[gate]

    def _write_skip_verdict(
        self, feature: str, active: Path, gate: str, overrides,
    ) -> None:
        """When skip-gate is active, write a synthetic panel-verdict.json
        with verdict='skipped' so cascade sees the artifact as fresh."""
        from autodev.artifacts.verdict import PanelVerdict, write_verdict
        from autodev.panel import prompt_path
        from datetime import datetime, timezone

        primary = self._gate_primary_artifact(active, gate)
        p_prompt = prompt_path(gate)
        skip_record = next(
            (r for r in overrides.active_records()
             if r.kind == "skip_gate" and r.gate == gate),
            None,
        )
        v = PanelVerdict(
            gate=gate,
            verdict="skipped",
            findings=[],
            source=str(primary),
            source_hash=hash_file(primary),
            prompt_file=str(p_prompt),
            prompt_hash=hash_file(p_prompt) if p_prompt.exists() else "sha256:" + "0" * 64,
            harness_version=HARNESS_VERSION,
            run_ts=datetime.now(timezone.utc).isoformat(),
            skip_reason=skip_record.reason if skip_record else "",
            skip_who=skip_record.who if skip_record else "",
        )
        write_verdict(active / f"panel-{gate}.json", v)
        # design-review's panel run normally writes BOTH
        # panel-design-review.json and panel-trace-review.json from one
        # invocation; when skip-gate substitutes for the panel run, we
        # must mirror that contract so write_accepted_design has both
        # verdict files to seal against.
        if gate == "design-review":
            from autodev.panel.runner import review_prompt_path
            trace_prompt = review_prompt_path("trace-review")
            v_trace = PanelVerdict(
                gate="trace-review",
                verdict="skipped",
                findings=[],
                source=str(primary),
                source_hash=hash_file(primary),
                prompt_file=str(trace_prompt),
                prompt_hash=hash_file(trace_prompt) if trace_prompt.exists() else "sha256:" + "0" * 64,
                harness_version=HARNESS_VERSION,
                run_ts=datetime.now(timezone.utc).isoformat(),
                skip_reason=skip_record.reason if skip_record else "",
                skip_who=skip_record.who if skip_record else "",
            )
            write_verdict(active / "panel-trace-review.json", v_trace)

    # ------------------------------------------------------------------
    # Coding-stage dispatch (G2 — phase-2)

    # Stage → (primary artifact name, extra artifact names, prompt file)
    _STAGE_MANIFEST: dict[str, tuple[str, tuple[str, ...], str]] = {
        "design": ("design.md",   ("scope.json", "trace.md", "test-plan.md", "design-changelog.json"), "stage-design.md"),
        "build":  ("build.json",  (),                         "stage-implement.md"),
        "spec":   ("implemented-spec.md", ("README.md",),     "stage-spec.md"),
    }

    def _advance_coding(
        self, feature: str, active: Path, stage: str, logger: JsonlLog,
    ) -> AdvanceResult:
        """Run a coding stage: load prompt → subprocess → validate → G3 drift check.

        Implements G2 (coding-stage wiring) + G3 (out-of-scope-write hook).
        """
        from autodev.prompts_loader import render_stage_prompt

        if stage not in self._STAGE_MANIFEST:
            raise PreflightError(f"unknown coding stage: {stage!r}")

        # v3-core R4: no global G ceiling; g-dev-bump machinery removed.

        primary_name, extra_names, prompt_file = self._STAGE_MANIFEST[stage]
        primary_target = active / primary_name
        extra_targets = [active / n for n in extra_names]
        stage_spec = self.cfg.vendors.resolve(stage)

        # One harness-owned contract drives both the prompt and the post-stage
        # drift check. Agents no longer have to infer write scope from cwd.
        allowed_write_paths, protected_write_paths = _stage_write_contract(
            repo_root=self.cfg.repo_root,
            active=active,
            stage=stage,
            primary_target=primary_target,
            extra_targets=extra_targets,
        )

        # On a design rerun the prior package is on disk; pre-fill the
        # subagent's .tmp working copies (see _run_stage_subprocess_checked)
        # so it revises in place via Edit instead of regenerating every
        # artifact. The prompt is told the .tmp are pre-filled only when
        # the landed primary already exists. This holds across output-retry
        # attempts too: the deficient prior artifact is still on disk, so a
        # retry edits it in place rather than starting over.
        preseeded = stage == "design" and primary_target.exists()

        # Prompt is rendered per attempt: on an output-validation retry the
        # harness appends <stage>-output-rejection.json (and the prior
        # artifacts) to CONTEXT_ARTIFACTS so the agent amends in place
        # rather than regenerating from scratch.
        def render_prompt(
            extra_context: list[str], continuation: bool = False,
        ) -> str:
            merged = list(self._context_artifacts_for_stage(
                active, stage, primary_target, extra_targets,
            ))
            for c in extra_context:
                if c not in merged:
                    merged.append(c)
            return render_stage_prompt(
                stage=stage,
                feature=feature,
                feature_active=active,
                repo_root=self.cfg.repo_root,
                primary_target=primary_target,
                extra_targets=extra_targets,
                context_artifacts=merged,
                writable_paths=allowed_write_paths,
                protected_paths=protected_write_paths,
                preseeded=preseeded,
                continuation=continuation,
            )

        logger.emit(stage=stage, event="subprocess-dispatch", feature=feature,
                    detail={"vendor": stage_spec.vendor, "model": stage_spec.model,
                            "primary": str(primary_target),
                            "extras": [str(p) for p in extra_targets]})

        if stage != "build":
            result = self._dispatch_stage_with_output_retry(
                feature=feature,
                active=active,
                stage=stage,
                logger=logger,
                stage_spec=stage_spec,
                render_prompt=render_prompt,
                primary_target=primary_target,
                extra_targets=extra_targets,
                allowed_write_paths=allowed_write_paths,
                protected_write_paths=protected_write_paths,
            )
            archive_path = None
            if stage == "design":
                archive_path = archive_design_package(active)
            self._consume_stage_feedback(active, stage)
            detail = {
                "artifact": str(primary_target),
                "elapsed_sec": result.elapsed_sec,
            }
            if archive_path is not None:
                detail["design_package_archive"] = str(archive_path)
            logger.emit(stage=stage, event="stage-complete", feature=feature,
                        detail=detail)
            return AdvanceResult(stage_name=stage, success=True)

        return self._advance_build_with_ralph_loop(
            feature=feature,
            active=active,
            logger=logger,
            stage_spec=stage_spec,
            primary_target=primary_target,
            allowed_write_paths=allowed_write_paths,
            protected_write_paths=protected_write_paths,
        )

    def _context_artifacts_for_stage(
        self,
        active: Path,
        stage: str,
        primary_target: Path,
        extra_targets: list[Path],
    ) -> list[str]:
        """Return stage-relevant feedback/context artifacts for prompt rendering."""
        context_artifacts: list[str] = []
        fresh_design = stage == "design" and not primary_target.exists()

        def append_once(path: Path) -> None:
            rendered = str(path)
            if rendered not in context_artifacts:
                context_artifacts.append(rendered)

        # Route feedback is persisted separately from the artifacts that route
        # invalidation deletes. Keep it pending across output retries and only
        # consume it after the producer stage completes successfully.
        pending = load_state(active).pending_feedback.get(stage, [])
        for raw_path in pending:
            path = Path(raw_path)
            if not path.is_absolute():
                path = active / path
            if path.exists():
                append_once(path)
                continue

            # Backward compatibility for runs routed by harness versions that
            # stored build.json itself as feedback and then deleted it. The
            # build stage's durable challenge record contains the same concrete
            # routing diagnosis and lets an in-flight run recover once.
            if stage == "design" and path.name == "build.json":
                legacy_fallback = active / BUILD_CHALLENGES_FILENAME
                if legacy_fallback.exists():
                    append_once(legacy_fallback)
                    continue
            raise PreflightError(
                f"pending feedback for stage {stage!r} is missing: {path}. "
                "Refusing to rerun without the diagnostic that triggered it."
            )

        # v3-core: no explicit pending_feedback queue for panel reruns.
        # Instead, pass stage-relevant panel verdicts as context to
        # producer reruns:
        #
        # - design produces design.md/scope.json/trace.md/test-plan.md;
        #   it must see BOTH design-review (its own gate) AND
        #   close-approval (close can route findings back to design
        #   when e.g. trace.md weakened a PRD modal verb).
        # - build produces code; it sees close-approval so it can fix
        #   shipped-behavior gaps the close-gate identified.
        # - spec is a passive describer (reads code → writes
        #   implemented-spec). It MUST NOT receive close-approval
        #   feedback — feeding a passive describer panel verdicts only
        #   tempts it to rewrite descriptions to placate reviewers,
        #   which doesn't change code or design and loops the harness.
        panel_context_by_stage = {
            "design": {
                "panel-design-review.json",
                "panel-trace-review.json",
                "panel-close-approval.json",
            },
            "build": {"panel-close-approval.json"},
        }
        if not fresh_design:
            for p in sorted(active.glob("panel-*.json")):
                if p.name.endswith(".docs.json"):
                    continue
                allowed_panels = panel_context_by_stage.get(stage, set())
                if p.name in allowed_panels:
                    append_once(p)

        # Existing own artifact lets a stage revise in place rather than
        # regenerate from scratch.
        if not fresh_design:
            if primary_target.exists():
                append_once(primary_target)
            for extra in extra_targets:
                if extra.exists():
                    append_once(extra)

            if stage == "design":
                changelog_path = active / "design-changelog.json"
                if changelog_path.exists():
                    append_once(changelog_path)

        if stage == "build":
            # Build is inside the Ralph loop: each retry must see the latest
            # code-level classifications and loop state, otherwise it can only
            # re-run tests and write the same build.json forever.
            for name in ("ralph-review.json", "ralph-state.json"):
                path = active / name
                if path.exists():
                    append_once(path)
            rejection = active / "build-output-rejection.json"
            if rejection.exists():
                append_once(rejection)

        # build.json if it exists. Do not hand it to spec: spec must see
        # only implementation-index.json so semantic build deviations do
        # not leak into the code-first implemented spec.
        build_json = active / "build.json"
        if stage != "spec" and build_json.exists():
            append_once(build_json)
        return context_artifacts

    def _consume_stage_feedback(self, active: Path, stage: str) -> None:
        """Clear feedback only after a producer stage succeeds.

        Failed subprocesses and output-validation retries retain the pending
        paths, so a diagnostic cannot be lost between attempts.
        """
        consumed = consume_pending_feedback(active, stage)
        if stage != "design":
            return
        route_feedback = (active / ROUTE_FEEDBACK_FILENAME).resolve()
        if any(Path(path).resolve() == route_feedback for path in consumed):
            route_feedback.unlink(missing_ok=True)

    def _run_stage_subprocess_checked(
        self,
        *,
        feature: str,
        active: Path,
        stage: str,
        logger: JsonlLog,
        stage_spec,
        prompt: str,
        resume_prompt: str | None,
        primary_target: Path,
        extra_targets: list[Path],
        allowed_write_paths: list[Path],
        pre_snap,
        protected_write_paths: list[Path] | None = None,
    ):
        from autodev.artifacts.failure import FailureReport, write_failure
        from autodev.vendors.subprocess_runner import run_stage_subprocess
        from autodev.workspace import snapshot, detect_out_of_scope_writes
        from datetime import datetime, timezone
        import os
        import shutil

        # Design reruns revise the prior package in place: pre-fill each
        # extra artifact's .tmp from its landed version so the subagent
        # edits only what changed. The runner pre-seeds the primary
        # (design.md) itself via preseed=True. A stale extra .tmp with no
        # landed source is dropped so it can't leak into the next round.
        preseed = stage == "design" and primary_target.exists()
        if preseed:
            for extra_target in extra_targets:
                extra_tmp = extra_target.with_name(extra_target.name + ".tmp")
                if extra_target.exists():
                    try:
                        shutil.copyfile(extra_target, extra_tmp)
                    except OSError:
                        pass
                elif extra_tmp.exists():
                    try:
                        extra_tmp.unlink()
                    except OSError:
                        pass

        result = run_stage_subprocess(
            stage=stage,
            stage_spec=stage_spec,
            prompt=prompt,
            artifact_target=primary_target,
            feature_active=active,
            allowed_write_paths=allowed_write_paths,
            cwd=self.cfg.repo_root,
            log_emit=lambda d: logger.emit(stage=stage, event=d.get("event", "subprocess"),
                                           feature=feature, detail=d),
            probe_config=self.cfg.vendors.probe,
            preseed=preseed,
            resume_prompt=resume_prompt,
        )

        if not result.ok:
            logger.emit(stage=stage, event="subprocess-failed", feature=feature,
                        detail={"kind": result.failure_kind,
                                "detail": result.failure_detail})
            raise PreflightError(
                f"stage {stage} failed: {result.failure_kind} — {result.failure_detail}"
            )

        for extra_target in extra_targets:
            extra_tmp = extra_target.with_name(extra_target.name + ".tmp")
            if extra_tmp.exists():
                os.replace(extra_tmp, extra_target)
            elif not extra_target.exists():
                fr = FailureReport(
                    stage=stage, kind="missing_artifact",
                    detail=f"extra artifact {extra_target.name} not produced",
                    subprocess_exit=result.exit_code, stderr_tail="",
                    ts=datetime.now(timezone.utc).isoformat(),
                )
                write_failure(active / f"{stage}-failure.json", fr)
                raise StageOutputInvalid(
                    stage, "missing_artifact",
                    f"extra artifact {extra_target.name} not produced",
                )

        # Provenance defense: a markdown primary artifact MUST carry a
        # parseable `<!-- source_hash: sha256:... -->` header. Without
        # this header the staleness cascade silently treats the artifact
        # as stale forever, causing the orchestrator to re-dispatch the
        # stage indefinitely. Fail loudly here instead.
        if primary_target.suffix == ".md" and primary_target.exists():
            from autodev.state.hashing import parse_markdown_source_hash
            recorded = parse_markdown_source_hash(primary_target)
            if recorded is None:
                fr = FailureReport(
                    stage=stage, kind="malformed_artifact",
                    detail=(
                        f"{primary_target.name} has no parseable "
                        f"`<!-- source_hash: sha256:... -->` header on "
                        f"line 1-5; cascade would loop forever. Fix the "
                        f"stage prompt or the produced artifact."
                    ),
                    subprocess_exit=result.exit_code, stderr_tail="",
                    ts=datetime.now(timezone.utc).isoformat(),
                )
                write_failure(active / f"{stage}-failure.json", fr)
                raise StageOutputInvalid(
                    stage, "malformed_artifact",
                    f"{primary_target.name} has no parseable "
                    f"`<!-- source_hash: sha256:... -->` header on line 1-5 "
                    f"(cascade would loop forever)",
                )

        post_snap = snapshot(
            self.cfg.repo_root,
            watched_paths=protected_write_paths,
        )
        escapes = detect_out_of_scope_writes(
            pre_snap, post_snap,
            allowed_scope=allowed_write_paths,
            repo_root=self.cfg.repo_root,
            protected_scope=protected_write_paths,
        )
        if escapes:
            fr = FailureReport(
                stage=stage, kind="detected_out_of_scope_write",
                detail=(
                    f"{len(escapes)} file(s) touched outside writable paths "
                    f"or inside protected paths: {escapes[:5]}"
                ),
                subprocess_exit=result.exit_code,
                stderr_tail="",
                ts=datetime.now(timezone.utc).isoformat(),
                workspace_dirty=post_snap.is_dirty,
            )
            write_failure(active / f"{stage}-failure.json", fr)
            raise PreflightError(
                f"stage {stage} violated its write contract: {escapes}"
            )
        return result

    def _dispatch_stage_with_output_retry(
        self,
        *,
        feature: str,
        active: Path,
        stage: str,
        logger: JsonlLog,
        stage_spec,
        render_prompt: Callable[[list[str], bool], str],
        primary_target: Path,
        extra_targets: list[Path],
        allowed_write_paths: list[Path],
        protected_write_paths: list[Path] | None = None,
    ):
        """Dispatch a coding stage, retrying on a deficient deliverable.

        A subprocess that exits 0 but produces a missing/malformed
        artifact raises ``StageOutputInvalid`` from
        ``_run_stage_subprocess_checked``. Rather than propagating that
        on the first occurrence (which stalled the whole run on a
        recoverable transport/format slip), re-dispatch the SAME agent up
        to ``STAGE_OUTPUT_RETRY_MAX`` times. Each retry hands the agent a
        ``<stage>-output-rejection.json`` describing exactly what was
        wrong plus its own prior artifact, with an explicit instruction
        to amend in place rather than start over.

        Hard subprocess failures (timeout, non-zero exit, interrupted)
        and containment violations (out-of-scope writes) are NOT
        ``StageOutputInvalid`` and still propagate immediately — those
        are not "fix your output" situations.
        """
        from autodev.workspace import snapshot

        feedback_path = active / f"{stage}-output-rejection.json"
        # Clear any stale rejection from a prior dispatch so attempt 1
        # starts clean (a fresh stage run is not an amendment).
        feedback_path.unlink(missing_ok=True)

        for attempt in range(1, STAGE_OUTPUT_RETRY_MAX + 1):
            extra_context: list[str] = []
            if attempt > 1:
                # Hand back the deficient artifacts + the structured
                # rejection so the agent fixes exactly what failed.
                if primary_target.exists():
                    extra_context.append(str(primary_target))
                for et in extra_targets:
                    if et.exists():
                        extra_context.append(str(et))
                extra_context.append(str(feedback_path))
            prompt = render_prompt(extra_context)
            resume_prompt = render_prompt(extra_context, True)
            pre_snap = snapshot(
                self.cfg.repo_root,
                watched_paths=protected_write_paths,
            )
            try:
                result = self._run_stage_subprocess_checked(
                    feature=feature,
                    active=active,
                    stage=stage,
                    logger=logger,
                    stage_spec=stage_spec,
                    prompt=prompt,
                    resume_prompt=resume_prompt,
                    primary_target=primary_target,
                    extra_targets=extra_targets,
                    allowed_write_paths=allowed_write_paths,
                    pre_snap=pre_snap,
                    protected_write_paths=protected_write_paths,
                )
                # Success — drop any rejection note from earlier attempts so
                # a later dispatch does not mistake it for live feedback.
                feedback_path.unlink(missing_ok=True)
                return result
            except StageOutputInvalid as e:
                self._write_output_rejection_feedback(
                    feedback_path, stage=stage, attempt=attempt,
                    kind=e.kind, detail=e.detail,
                    primary_target=primary_target, extra_targets=extra_targets,
                    missing_ids=None,
                )
                if attempt >= STAGE_OUTPUT_RETRY_MAX:
                    logger.emit(
                        stage=stage, event="output-rejected-exhausted",
                        feature=feature, detail={
                            "attempts": attempt, "kind": e.kind,
                            "detail": str(e.detail)[:300],
                        },
                    )
                    raise
                logger.emit(
                    stage=stage, event="output-rejected-retrying",
                    feature=feature, detail={
                        "attempt": attempt, "max": STAGE_OUTPUT_RETRY_MAX,
                        "kind": e.kind, "detail": str(e.detail)[:300],
                    },
                )
        # Unreachable: the final attempt either returns or re-raises.
        raise PreflightError(f"stage {stage} retry loop exited unexpectedly")

    def _write_output_rejection_feedback(
        self,
        feedback_path: Path,
        *,
        stage: str,
        attempt: int,
        kind: str,
        detail: str,
        primary_target: Path,
        extra_targets: list[Path] | None = None,
        missing_ids: list[str] | None = None,
    ) -> None:
        """Write the structured rejection the next attempt reads.

        Deliberately small and machine-readable. The amend instruction is
        explicit: keep every valid entry, fix only the flagged problem,
        do NOT regenerate from scratch — this is what makes the retry a
        revision of the prior (deficient) artifact rather than a rewrite.
        """
        import json as _json
        from autodev.state.atomic import atomic_write

        payload: dict = {
            "stage": stage,
            "rejected_attempt": attempt,
            "kind": kind,
            "detail": detail,
            "artifact": primary_target.name,
            "instruction": (
                f"Your previous {primary_target.name} was rejected by the "
                f"harness ({kind}): {detail}. The prior version is in "
                f"CONTEXT_ARTIFACTS. AMEND it in place: keep every entry "
                f"that was already correct and fix ONLY the flagged "
                f"problem. Do NOT regenerate the artifact from scratch."
            ),
        }
        if extra_targets:
            payload["extra_artifacts"] = [p.name for p in extra_targets]
        if missing_ids:
            payload["missing_scope_ids"] = sorted(missing_ids)
        atomic_write(feedback_path, _json.dumps(payload, indent=2) + "\n")

    def _prepare_ralph_iteration_context(self, active: Path) -> Path:
        """Persist the pre-build Git ref and prior accepted Ralph output.

        The same Ralph iteration can be re-entered after a quota pause,
        subprocess failure, or process restart.  In that case the original
        ``before_ref`` must survive so an amend/retry cannot erase the very
        delta the reviewer needs to inspect.
        """
        import json
        from datetime import datetime, timezone

        from autodev.artifacts.implementation_index import _current_git_head
        from autodev.state.atomic import atomic_write, atomic_write_json

        state = ralph.load_ralph_state(active)
        iteration = state.iter + 1
        trace_hash = hash_file(active / "trace.md")
        context_path = active / RALPH_ITERATION_CONTEXT_FILENAME
        previous_path = active / RALPH_PREVIOUS_REVIEW_FILENAME

        existing: dict = {}
        if context_path.exists():
            try:
                candidate = json.loads(context_path.read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    existing = candidate
            except (OSError, json.JSONDecodeError):
                existing = {}

        reuse = (
            existing.get("schema") == 1
            and existing.get("iteration") == iteration
            and existing.get("trace_hash") == trace_hash
            and isinstance(existing.get("before_ref"), str)
        )
        before_ref = (
            existing["before_ref"]
            if reuse
            else (_current_git_head(self.cfg.repo_root) or "")
        )

        # On a new iteration, freeze the last accepted review under a
        # different filename.  The live ralph-review.json target is replaced
        # atomically by the upcoming reviewer turn, so handing that mutable
        # path back would make output retries lose their real baseline.
        if not reuse:
            previous_path.unlink(missing_ok=True)
            live_review = active / "ralph-review.json"
            if state.iter > 0 and live_review.exists():
                try:
                    statuses = ralph.parse_review_statuses(live_review)
                    ralph.validate_active_review_coverage(
                        statuses, ralph.active_scope_ids(active / "scope.json")
                    )
                except (OSError, SchemaError):
                    pass
                else:
                    atomic_write(previous_path, live_review.read_bytes())

        previous_review = str(previous_path) if previous_path.exists() else None
        previous_hash = hash_file(previous_path) if previous_path.exists() else None
        atomic_write_json(context_path, {
            "schema": 1,
            "iteration": iteration,
            "status": "build_pending",
            "written_at": datetime.now(timezone.utc).isoformat(),
            "repo_root": str(self.cfg.repo_root),
            "trace_hash": trace_hash,
            "before_ref": before_ref,
            "after_ref": None,
            "previous_ralph_review_path": previous_review,
            "previous_ralph_review_hash": previous_hash,
            "diff": None,
        })
        return context_path

    def _finalize_ralph_iteration_context(self, context_path: Path) -> dict:
        """Add the post-build ref and executable diff pointers."""
        import json
        from datetime import datetime, timezone

        from autodev.artifacts.implementation_index import _current_git_head
        from autodev.state.atomic import atomic_write_json

        try:
            payload = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise PreflightError(
                f"cannot finalize {context_path.name}: {e}"
            ) from e
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise PreflightError(
                f"cannot finalize {context_path.name}: invalid schema"
            )

        before_ref = payload.get("before_ref")
        after_ref = _current_git_head(self.cfg.repo_root) or ""
        refs_available = bool(before_ref and after_ref)
        payload.update({
            "status": "ready_for_review",
            "written_at": datetime.now(timezone.utc).isoformat(),
            "after_ref": after_ref,
            "diff": {
                "available": refs_available,
                "patch_command": (
                    [
                        "git", "-C", str(self.cfg.repo_root), "diff",
                        "--no-ext-diff", before_ref, after_ref, "--",
                    ]
                    if refs_available else []
                ),
                "changed_files_command": (
                    [
                        "git", "-C", str(self.cfg.repo_root), "diff",
                        "--no-ext-diff", "--name-status",
                        before_ref, after_ref, "--",
                    ]
                    if refs_available else []
                ),
            },
        })
        atomic_write_json(context_path, payload)
        return payload

    def _stamp_build_sealed_ref(self, build_path: Path) -> None:
        """Augment the just-written build.json with the current git HEAD.

        Read-modify-write the build.json file so that any WIP commits
        produced inside the build subprocess change the build.json
        content hash. Cascade can then propagate that change through
        impl-index / spec / panel-close-approval — without this stamp,
        byte-identical build.json across reruns silently leaves the
        downstream chain "fresh" and the pipeline emits a misleading
        pipeline-done while close-approval is still needs_revision.
        """
        if not build_path.exists():
            return
        try:
            from autodev.artifacts.build import load_build, write_build
            from autodev.artifacts.implementation_index import _current_git_head
        except ImportError:
            return
        try:
            report = load_build(build_path)
        except Exception:
            return
        head = _current_git_head(self.cfg.repo_root) or ""
        if report.sealed_ref == head:
            return  # already current; avoid spurious mtime churn
        report.sealed_ref = head
        write_build(build_path, report)

    def _advance_build_with_ralph_loop(
        self,
        *,
        feature: str,
        active: Path,
        logger: JsonlLog,
        stage_spec,
        primary_target: Path,
        allowed_write_paths: list[Path],
        protected_write_paths: list[Path] | None = None,
    ) -> AdvanceResult:
        self._reset_ralph_state_if_inputs_changed(active, logger, feature)
        while True:
            self._heartbeat(logger, feature)
            # Pause checkpoint between build rounds: the only outer pause
            # check (_advance_one entry) precedes the whole Ralph loop, so
            # without this a pause set during build round N is not seen
            # until the loop exits — one or more rounds later. ralph-state
            # is persisted each round, so halting here is resumable: on
            # resume cascade re-selects "build" and re-enters this loop.
            if self._check_pause_sentinel(active):
                logger.emit(stage="orchestrator", event="paused", feature=feature)
                raise GatePending("pause", "run `autodev resume` to continue")
            from autodev.prompts_loader import render_stage_prompt
            from autodev.workspace import snapshot

            ralph_context_path = self._prepare_ralph_iteration_context(active)

            feedback_path = active / "build-output-rejection.json"
            iteration_baseline = snapshot(self.cfg.repo_root)
            result = None
            for attempt in range(1, STAGE_OUTPUT_RETRY_MAX + 1):
                build_context = self._context_artifacts_for_stage(
                    active, "build", primary_target, [],
                )
                iteration_protected = _protect_context_inputs(
                    protected_write_paths or [],
                    context_artifacts=build_context,
                    owned_outputs=[primary_target],
                )
                prompt = render_stage_prompt(
                    stage="build",
                    feature=feature,
                    feature_active=active,
                    repo_root=self.cfg.repo_root,
                    primary_target=primary_target,
                    extra_targets=[],
                    context_artifacts=build_context,
                    writable_paths=allowed_write_paths,
                    protected_paths=iteration_protected,
                )
                resume_prompt = render_stage_prompt(
                    stage="build",
                    feature=feature,
                    feature_active=active,
                    repo_root=self.cfg.repo_root,
                    primary_target=primary_target,
                    extra_targets=[],
                    context_artifacts=build_context,
                    writable_paths=allowed_write_paths,
                    protected_paths=iteration_protected,
                    continuation=True,
                )
                pre_snap = snapshot(
                    self.cfg.repo_root,
                    watched_paths=iteration_protected,
                )
                try:
                    result = self._run_stage_subprocess_checked(
                        feature=feature,
                        active=active,
                        stage="build",
                        logger=logger,
                        stage_spec=stage_spec,
                        prompt=prompt,
                        resume_prompt=resume_prompt,
                        primary_target=primary_target,
                        extra_targets=[],
                        allowed_write_paths=allowed_write_paths,
                        pre_snap=pre_snap,
                        protected_write_paths=iteration_protected,
                    )
                    residue = user_visible_changes(
                        iteration_baseline,
                        snapshot(self.cfg.repo_root),
                    )
                    if residue:
                        raise StageOutputInvalid(
                            "build",
                            "uncommitted_product_changes",
                            "Build exited before its product/test changes were "
                            "fully committed. Wait synchronously for git commit "
                            "and all hooks, then return with the iteration's "
                            f"workspace baseline restored. Changes: {residue[:10]}",
                        )
                except StageOutputInvalid as e:
                    self._write_output_rejection_feedback(
                        feedback_path,
                        stage="build",
                        attempt=attempt,
                        kind=e.kind,
                        detail=e.detail,
                        primary_target=primary_target,
                        extra_targets=None,
                        missing_ids=None,
                    )
                    if attempt >= STAGE_OUTPUT_RETRY_MAX:
                        logger.emit(
                            stage="build",
                            event="output-rejected-exhausted",
                            feature=feature,
                            detail={
                                "attempts": attempt,
                                "kind": e.kind,
                                "detail": str(e.detail)[:300],
                            },
                        )
                        raise
                    logger.emit(
                        stage="build",
                        event="output-rejected-retrying",
                        feature=feature,
                        detail={
                            "attempt": attempt,
                            "max": STAGE_OUTPUT_RETRY_MAX,
                            "kind": e.kind,
                            "detail": str(e.detail)[:300],
                        },
                    )
                    continue
                feedback_path.unlink(missing_ok=True)
                break
            if result is None:  # defensive: loop either sets it or raises
                raise PreflightError("build output retry loop produced no result")
            # Stamp current git HEAD into build.json. Without this, a
            # rerun whose LLM-produced build.json is byte-identical to
            # the previous one (same test_results, same files_changed,
            # etc.) hashes the same — so cascade thinks impl-index /
            # spec / panel-close-approval are still fresh and emits
            # pipeline-done while close-approval is needs_revision.
            # Stamping HEAD here makes the per-rerun WIP commit (build
            # prompt's squash-as-you-go) propagate through the cascade.
            self._stamp_build_sealed_ref(primary_target)
            ralph_context = self._finalize_ralph_iteration_context(
                ralph_context_path
            )
            logger.emit(stage="build", event="stage-complete", feature=feature,
                        detail={"artifact": str(primary_target),
                                "elapsed_sec": result.elapsed_sec})
            logger.emit(
                stage="build", event="ralph-diff-context-ready",
                feature=feature, detail={
                    "iteration": ralph_context["iteration"],
                    "before_ref": ralph_context["before_ref"],
                    "after_ref": ralph_context["after_ref"],
                    "previous_review": ralph_context[
                        "previous_ralph_review_path"
                    ],
                },
            )

            route = self._enforce_build_blocking(active, logger, feature)
            if route is not None:
                return AdvanceResult(
                    stage_name="build", success=True,
                    detail=f"routed: {route.reason}",
                )

            review_result = self._run_ralph_review(
                feature, active, logger,
                iteration_context_path=ralph_context_path,
                iteration_context=ralph_context,
            )
            if review_result["complete"]:
                return AdvanceResult(stage_name="build", success=True)

    def _run_ralph_review(
        self, feature: str, active: Path, logger: JsonlLog, *,
        iteration_context_path: Path,
        iteration_context: dict,
    ) -> dict[str, bool]:
        """Dispatch the per-iteration ralph classification, retrying on a
        deficient classification list.

        The reviewer's output is consumed by Python, so an incomplete list
        (an active scope item with no classification) or an unparseable /
        malformed JSON body is a hard schema error. Previously that error
        propagated and killed the whole run. Now the harness re-dispatches
        the SAME ralph-review agent up to ``STAGE_OUTPUT_RETRY_MAX`` times,
        handing it its own prior (rejected) ``ralph-review.json`` plus a
        ``ralph-review-output-rejection.json`` that names exactly which
        scope_ids were missing (or what made the JSON unparseable). The
        prompt instructs the agent to AMEND the prior list — keep the
        classifications it already produced, add/repair only the flagged
        rows — rather than reclassify everything from scratch.

        This retry is within a single build iteration: the code on disk is
        unchanged, the agent simply failed to emit a complete/valid list.
        Only after the cap is exhausted does the schema error propagate.
        """
        from autodev.prompts_loader import render_stage_prompt
        from autodev.state.hashing import hash_file
        from autodev.workspace import snapshot

        review_target = active / "ralph-review.json"
        review_spec = self.cfg.vendors.resolve("review")
        review_writable, review_protected = _stage_write_contract(
            repo_root=self.cfg.repo_root,
            active=active,
            stage="ralph-review",
            primary_target=review_target,
            extra_targets=[],
        )
        active_ids = ralph.active_scope_ids(active / "scope.json")
        feedback_path = active / "ralph-review-output-rejection.json"
        # A fresh iteration's first pass is not an amendment.
        feedback_path.unlink(missing_ok=True)

        invocation_bindings: dict[str, str | Path] = {
            "RALPH_ITERATION_CONTEXT_PATH": iteration_context_path,
            "RALPH_ITERATION_CONTEXT_HASH": hash_file(iteration_context_path),
            "BUILD_BEFORE_REF": iteration_context.get("before_ref") or "unavailable",
            "BUILD_AFTER_REF": iteration_context.get("after_ref") or "unavailable",
        }
        previous_review = iteration_context.get("previous_ralph_review_path")
        if previous_review and Path(previous_review).exists():
            invocation_bindings["PREVIOUS_RALPH_REVIEW_PATH"] = previous_review
            invocation_bindings["PREVIOUS_RALPH_REVIEW_HASH"] = hash_file(
                Path(previous_review)
            )

        statuses: dict[str, str] | None = None
        last_exc: SchemaError | None = None
        for attempt in range(1, STAGE_OUTPUT_RETRY_MAX + 1):
            context_artifacts: list[str] = []
            if attempt > 1:
                if review_target.exists():
                    context_artifacts.append(str(review_target))
                context_artifacts.append(str(feedback_path))
            prompt = render_stage_prompt(
                stage="ralph-review",
                feature=feature,
                feature_active=active,
                repo_root=self.cfg.repo_root,
                primary_target=review_target,
                extra_targets=[],
                context_artifacts=context_artifacts or None,
                invocation_bindings=invocation_bindings,
                writable_paths=review_writable,
                protected_paths=review_protected,
            )
            resume_prompt = render_stage_prompt(
                stage="ralph-review",
                feature=feature,
                feature_active=active,
                repo_root=self.cfg.repo_root,
                primary_target=review_target,
                extra_targets=[],
                context_artifacts=context_artifacts or None,
                invocation_bindings=invocation_bindings,
                writable_paths=review_writable,
                protected_paths=review_protected,
                continuation=True,
            )
            result = self._run_stage_subprocess_checked(
                feature=feature,
                active=active,
                stage="ralph-review",
                logger=logger,
                stage_spec=review_spec,
                prompt=prompt,
                resume_prompt=resume_prompt,
                primary_target=review_target,
                extra_targets=[],
                allowed_write_paths=review_writable,
                pre_snap=snapshot(
                    self.cfg.repo_root,
                    watched_paths=review_protected,
                ),
                protected_write_paths=review_protected,
            )
            logger.emit(stage="ralph-review", event="stage-complete", feature=feature,
                        detail={"artifact": str(review_target),
                                "elapsed_sec": result.elapsed_sec})

            parsed: dict[str, str] = {}
            try:
                parsed = ralph.parse_review_statuses(review_target)
                ralph.validate_active_review_coverage(parsed, active_ids)
            except SchemaError as e:
                last_exc = e
                missing = sorted(active_ids - set(parsed))
                self._write_output_rejection_feedback(
                    feedback_path, stage="ralph-review", attempt=attempt,
                    kind="incomplete_or_malformed_review", detail=str(e),
                    primary_target=review_target, extra_targets=None,
                    missing_ids=missing or None,
                )
                if attempt >= STAGE_OUTPUT_RETRY_MAX:
                    logger.emit(
                        stage="ralph-review", event="output-rejected-exhausted",
                        feature=feature, detail={
                            "attempts": attempt, "error": str(e)[:300],
                        },
                    )
                    raise
                logger.emit(
                    stage="ralph-review", event="output-rejected-retrying",
                    feature=feature, detail={
                        "attempt": attempt, "max": STAGE_OUTPUT_RETRY_MAX,
                        "missing_scope_ids": missing, "error": str(e)[:300],
                    },
                )
                continue
            statuses = parsed
            break

        if statuses is None:  # defensive — loop either set statuses or raised
            raise last_exc or SchemaError("ralph review produced no valid classification")

        # Validation passed — drop the rejection note so a later iteration
        # does not mistake it for live feedback.
        feedback_path.unlink(missing_ok=True)

        state = ralph.load_ralph_state(active)
        state.source = str(active / "scope.json")
        state.source_hash = hash_file(active / "scope.json")
        state.trace_hash = hash_file(active / "trace.md")
        state.test_plan_hash = hash_file(active / "test-plan.md")
        state.design_hash = hash_file(active / "design.md")
        state, _ = ralph.record_iter(state, statuses=statuses)
        ralph.write_ralph_state(active, state)

        complete = ralph.is_complete(state, active_ids)
        logger.emit(stage="ralph-review", event="iteration-recorded",
                    feature=feature, detail={
                        "iter": state.iter,
                        "complete": complete,
                        "fully": sorted(state.fully_history[-1]),
                    })
        return {"complete": complete}

    def _ralph_loop_complete(self, active: Path) -> bool:
        review_path = active / "ralph-review.json"
        if not review_path.exists():
            return False
        try:
            state = ralph.load_ralph_state(active)
            active_ids = ralph.active_scope_ids(active / "scope.json")
            statuses = ralph.parse_review_statuses(review_path)
            ralph.validate_active_review_coverage(statuses, active_ids)
        except Exception:
            return False
        return state.iter > 0 and ralph.is_complete(state, active_ids)

    def _reset_ralph_state_if_inputs_changed(
        self, active: Path, logger: JsonlLog, feature: str,
    ) -> None:
        from autodev.state.hashing import hash_file

        state = ralph.load_ralph_state(active)
        if state.iter == 0:
            return
        current_scope_hash = hash_file(active / "scope.json")
        current_trace_hash = hash_file(active / "trace.md")
        current_test_plan_hash = hash_file(active / "test-plan.md")
        current_design_hash = hash_file(active / "design.md")
        if (
            state.source_hash == current_scope_hash
            and state.trace_hash == current_trace_hash
            and state.test_plan_hash == current_test_plan_hash
            and state.design_hash == current_design_hash
        ):
            return
        ralph.clear_ralph_state(active)
        (active / "ralph-review.json").unlink(missing_ok=True)
        (active / RALPH_PREVIOUS_REVIEW_FILENAME).unlink(missing_ok=True)
        (active / RALPH_ITERATION_CONTEXT_FILENAME).unlink(missing_ok=True)
        logger.emit(stage="build", event="ralph-reset-on-input-change",
                    feature=feature, detail={
                        "scope_hash_changed": state.source_hash != current_scope_hash,
                        "trace_hash_changed": state.trace_hash != current_trace_hash,
                        "test_plan_hash_changed": state.test_plan_hash != current_test_plan_hash,
                        "design_hash_changed": state.design_hash != current_design_hash,
                    })

    # G23: build-blocking halt. stage-implement prompt contracts that a
    # subagent sets top-level `blocking: true` on build.json when a scope
    # item is irreducibly blocked (e.g., trace row has no PRD backing).
    #
    # Phase-5 / g-24 extends this: when any blocking deviation carries a
    # `diagnosis` naming a routable upstream layer (design), the
    # orchestrator invalidates that layer and passes the
    # build.json as panel-feedback so the rerun can respond to evidence
    # directly. If any blocking deviation names `prd` or `ambiguous`, or
    # L/G limits prevent routing, we fall back to the g-23 halt.
    #
    # Returns the RouteDecision when a route was successfully applied
    # (caller should NOT advance to spec); returns None when no blocking
    # deviations were present or no route was applied. Raises
    # GatePending on the fallback halt.
    def _enforce_build_blocking(
        self, active: Path, logger: JsonlLog, feature: str,
    ) -> RouteDecision | None:
        from autodev.artifacts.build import (
            LAYER_CANONICAL, ROUTABLE_LAYERS, load_build,
        )
        from autodev.artifacts.scope import load_scope

        build_path = active / "build.json"
        if not build_path.exists():
            return None
        report = load_build(build_path)
        if not report.blocking:
            return None
        blocking_deviations = [
            (i, d) for i, d in enumerate(report.deviations)
            if d.get("blocking") is True
        ]
        blocking_ids = [d.get("scope_id", "?") for _, d in blocking_deviations]

        # g-24 routing candidates: blocking deviations with a diagnosis.
        diagnosed = [
            (i, d, d["diagnosis"]) for i, d in blocking_deviations
            if isinstance(d.get("diagnosis"), dict)
        ]

        # g-24 scope-id cross-check — applies only to deviations that
        # carry a diagnosis (diagnosis makes the scope_id load-bearing
        # for routing; non-diagnosed legacy blocking deviations keep
        # their pre-g-24 loose semantics).
        if diagnosed:
            scope_path = active / "scope.json"
            known_ids: set[str] = set()
            if scope_path.exists():
                try:
                    sc = load_scope(str(scope_path))
                    known_ids = {item.id for item in sc.in_scope}
                except Exception:
                    known_ids = set()
            unknown = [
                d.get("scope_id")
                for _, d, _ in diagnosed
                if d.get("scope_id") and d.get("scope_id") not in known_ids
            ]
            if unknown:
                raise PreflightError(
                    f"build.json diagnosed deviations reference scope_ids "
                    f"not in scope.json: {unknown}"
                )

        # Unroutable diagnoses (prd / ambiguous) dominate — any one → halt.
        halt_diagnoses = [
            (i, d, dx) for i, d, dx in diagnosed
            if dx.get("defective_layer") in ("prd", "ambiguous")
        ]
        if halt_diagnoses:
            logger.emit(
                stage="build", event="build-blocking-halt-diagnosed",
                feature=feature, detail={
                    "scope_ids": blocking_ids,
                    "halt_layers": [
                        dx.get("defective_layer")
                        for _, _, dx in halt_diagnoses
                    ],
                },
            )
            first_layer = halt_diagnoses[0][2].get("defective_layer")
            ids = ", ".join(blocking_ids) if blocking_ids else "(none listed)"
            raise GatePending(
                "build_blocking",
                f"build.json.blocking=true; diagnosis={first_layer!r} "
                f"is not auto-routable. Scope items: {ids}. "
                f"PRD amendment required via `autodev update`.",
            )

        routable = [
            (i, d, dx) for i, d, dx in diagnosed
            if dx.get("defective_layer") in ROUTABLE_LAYERS
        ]
        if routable:
            i, dev, dx = routable[0]
            layer = dx["defective_layer"]
            trigger_ref = f"build.json#/deviations/{i}"

            decision = route_to_layer(
                active, layer, trigger_ref=trigger_ref,
            )
            if decision.kind == DecisionKind.HALT_FOR_HUMAN:
                logger.emit(
                    stage="build", event="route-halted",
                    feature=feature, detail={
                        "layer": layer, "reason": decision.reason,
                    },
                )
                raise GatePending("build_blocking", decision.reason)

            # Successful route: freeze the selected build diagnostic before
            # invalidation deletes build.json, then point pending_feedback at
            # the durable snapshot (and the richer build challenge log when
            # present).
            feedback_paths = self._snapshot_route_feedback(
                active=active,
                layer=layer,
                trigger_ref=trigger_ref,
                deviation_index=i,
                deviation=dev,
                build_report=report.to_dict(),
            )
            self._apply_route_invalidation(active, layer)
            self._reanchor_route_feedback(active, decision, feedback_paths)

            logger.emit(
                stage="build", event="route-triggered",
                feature=feature, detail={
                    "layer": layer,
                    "scope_id": dev.get("scope_id"),
                    "trigger_ref": trigger_ref,
                    "source": "route",
                    "L_after": decision.state.L.get("design-review", 0)
                    if decision.state else None,
                },
            )
            # Also emit the unified L-bump audit event (g-24c).
            logger.emit(
                stage="revision-loop", event="l-bump",
                feature=feature, detail={
                    "source": "route",
                    "layer": layer,
                    "trigger_ref": trigger_ref,
                },
            )
            return decision

        # No diagnosis on any blocking deviation → fall through to
        # legacy g-23 halt.
        logger.emit(stage="build", event="build-blocking-halt", feature=feature,
                    detail={"scope_ids": blocking_ids,
                            "deviation_count": len(report.deviations)})
        ids = ", ".join(blocking_ids) if blocking_ids else "(none listed)"
        raise GatePending(
            "build_blocking",
            f"build.json.blocking=true; PRD amendment required for "
            f"scope items: {ids}",
        )

    def _apply_route_invalidation(self, active: Path, layer: str) -> None:
        """g-24: delete the artifacts of the target layer so the
        StalenessCascade will re-run its producer stage next."""
        if layer == "design":
            (active / "design.md").unlink(missing_ok=True)
            (active / "scope.json").unlink(missing_ok=True)
            (active / "trace.md").unlink(missing_ok=True)
            (active / "test-plan.md").unlink(missing_ok=True)
            (active / "design-packet.json").unlink(missing_ok=True)
            (active / "panel-design-review.json").unlink(missing_ok=True)
            (active / "panel-trace-review.json").unlink(missing_ok=True)
            (active / "accepted-design.json").unlink(missing_ok=True)
        else:
            raise PreflightError(f"cannot invalidate unknown layer {layer!r}")
        # Also drop downstream artifacts so the pipeline reruns
        # them clean. StalenessCascade would catch these via hash
        # mismatch on next pass, but proactive cleanup prevents the
        # "stale downstream" warning churn.
        for downstream in (
            "build.json", "implementation-index.json", "implemented-spec.md",
            "prd-checklist.json", "ralph-review.json", "panel-close-approval.json",
            RALPH_PREVIOUS_REVIEW_FILENAME, RALPH_ITERATION_CONTEXT_FILENAME,
        ):
            (active / downstream).unlink(missing_ok=True)
        # Also drop ralph-state since upstream changed.
        (active / "ralph-state.json").unlink(missing_ok=True)
        # A routed design rerun is a structural rework dispatch the
        # rework-mode selector never saw — a stale `patch` mode from an
        # earlier gate verdict must not constrain it.
        (active / "rework-mode.json").unlink(missing_ok=True)

    def _snapshot_route_feedback(
        self,
        *,
        active: Path,
        layer: str,
        trigger_ref: str,
        deviation_index: int,
        deviation: dict,
        build_report: dict,
    ) -> list[Path]:
        """Persist a route diagnostic outside the invalidation cascade."""
        from autodev.state.atomic import atomic_write_json

        feedback_path = active / ROUTE_FEEDBACK_FILENAME
        atomic_write_json(feedback_path, {
            "kind": "build-route-feedback",
            "target_layer": layer,
            "target_stage": "design" if layer == "design" else layer,
            "trigger_ref": trigger_ref,
            "deviation_index": deviation_index,
            "scope_id": deviation.get("scope_id"),
            "deviation": deviation,
            "build_report": build_report,
            "instruction": (
                "Revise the routed layer to resolve this blocking build "
                "diagnostic. Preserve requirements and artifacts that are "
                "unrelated to the selected deviation."
            ),
        })
        paths = [feedback_path]
        challenges = active / BUILD_CHALLENGES_FILENAME
        if challenges.exists():
            paths.append(challenges)
        return paths

    def _reanchor_route_feedback(
        self, active: Path, decision: RouteDecision, feedback_paths: list[Path],
    ) -> None:
        """Replace the symbolic trigger with durable, readable feedback."""
        s = load_state(active)
        if decision.stage_to_rerun is None:
            return
        rendered: list[str] = []
        for path in feedback_paths:
            try:
                rendered.append(str(path.resolve().relative_to(active.resolve())))
            except ValueError:
                rendered.append(str(path))
        s.pending_feedback[decision.stage_to_rerun] = rendered
        write_state(active, s)
