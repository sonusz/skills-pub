"""Pipeline orchestrator — wires stages, subagents, and gates.

Drives `implement`, `update`, `close` through filesystem-as-state. The
orchestrator does NOT loop on the model; each stage is a single function
call that either completes (artifact on disk) or raises.
"""
from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from auto_dev.errors import (
    GatePending,
    LockConflict,
    PipelineError,
    StaleInputs,
)
from auto_dev.executor.permissions import PermissionRules
from auto_dev.preflight import preflight
from auto_dev.stages import gates
from auto_dev.stages.close import close_feature, VALID_REASONS
from auto_dev.stages.paths import FeaturePaths, STATUS_DIRS
from auto_dev.stages.prd import check_completeness, ensure_prd
from auto_dev.state.cascade import StalenessCascade
from auto_dev.state.lock import Lock, read_owner
from auto_dev.state.log import JsonlLog
from auto_dev.subagents.implement import run_implement
from auto_dev.subagents.plan import run_plan
from auto_dev.subagents.prd_review import run_prd_review
from auto_dev.subagents.review import run_review
from auto_dev.subagents.spec import run_spec
from auto_dev.subagents.runner import SubagentError
from auto_dev.vendors.config import VendorsConfig


# ---------------------------------------------------------------------------
# Config


@dataclass
class OrchestratorConfig:
    repo_root: Path
    vendors: VendorsConfig
    prd_review_mode: str = "none"  # none | subagent | panel
    yes: bool = False
    allow_cmd: list[str] = field(default_factory=list)
    deny_cmd: list[str] = field(default_factory=list)
    session_id: str = ""

    def __post_init__(self) -> None:
        if not self.session_id:
            self.session_id = f"cli-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Core orchestrator


