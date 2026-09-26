"""human-feedback-<point>.json — mechanical injection of human review
feedback into a review point's response/synthesis package (core R4-R8,
detail §1-§2).

One module owns all injection logic; ``autodev.orchestrator`` and
``autodev.cli`` (stage B) only call it. This module must NOT import
``autodev.orchestrator`` at top level — orchestrator imports this module,
so a top-level cycle would result. Where this module needs something
orchestrator owns (``_POST_BUILD_STAGES``), it imports lazily inside the
function that needs it.

Mechanism, in one line: a review point's stage/panel writes its package
as usual -> harness mechanically merges the human's finding(s) into that
already-written package -> everything downstream reads the merged file.
No agent is re-run and no prompt is touched by this module.

The input model (``HumanFeedback``) and validation (``validate_feedback``
and its family-specific helpers) live in ``autodev.human_feedback_validate``
-- split out to keep this file under the repo's ~500-line guideline --
and are re-exported here so callers only need this module.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from autodev import overrides_api as ov
from autodev import ralph
from autodev.artifacts.arch_review import ArchReview, load_arch_review
from autodev.artifacts.verdict import (
    IssueCluster,
    PanelFinding,
    PanelVerdict,
    _load_targets,
    load_verdict,
    write_verdict,
)
from autodev.errors import SchemaError
from autodev.human_feedback_validate import (  # noqa: F401 -- re-exported
    HumanFeedback,
    PANEL_POINTS,
    REVIEW_POINTS,
    validate_feedback,
    _feedback_path,
)
from autodev.state.atomic import atomic_write_json
from autodev.state.cascade import StalenessCascade
from autodev.state.log import JsonlLog


# ---------------------------------------------------------------------
# §1.1 / §1.3 — load / write / consume
# ---------------------------------------------------------------------


def load_feedback(active: Path, point: str) -> HumanFeedback | None:
    """Load the feedback file for ``point`` regardless of status. Looks
    up only the one fixed filename -- never globs, so an archived
    (renamed) file is never picked up (detail §1.1)."""
    path = _feedback_path(Path(active), point)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return HumanFeedback.from_dict(raw)


def load_pending(active: Path, point: str) -> HumanFeedback | None:
    fb = load_feedback(active, point)
    if fb is not None and fb.status == "pending":
        return fb
    return None


def write_feedback(active: Path, fb: HumanFeedback) -> Path:
    """Write the feedback file, applying detail §1.1's overwrite/rename
    rule: a pending predecessor is overwritten in place; a resolved
    (consumed or rejected) predecessor is archived under its OWN
    feedback_id (no colons -- feedback_id is already a filename-safe
    ``hf-<timestamp>-<hex>`` string) so it survives for audit and never
    collides with a same-second successor."""
    active = Path(active)
    target = _feedback_path(active, fb.review_point)
    existing = load_feedback(active, fb.review_point)
    if existing is not None and existing.status != "pending" and target.exists():
        archived = active / (
            f"human-feedback-{fb.review_point}.{existing.feedback_id}.json"
        )
        target.rename(archived)
    atomic_write_json(target, fb.to_dict())
    return target


def mark_consumed(
    active: Path, point: str, *, into: Path, log: JsonlLog,
) -> None:
    active = Path(active)
    path = _feedback_path(active, point)
    raw = json.loads(path.read_text(encoding="utf-8"))
    consumed_at = datetime.now(timezone.utc).isoformat()
    raw["status"] = "consumed"
    raw["consumed_at"] = consumed_at
    raw["consumed_into"] = str(into)
    atomic_write_json(path, raw)
    log.emit(
        stage="human-feedback", event="human-feedback-consumed",
        feature=active.parent.name,
        detail={"review_point": point, "consumed_at": consumed_at},
    )


def _reject(
    active: Path, point: str, *, reason: str, log: JsonlLog,
) -> None:
    active = Path(active)
    path = _feedback_path(active, point)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["status"] = "rejected"
    raw["rejected_reason"] = reason
    atomic_write_json(path, raw)
    log.emit(
        stage="human-feedback", event="human-feedback-rejected",
        feature=active.parent.name,
        detail={"review_point": point, "reason": reason},
    )


# ---------------------------------------------------------------------
# §1.4 — panel merge
# ---------------------------------------------------------------------


def _next_human_number(findings: list[PanelFinding]) -> int:
    max_n = 0
    for f in findings:
        if f.finding_id and f.finding_id.startswith("human:"):
            suffix = f.finding_id.split(":", 1)[1]
            if suffix.isdigit():
                max_n = max(max_n, int(suffix))
    return max_n + 1


def merge_into_panel_verdict(
    v: PanelVerdict, fb: HumanFeedback, *, active: Path, point: str,
) -> tuple[PanelVerdict, dict | None]:
    """Mechanically fold ``fb``'s findings into an in-memory
    ``PanelVerdict`` (core R6, detail §1.4). Caller persists via
    ``write_verdict`` (which re-validates before writing).

    Returns ``(verdict, override_event_detail)``. ``override_event_detail``
    is the ``release-policy-decision-override`` event detail when the
    human finding overrode a canonical ``pass`` decision, else ``None``.
    It is a return value rather than an attribute stashed on the verdict
    (detail §10, closing-review pin) so the event's payload travels
    through a typed, self-contained channel; the event must only fire
    once the merged verdict has actually been written to disk
    (``apply_pending`` emits it, via the ``log`` it was given, right
    after ``write_verdict`` succeeds)."""
    active = Path(active)
    next_n = _next_human_number(v.findings)
    new_findings: list[PanelFinding] = []
    for offset, raw in enumerate(fb.findings):
        finding_id = f"human:{next_n + offset}"
        span = dict(raw.get("cited_artifact_span") or {})
        span["human_feedback_id"] = fb.feedback_id
        new_findings.append(PanelFinding(
            severity=raw["severity"], vendor="human", summary=raw["summary"],
            cited_artifact_span=span, targets=_load_targets(raw),
            category=raw.get("category"),
            evidence_refs=[str(x) for x in raw.get("evidence_refs", [])],
            failure_class=raw.get("failure_class"),
            missized_direction=raw.get("missized_direction"),
            severity_reported=raw.get("severity_reported"),
            priority=raw.get("priority"),
            finding_id=finding_id,
        ))

    # Same rigor treatment as every other reviewer's findings (core R6;
    # anchor filtering already happened as a validate-time precheck, so
    # it is not repeated here).
    from autodev.panel.runner import _apply_rigor_to_findings
    _apply_rigor_to_findings(new_findings, feature_active=active)

    v.findings = list(v.findings) + new_findings
    new_clusters: list[IssueCluster] = []
    for pf in new_findings:
        identity = json.dumps(
            {
                "summary": f"{point}: {pf.summary.strip().lower()}",
                "members": [pf.finding_id],
            },
            sort_keys=True, separators=(",", ":"),
        )
        cluster_id = "issue-" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:16]
        new_clusters.append(IssueCluster(
            cluster_id=cluster_id, finding_ids=[pf.finding_id],
            summary=pf.summary, priority=pf.effective_priority(),
        ))
    v.issue_clusters = list(v.issue_clusters) + new_clusters

    v.per_vendor_raw = dict(v.per_vendor_raw)
    v.per_vendor_raw[f"human:{fb.feedback_id}"] = json.dumps(
        {"verdict": fb.verdict, "findings": fb.findings}, ensure_ascii=False,
    )

    # Total verdict recomputed from findings ONLY (core R6; §5 -- no
    # weighting, no veto from the human's own reported verdict).
    from autodev.panel.rigor_filter import has_effective_blocking
    blocks = has_effective_blocking(v.findings, v.release_threshold)
    override_detail: dict | None = None
    if v.decision is None:
        if blocks and v.verdict != "fail":
            v.verdict = "needs_revision"
        # not blocks -> v.verdict unchanged; fail stays fail.
    elif blocks and v.decision.outcome == "pass":
        from autodev.panel.runner import override_pass_decision
        prior = v.decision_overridden_by_policy
        new_decision, overridden = override_pass_decision(
            v.decision, v.release_threshold,
            reason="human finding overrides pass decision",
        )
        if prior is not None:
            overridden["prior"] = prior
        v.decision = new_decision
        v.decision_overridden_by_policy = overridden
        v.verdict = "needs_revision"
        override_detail = {
            "gate": v.gate, "from_outcome": "pass",
            "to_outcome": "retry_design",
            "release_threshold": v.release_threshold,
            "source": "human-feedback",
        }
    # else: decision present but not (blocks and outcome=="pass") ->
    # decision and verdict are left untouched (detail §1.4).
    return v, override_detail


# ---------------------------------------------------------------------
# §1.5 — arch-review merge
# ---------------------------------------------------------------------


def merge_into_arch_review(
    path: Path, fb: HumanFeedback, *, arch_design_path: Path,
) -> ArchReview:
    from autodev.artifacts.arch_review import _validate as _validate_arch_review

    path = Path(path)
    arch_design_path = Path(arch_design_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    findings = list(raw.get("findings", []))
    for f in fb.findings:
        entry = dict(f)
        entry["vendor"] = "human"
        entry["human_feedback_id"] = fb.feedback_id
        findings.append(entry)
    raw["findings"] = findings
    raw["verdict"] = "needs_revision"
    raw["written"] = datetime.now(timezone.utc).isoformat()

    _validate_arch_review(raw)
    from autodev.state.hashing import hash_file
    current_hash = hash_file(arch_design_path)
    if raw.get("source_hash") != current_hash:
        raise SchemaError(
            "arch-review.json: source_hash no longer matches "
            "arch-design.md -- stale for human-feedback merge"
        )
    atomic_write_json(path, raw)
    return load_arch_review(path, arch_design_path)


# ---------------------------------------------------------------------
# §1.6 — ralph-review merge
# ---------------------------------------------------------------------


def merge_into_ralph_review(
    path: Path, fb: HumanFeedback, *, active: Path,
) -> dict[str, str]:
    path = Path(path)
    active = Path(active)
    raw = json.loads(path.read_text(encoding="utf-8"))
    conformance = dict(raw.get("design_conformance", {}))
    findings = list(conformance.get("findings", []))
    for f in fb.findings:
        entry = dict(f)
        entry["vendor"] = "human"
        entry["human_feedback_id"] = fb.feedback_id
        findings.append(entry)
    conformance["findings"] = findings
    conformance["verdict"] = "Deviated"
    raw["design_conformance"] = conformance

    statuses = ralph._parse_ralph_review_json(raw)
    active_ids = ralph.active_scope_ids(active / "scope.json")
    ralph.validate_active_review_coverage(statuses, active_ids)

    atomic_write_json(path, raw)
    return statuses


# ---------------------------------------------------------------------
# §2.1 — "current" judgment per point family
# ---------------------------------------------------------------------


def _panel_target_path(active: Path, point: str) -> Path:
    return Path(active) / f"panel-{point}.json"


def _panel_current(active: Path, point: str) -> tuple[bool, str | None]:
    cascade_node = (
        "panel_close_approval" if point == "close-approval"
        else "panel_design_review"
    )
    fresh = StalenessCascade(active).fresh()
    if not fresh.get(cascade_node, False):
        return False, "not-current"
    try:
        v = load_verdict(_panel_target_path(active, point))
    except (SchemaError, OSError, json.JSONDecodeError, KeyError):
        return False, "not-current"
    # Skip-gate-override checked BEFORE the raw verdict==skipped check
    # (detail §10, closing-review pin): when an override is genuinely
    # active, the skip-gate path is what produced the synthetic
    # verdict=="skipped" in the first place, so the override is the
    # attributable cause and must win the reason even though both
    # conditions are true together. Only a verdict=="skipped" with NO
    # active override (e.g. the override has since been resolved) falls
    # through to the "skipped-verdict" reason.
    skip_gate_name = "close-approval" if point == "close-approval" else "design-review"
    if ov.load(active).has_active_skip_gate(skip_gate_name):
        return False, "skip-gate-override"
    if v.verdict == "skipped":
        return False, "skipped-verdict"
    return True, None


def _arch_review_current(active: Path) -> tuple[bool, str | None]:
    active = Path(active)
    path = active / "arch-review.json"
    arch_design_path = active / "arch-design.md"
    if not path.exists():
        return False, "not-current"
    try:
        load_arch_review(path, arch_design_path)
    except (SchemaError, OSError, json.JSONDecodeError):
        return False, "not-current"
    return True, None


def _ralph_review_current(active: Path) -> tuple[bool, str | None]:
    active = Path(active)
    review_path = active / "ralph-review.json"
    scope_path = active / "scope.json"
    state_file = ralph.state_path(active)
    if not (review_path.exists() and state_file.exists() and scope_path.exists()):
        return False, "not-current"
    state = ralph.load_ralph_state(active)
    if state.iter <= 0:
        return False, "not-current"

    # _POST_BUILD_STAGES and RALPH_ITERATION_CONTEXT_FILENAME are
    # orchestrator-owned; lazy import avoids the module-level import
    # cycle (orchestrator imports human_feedback).
    from autodev.orchestrator import Orchestrator, RALPH_ITERATION_CONTEXT_FILENAME
    not_done = ("build",) + Orchestrator._POST_BUILD_STAGES
    if StalenessCascade(active).next_stage() not in not_done:
        return False, "pipeline-done"

    context_path = active / RALPH_ITERATION_CONTEXT_FILENAME
    if context_path.exists():
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            context = None
        if isinstance(context, dict) and context.get("iteration") == state.iter + 1:
            # A build has landed for the NEXT iteration but that
            # iteration's review has not run yet -- merging now would
            # rewrite history for a round already superseded on disk.
            return False, "not-current"
    return True, None


def _is_current(active: Path, point: str) -> tuple[bool, str | None]:
    """Detail §2.1's "current" judgment per point family. Returns
    ``(is_current, reason)``; ``reason`` is only meaningful when
    ``is_current`` is False, and is one of ``"skipped-verdict"``,
    ``"skip-gate-override"``, ``"pipeline-done"`` or the generic
    ``"not-current"`` (detail §10, closing-review pin). ``cli.py``'s
    ``_pending_feedback_message`` maps these reasons to user-facing
    hints rather than re-deriving them."""
    if point in PANEL_POINTS:
        return _panel_current(active, point)
    if point == "arch-review":
        return _arch_review_current(active)
    if point == "ralph-review":
        return _ralph_review_current(active)
    return False, "not-current"


# ---------------------------------------------------------------------
# idempotency guard -- detect a finding already merged for this feedback_id
# ---------------------------------------------------------------------


def _already_merged(path: Path, point: str, feedback_id: str) -> bool:
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if point in PANEL_POINTS:
        findings = raw.get("findings", [])
        return any(
            isinstance(f, dict)
            and f.get("cited_artifact_span", {}).get("human_feedback_id") == feedback_id
            for f in findings
        )
    if point == "arch-review":
        findings = raw.get("findings", [])
        return any(
            isinstance(f, dict) and f.get("human_feedback_id") == feedback_id
            for f in findings
        )
    if point == "ralph-review":
        findings = raw.get("design_conformance", {}).get("findings", [])
        return any(
            isinstance(f, dict) and f.get("human_feedback_id") == feedback_id
            for f in findings
        )
    return False


def _recompute_ralph_state(active: Path, statuses: dict[str, str]) -> None:
    """Recompute the most recent iter's statuses/fully/regressions after
    an at-rest ``ralph-review.json`` was merged (detail §1.6/§2.1 D5, §10
    d10): the recorded iter must read as though the agent had written this
    finding itself. Rather than hand-copying ``record_iter``'s regression
    rule, pop the last iter off the history and replay it through
    ``ralph.record_iter`` so "same rule" is guaranteed by construction.
    Net effect is identical to before except ``last_iter_at`` refreshes;
    ``iter`` ends up unchanged (popped, then re-appended)."""
    active = Path(active)
    state = ralph.load_ralph_state(active)
    state.statuses_history.pop()
    state.fully_history.pop()
    state.regressions = [
        r for r in state.regressions if r.iter_index != state.iter
    ]
    state.iter -= 1
    ralph.record_iter(state, statuses=statuses)
    ralph.write_ralph_state(active, state)


# ---------------------------------------------------------------------
# §1.3 — the single entry point
# ---------------------------------------------------------------------


def apply_pending(
    active: Path, point: str, *, log: JsonlLog, check_current: bool,
) -> bool:
    """Merge a pending human-feedback file for ``point`` into its
    target package, if there is one to merge.

    ``check_current=True`` -- called from the feedback verb and from
    the run-loop/`_advance_one` entry (detail §2.1, timings A/A'): only
    merges when the target package is judged "current" for this point
    (see ``_is_current``), so the merge cannot be clobbered by the
    harness re-entering the gate on its next pass.

    ``check_current=False`` -- called from the three producer hooks
    (detail §2.2, timing B) immediately after the harness validates a
    freshly produced package: merges unconditionally (the package is
    current by construction).

    Idempotent: if the target file already carries a finding tagged
    with this feedback's ``feedback_id`` (a prior run wrote the merge
    but crashed before ``mark_consumed``), only ``mark_consumed`` runs.

    Returns True iff a merge (or the idempotent mark_consumed) happened.
    """
    active = Path(active)
    if point not in REVIEW_POINTS:
        return False
    fb = load_pending(active, point)
    if fb is None:
        return False

    if point in PANEL_POINTS:
        target_path = _panel_target_path(active, point)
    elif point == "arch-review":
        target_path = active / "arch-review.json"
    else:  # ralph-review
        target_path = active / "ralph-review.json"

    if check_current:
        current, _reason = _is_current(active, point)
        if not current:
            return False

    if _already_merged(target_path, point, fb.feedback_id):
        mark_consumed(active, point, into=target_path, log=log)
        return True

    try:
        if point in PANEL_POINTS:
            v_before = load_verdict(target_path)
            verdict_before = v_before.verdict
            v_after, override_detail = merge_into_panel_verdict(
                v_before, fb, active=active, point=point,
            )
            write_verdict(target_path, v_after)
            if override_detail is not None:
                log.emit(
                    stage="gate", event="release-policy-decision-override",
                    feature=active.parent.name, detail=override_detail,
                )
            verdict_after = v_after.verdict
        elif point == "arch-review":
            verdict_before = json.loads(
                target_path.read_text(encoding="utf-8")
            ).get("verdict")
            review = merge_into_arch_review(
                target_path, fb, arch_design_path=active / "arch-design.md",
            )
            verdict_after = review.verdict
        else:  # ralph-review
            verdict_before = json.loads(
                target_path.read_text(encoding="utf-8")
            ).get("design_conformance", {}).get("verdict")
            statuses = merge_into_ralph_review(target_path, fb, active=active)
            verdict_after = "Deviated"
            if check_current:
                # Timing B (hook, check_current=False) merges just
                # before the caller's own record_iter, which will pick
                # up the reread statuses naturally -- no separate
                # recompute needed there (detail §2.1/§2.2).
                _recompute_ralph_state(active, statuses)
    except SchemaError as exc:
        _reject(active, point, reason=str(exc), log=log)
        return False

    mark_consumed(active, point, into=target_path, log=log)
    log.emit(
        stage="human-feedback", event="human-feedback-merged",
        feature=active.parent.name,
        detail={
            "review_point": point, "into": str(target_path),
            "finding_count": len(fb.findings),
            "verdict_before": verdict_before, "verdict_after": verdict_after,
        },
    )
    return True