class Orchestrator:
    def __init__(self, cfg: OrchestratorConfig) -> None:
        self.cfg = cfg

    # --- helpers ------------------------------------------------------

    def feature_paths(self, feature: str) -> FeaturePaths:
        return FeaturePaths(repo_root=self.cfg.repo_root, feature=feature)

    def log(self, feature: str) -> JsonlLog:
        return JsonlLog(self.feature_paths(feature).log_path())

    # --- verbs --------------------------------------------------------

    def implement(
        self,
        feature: str,
        *,
        prd_from_file: Path | None = None,
        spec_skill: Callable | None = None,
    ) -> dict:
        fp = self.feature_paths(feature)
        active = fp.active()
        self._promote_to_active(fp, prd_from_file=prd_from_file)
        active.mkdir(parents=True, exist_ok=True)
        logger = self.log(feature)

        with self._lock(active, verb="implement"):
            logger.emit(stage="orchestrator", event="start", feature=feature,
                        detail={"session_id": self.cfg.session_id})

            # Preflight (lock is ours now; orphan .tmp + cascade matter).
            pre = preflight(active, reject_on_lock=False)
            logger.emit(stage="preflight", event="report", feature=feature,
                        detail={"orphans": pre.orphan_tmps, "stale": pre.stale_artifacts,
                                "next_stage": pre.next_stage})

            # PRD stage.
            self._prd_stage(feature, active, logger)

            # Scope stage (must pre-exist; the SDK does not auto-generate scope).
            scope_path = active / "scope.json"
            if not scope_path.exists():
                raise PipelineError(
                    "scope.json not found. Run the scope subagent or author scope.json "
                    "manually from prd.md (schema at docs/scope-schema.md)."
                )

            # Plan.
            if self._needs("plan", active):
                result = run_plan(
                    feature=feature,
                    feature_root=active,
                    scope_path=scope_path,
                    stage_spec=self.cfg.vendors.resolve("plan"),
                )
                logger.emit(stage="plan", event="done", feature=feature, detail=result)

            # Build.
            if self._needs("build", active):
                trace_path = active / "trace.md"
                test_plan_path = active / "test-plan.md"
                try:
                    result = run_implement(
                        feature=feature,
                        feature_root=active,
                        scope_path=scope_path,
                        trace_path=trace_path,
                        test_plan_path=test_plan_path,
                        stage_spec=self.cfg.vendors.resolve("implement"),
                        allow_cmd=self.cfg.allow_cmd,
                        deny_cmd=self.cfg.deny_cmd,
                        repo_root=self.cfg.repo_root,
                    )
                except SubagentError as e:
                    logger.emit(stage="build", event="halt", feature=feature,
                                detail={"type": e.type, "detail": e.detail, "affected": e.affected_items})
                    raise PipelineError(f"build halted: {e}") from e
                logger.emit(stage="build", event="done", feature=feature, detail=result)
                if result["blocking"]:
                    logger.emit(stage="build", event="blocking-deviations", feature=feature,
                                detail={"deviations": result["deviations"]})
                    raise PipelineError(
                        "build completed with blocking deviations; run `update` to amend PRD"
                    )

            # Spec.
            if self._needs("spec", active):
                result = run_spec(
                    feature=feature,
                    feature_root=active,
                    scope_path=scope_path,
                    stage_spec=self.cfg.vendors.resolve("spec"),
                    skill_invoker=spec_skill,
                )
                logger.emit(stage="spec", event="done", feature=feature, detail=result)

            # Review.
            if self._needs("review", active):
                result = run_review(
                    feature=feature,
                    feature_root=active,
                    scope_path=scope_path,
                    prd_path=active / "prd.md",
                    spec_path=active / "spec.md",
                    trace_path=active / "trace.md",
                    stage_spec=self.cfg.vendors.resolve("review"),
                )
                logger.emit(stage="review", event="done", feature=feature, detail=result)

            # Ready to close — surface close-approval gate.
            gates.request(active, "close-approval",
                          detail="pipeline complete; user must `approve close-approval` or `close`")
            logger.emit(stage="orchestrator", event="ready-to-close", feature=feature, detail={})
            if not self.cfg.yes:
                raise GatePending("close-approval", "pipeline complete; run `approve` or `close`")

        return {"status": "ready-to-close", "feature": feature, "active": str(active)}

    def update(self, feature: str, *, amendment_text: str) -> dict:
        fp = self.feature_paths(feature)
        current = fp.current_status()
        if current is None:
            raise PipelineError(f"feature {feature!r} not found")
        if current == "cancelled":
            raise PipelineError("cancelled features cannot be updated")
        if current != "active":
            # Reopen: move back to active/.
            src = fp.status_dir(current)
            dst = fp.active()
            shutil.move(str(src), str(dst))
        active = fp.active()

        with self._lock(active, verb="update"):
            logger = self.log(feature)
            prd = active / "prd.md"
            if not prd.exists():
                raise PipelineError("prd.md missing; cannot append amendment")
            existing = prd.read_text(encoding="utf-8")
            from datetime import date

            stamped = f"\n\n## Amendment {date.today().isoformat()}\n\n{amendment_text.strip()}\n"
            from auto_dev.state.atomic import atomic_write

            atomic_write(prd, existing + stamped)
            logger.emit(stage="update", event="prd-amended", feature=feature,
                        detail={"date": date.today().isoformat()})

            # Staleness cascade will pick up the PRD change on the next run.
            # Request prd-review gate so user re-approves.
            gates.request(active, "prd-review", detail="PRD amended; re-approve before scope")

        return {"status": "pending-prd-review", "feature": feature}

    def close(self, feature: str, reason: str, *, cancel_note: str = "") -> dict:
        if reason not in VALID_REASONS:
            raise ValueError(f"invalid reason: {reason!r}")
        fp = self.feature_paths(feature)
        if fp.current_status() != "active":
            raise PipelineError(f"feature not in active/; current = {fp.current_status()}")
        active = fp.active()
        if not self.cfg.yes and not gates.is_approved(active, "close-approval"):
            raise GatePending(
                "close-approval",
                f"run `approve {feature} close-approval` first, or use --yes",
            )
        logger = self.log(feature)
        logger.emit(stage="close", event="start", feature=feature, detail={"reason": reason})
        result = close_feature(fp.base, reason, cancel_note=cancel_note)
        # Reopen logger since `active/` moved; no-op if log rolled with it.
        return {"status": reason, "feature": feature, "new_path": str(result.new_path)}

    # --- internals ----------------------------------------------------

    def _promote_to_active(
        self,
        fp: FeaturePaths,
        *,
        prd_from_file: Path | None,
    ) -> None:
        fp.base.mkdir(parents=True, exist_ok=True)
        current = fp.current_status()
        if current == "active":
            if prd_from_file:
                ensure_prd(fp.active(), from_file=prd_from_file)
            return
        if current is None:
            fp.active().mkdir(parents=True, exist_ok=True)
            if prd_from_file:
                ensure_prd(fp.active(), from_file=prd_from_file)
            return
        if current == "planned":
            shutil.move(str(fp.status_dir("planned")), str(fp.active()))
            if prd_from_file:
                ensure_prd(fp.active(), from_file=prd_from_file)
            return
        if current in ("complete", "retiring", "deferred"):
            shutil.move(str(fp.status_dir(current)), str(fp.active()))
            if prd_from_file:
                ensure_prd(fp.active(), from_file=prd_from_file)
            return
        raise PipelineError(f"cannot promote {current!r} to active")

    def _prd_stage(self, feature: str, active: Path, logger: JsonlLog) -> None:
        prd = active / "prd.md"
        if not prd.exists():
            raise PipelineError(
                "prd.md not present in active/. Run `auto-dev prd <feature>` first, "
                "or pass `--from-file` to `implement`."
            )
        completeness = check_completeness(prd)
        logger.emit(
            stage="prd",
            event="completeness",
            feature=feature,
            detail={"complete": completeness.complete, "missing": completeness.missing},
        )
        if not completeness.complete:
            raise PipelineError(
                f"prd.md missing sections: {completeness.missing}; fix before proceeding"
            )

        # PRD review mode.
        mode = self.cfg.prd_review_mode
        if mode == "subagent":
            result = run_prd_review(
                feature=feature,
                feature_root=active,
                prd_path=prd,
                stage_spec=self.cfg.vendors.resolve("prd_review"),
            )
            logger.emit(stage="prd_review", event="done-subagent", feature=feature, detail=result)
        elif mode == "panel":
            from auto_dev.skills.panel_review import invoke_panel_review

            result = invoke_panel_review(feature=feature, feature_root=active, prd_path=prd)
            logger.emit(stage="prd_review", event="done-panel", feature=feature, detail=result)

        # PRD approval gate (blocking in `none` mode too).
        if not gates.is_approved(active, "prd-review"):
            gates.request(active, "prd-review", detail=f"prd-review mode={mode}")
            if not self.cfg.yes:
                raise GatePending("prd-review", "run `approve <feature> prd-review` to continue")

    def _needs(self, stage: str, active: Path) -> bool:
        cascade = StalenessCascade(active)
        fresh = cascade.fresh()
        artifact_map = {
            "plan": ("trace", "test_plan"),
            "build": ("build",),
            "spec": ("spec",),
            "review": ("review",),
        }
        return not all(fresh[a] for a in artifact_map[stage])

    def _lock(self, active: Path, *, verb: str) -> Lock:
        """Return a Lock ready for use as a context manager.

        Acquisition happens in `__enter__`; if the lock is already held by
        another owner, LockConflict is raised on entry and bubbles up.
        """
        return Lock(active, session_id=self.cfg.session_id, verb=verb)


# ---------------------------------------------------------------------------
# Status / resume / abort helpers


def status(repo_root: Path, feature: str) -> dict:
    fp = FeaturePaths(repo_root=repo_root, feature=feature)
    current = fp.current_status()
    if current is None:
        return {"feature": feature, "exists": False}
    active_like = fp.status_dir(current)
    cascade = StalenessCascade(active_like) if current == "active" else None
    next_stage = cascade.next_stage() if cascade else "—"
    stale = cascade.stale() if cascade else []
    owner = read_owner(active_like) if current == "active" else None
    pending = gates.list_pending(active_like) if current == "active" else []
    log = JsonlLog(active_like / "log.jsonl")
    tail = log.tail(5)
    return {
        "feature": feature,
        "exists": True,
        "status": current,
        "next_stage": next_stage,
        "stale_artifacts": stale,
        "lock_owner": owner,
        "pending_gates": pending,
        "recent_events": tail,
    }


def abort(repo_root: Path, feature: str) -> dict:
    """Record an interrupted.json — used by outer supervisor to signal stop."""
    fp = FeaturePaths(repo_root=repo_root, feature=feature)
    active = fp.active()
    if not active.is_dir():
        return {"feature": feature, "aborted": False, "reason": "no active/"}
    from datetime import datetime, timezone

    from auto_dev.state.atomic import atomic_write_json

    atomic_write_json(
        active / "interrupted.json",
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": "user abort via CLI",
        },
    )
    # Release lock so resume can re-acquire.
    lock = Lock(active, session_id="abort", verb="abort")
    lock.release()
    return {"feature": feature, "aborted": True}
