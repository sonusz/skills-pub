"""Revision loop.

Every blocking panel verdict either:

1. Dispatches a producer rerun (bumping L[gate] by 1), or
2. Halts for human decision (not rerunnable, mixed producers,
   indeterminate, repeated PRD target, or L[gate] already at L_MAX).

Blocking = at least one finding in ``findings[]`` (after anchor-filter)
has severity ``invariant_violation`` or ``risk``. ``opinion``-only
verdicts pass.

Dispatch is driven by the filename-qualified targets the reviewers
emitted on blocking findings (``primary_pair.<filename>`` or
``anchor.<filename>``). Filename → producer mapping lives in
``FILENAME_TO_PRODUCER`` (in revision_state). A design-review finding
that targets ``prd.md`` gets one design rerun before human halt.

Per-gate cap ``L_MAX`` (currently 10) applies uniformly to all tracked
gates. Total panel runs per gate per cycle: 1 initial + up to ``L_MAX``
reruns = ``L_MAX + 1`` panel runs. The ``(L_MAX + 1)``th blocking verdict
halts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from autodev.artifacts.revision_state import (
    ALL_PANEL_GATES, GATE_FALLBACK_PRODUCER, L_MAX,
    PRD_TARGET_HALT_STREAK,
    RevisionState, consume_manual_rerun_credit, filename_to_producer,
    gate_for_layer, load_state,
    producer_stage_for_layer, write_state,
)
from autodev.artifacts.verdict import PanelVerdict


class DecisionKind(str, Enum):
    LOCAL_REVISE = "local_revise"
    HALT_FOR_HUMAN = "halt_for_human"
    OUT_OF_SCOPE = "out_of_scope"          # gate not a known panel gate


@dataclass
class Decision:
    kind: DecisionKind
    gate: str
    stage_to_rerun: str | None = None
    feedback_paths: list[str] = field(default_factory=list)
    reason: str = ""
    state: RevisionState | None = None
    # Informational: which producer the orchestrator would have picked,
    # even on a halt (helps humans triage). "" if indeterminate.
    would_rerun: str = ""


@dataclass(frozen=True)
class _DispatchResult:
    producer: str | None
    would_rerun: str
    reason: str
    prd_targeted: bool = False


def _gate_verdict_filename(gate: str) -> str:
    return f"panel-{gate}.json"


def _blocking_findings(v: PanelVerdict) -> list:
    """Findings that meet both severity and release-priority policy."""
    return v.blocking_findings()


def _claim_rerun_slot(state: RevisionState, gate: str) -> tuple[bool, bool]:
    """Claim a normal L slot, or consume one human-authorized extra slot.

    Returns ``(allowed, used_manual_credit)``.  Manual credits deliberately
    do not increase ``L`` beyond its invariant-preserving cap.
    """
    current = state.L.get(gate, 0)
    if current < L_MAX:
        state.L[gate] = current + 1
        return True, False
    if consume_manual_rerun_credit(state, gate):
        return True, True
    return False, False


def _extract_primary_filenames(findings) -> tuple[set[str], bool]:
    """Return (filenames, has_indeterminate).

    ``filenames`` is the set of ``<filename>`` extracted from targets
    on blocking findings. Both ``primary_pair.<filename>`` and
    ``anchor.<filename>`` targets contribute filenames — anchor targets
    reach this function only if the anchor-filter kept the finding
    (i.e. the finding cites ≥2 distinct anchor artifacts, which marks
    it as a cross-anchor substantive finding that must route back to
    the appropriate producer).

    ``has_indeterminate`` is True if any blocking finding had no
    filename-qualified target (empty targets, bare literals, or
    unknown/malformed strings).
    """
    filenames: set[str] = set()
    indeterminate = False
    for f in findings:
        found_specific = False
        for t in f.targets:
            if t.startswith("primary_pair."):
                filenames.add(t[len("primary_pair."):])
                found_specific = True
            elif t.startswith("anchor."):
                filenames.add(t[len("anchor."):])
                found_specific = True
            # Bare literals "primary_pair" / "anchor" are not recognized
            # in v3-core (filename-qualified targets required); they fall
            # through to found_specific=False and are treated as
            # indeterminate at dispatch.
        if not found_specific:
            indeterminate = True
    return filenames, indeterminate


def _dispatch_for_verdict(
    gate: str, v: PanelVerdict,
) -> _DispatchResult:
    """Decide the producer to rerun for a blocking verdict.

    Returns a dispatch object. ``producer`` is ``None`` if the
    orchestrator must halt for human decision (arch-doc target, mixed
    producers, repeated PRD target handled by caller, or indeterminate
    with no single-producer fallback).
    """
    blocking = _blocking_findings(v)
    filenames, indeterminate = _extract_primary_filenames(blocking)

    if filenames:
        # Check for halt-triggering filenames.
        prd_filenames = []
        halt_filenames = []
        producers: set[str] = set()
        for fn in filenames:
            base = fn.split("/")[-1]  # path-qualified → bare, for lookup
            if base == "prd.md":
                prd_filenames.append(fn)
                continue
            producer = filename_to_producer(gate, base)
            if producer is not None:
                producers.add(producer)
            else:
                # Not a harness-rerunnable artifact in this gate — either
                # an unknown filename or an external arch-doc per the
                # gate-specific map. Halt for human.
                halt_filenames.append(fn)

        if halt_filenames:
            return _DispatchResult(None, "", (
                f"findings target non-rerunnable artifact(s) "
                f"{sorted(halt_filenames)!r}; halt for human "
                f"(PRD amendment or arch-doc edit required)"
            ), prd_targeted=bool(prd_filenames))
        if prd_filenames:
            if gate != "design-review":
                return _DispatchResult(None, "", (
                    f"findings target PRD artifact(s) "
                    f"{sorted(prd_filenames)!r}; halt for human "
                    f"(PRD amendment required)"
                ), prd_targeted=True)
            producers.add("arch-design")
        if len(producers) >= 1:
            # When findings span multiple rerunnable producer stages, pick
            # the upstream-most. Rerunning upstream cascades downstream
            # stages naturally (design rerun → build redo → spec redo),
            # so the harness can make progress without halting for a human
            # to pick. Halt remains the right answer for PRD or arch-doc
            # targets (handled above) — those are not rerunnable.
            # "design" stays in this order (core R6): producer_stage_for_layer
            # can still hand back "design" via route_to_layer's build-diagnostic
            # path, which is out of scope for this change.
            _UPSTREAM_ORDER = ("arch-design", "design", "build", "spec")
            p = next((s for s in _UPSTREAM_ORDER if s in producers), None)
            if p is None:
                # Defensive: an unrecognized producer value made it in.
                return _DispatchResult(None, "", (
                    f"findings span unrecognized producer stages "
                    f"{sorted(producers)!r}; halt for human"
                ), prd_targeted=bool(prd_filenames))
            if len(producers) == 1:
                reason = f"rerun {p} — targets: {sorted(filenames)!r}"
            else:
                reason = (
                    f"rerun {p} (upstream-most of "
                    f"{sorted(producers)!r}); downstream stages will "
                    f"cascade. targets: {sorted(filenames)!r}"
                )
            if prd_filenames:
                reason = (
                    f"PRD-targeted design-review finding(s) "
                    f"{sorted(prd_filenames)!r}; rerun arch-design first so "
                    "the initial-design agent can try to avoid the apparent "
                    "PRD conflict"
                )
            return _DispatchResult(p, p, reason, prd_targeted=bool(prd_filenames))
        # producers empty but filenames non-empty — all were halt-filenames
        # (already handled above). Defensive fallthrough:
        return _DispatchResult(
            None, "", "no rerunnable producer derivable from targets",
            prd_targeted=bool(prd_filenames),
        )

    # No primary_pair.<filename> targets at all. Indeterminate — use
    # fallback for the gate if it has a single-producer primary pair.
    fallback = GATE_FALLBACK_PRODUCER.get(gate)
    if fallback is not None:
        return _DispatchResult(fallback, fallback, (
            f"indeterminate targets; gate {gate!r} primary pair has "
            f"single producer {fallback!r}; fallback rerun"
        ))
    return _DispatchResult(None, "", (
        f"indeterminate targets and gate {gate!r} has no single-producer "
        f"fallback; halt for human"
    ))


def producer_eligible_for_manual_rerun(
    feature_active: Path, gate: str, verdict: PanelVerdict,
) -> str | None:
    """Return the producer blocked only by ``L_MAX``, else ``None``.

    This is the non-mutating eligibility check used before issuing a manual
    rerun credit.  It mirrors every halt condition that precedes the L cap so
    a credit cannot attach to a PRD/human-only verdict and leak into a later,
    unrelated correction.
    """
    if gate not in ALL_PANEL_GATES:
        return None
    state = load_state(feature_active)
    if state.L.get(gate, 0) < L_MAX:
        return None
    if gate == "design-review" and verdict.decision is not None:
        return "arch-design" if verdict.decision.outcome == "retry_design" else None
    dispatch = _dispatch_for_verdict(gate, verdict)
    if dispatch.producer is None:
        return None
    if dispatch.prd_targeted and gate == "design-review":
        next_streak = min(
            PRD_TARGET_HALT_STREAK,
            state.prd_target_streak.get(gate, 0) + 1,
        )
        if next_streak >= PRD_TARGET_HALT_STREAK:
            return None
    return dispatch.producer


def handle_panel_verdict(
    feature_active: Path, gate: str, verdict: PanelVerdict,
) -> Decision:
    """Decide the next action given a blocking panel verdict.

    Precondition: caller has already confirmed the verdict blocks (at
    least one blocking-severity finding in ``findings[]`` after the
    anchor-filter has run) and that no skip-gate override is active.
    """
    if gate not in ALL_PANEL_GATES:
        return Decision(
            kind=DecisionKind.OUT_OF_SCOPE, gate=gate,
            reason=f"{gate} is not a known panel gate",
        )

    s = load_state(feature_active)

    if gate == "design-review" and verdict.decision is not None:
        outcome = verdict.decision.outcome
        if outcome == "halt_for_human":
            return Decision(
                kind=DecisionKind.HALT_FOR_HUMAN,
                gate=gate,
                state=s,
                reason=verdict.decision.summary,
                would_rerun="arch-design",
            )
        if outcome == "retry_design":
            prd_targeted = bool(verdict.decision.prd_targeted)
            s.prd_target_streak[gate] = 1 if prd_targeted else 0
            l = s.L.get(gate, 0)
            allowed, used_manual = _claim_rerun_slot(s, gate)
            if not allowed:
                return Decision(
                    kind=DecisionKind.HALT_FOR_HUMAN,
                    gate=gate,
                    state=s,
                    reason=(
                        f"L[{gate}]={l} already at L_MAX={L_MAX}; this is the "
                        f"{L_MAX+1}th blocking verdict — halt for human decision "
                        "(would have rerun 'arch-design')"
                    ),
                    would_rerun="arch-design",
                )
            write_state(feature_active, s)
            suffix = (
                "; consumed human-authorized rerun credit"
                if used_manual else ""
            )
            return Decision(
                kind=DecisionKind.LOCAL_REVISE,
                gate=gate,
                stage_to_rerun="arch-design",
                feedback_paths=[_gate_verdict_filename(gate)],
                state=s,
                would_rerun="arch-design",
                reason=(
                    f"canonical design_review retry_design; "
                    f"L[{gate}]={s.L[gate]}/{L_MAX}{suffix}"
                ),
            )
        if outcome == "pass":
            return Decision(
                kind=DecisionKind.OUT_OF_SCOPE,
                gate=gate,
                reason="canonical design_review pass does not enter the blocking revision loop",
                state=s,
            )

    dispatch = _dispatch_for_verdict(gate, verdict)
    producer = dispatch.producer

    if producer is None:
        return Decision(
            kind=DecisionKind.HALT_FOR_HUMAN, gate=gate, state=s,
            reason=dispatch.reason, would_rerun=dispatch.would_rerun,
        )

    if dispatch.prd_targeted and gate == "design-review":
        streak = min(
            PRD_TARGET_HALT_STREAK,
            s.prd_target_streak.get(gate, 0) + 1,
        )
        s.prd_target_streak[gate] = streak
        if streak >= PRD_TARGET_HALT_STREAK:
            write_state(feature_active, s)
            return Decision(
                kind=DecisionKind.HALT_FOR_HUMAN, gate=gate, state=s,
                reason=(
                    f"PRD-targeted design-review findings repeated "
                    f"{streak} consecutive rounds; halt for human after "
                    "design agent already had a chance to avoid the "
                    "apparent PRD conflict"
                ),
                would_rerun=producer,
            )
    else:
        s.prd_target_streak[gate] = 0

    # L_MAX halt — the (L_MAX+1)th blocking verdict (L already at L_MAX) halts.
    l = s.L.get(gate, 0)
    allowed, used_manual = _claim_rerun_slot(s, gate)
    if not allowed:
        return Decision(
            kind=DecisionKind.HALT_FOR_HUMAN, gate=gate, state=s,
            reason=(
                f"L[{gate}]={l} already at L_MAX={L_MAX}; this is the "
                f"{L_MAX+1}th blocking verdict — halt for human decision "
                f"(would have rerun {producer!r})"
            ),
            would_rerun=producer,
        )

    # Bump L[gate] and dispatch producer rerun. v3-core: pending_feedback
    # is no longer populated — orchestrator's CONTEXT_ARTIFACTS passes the
    # stage-relevant panel verdict to the re-run stage automatically.
    write_state(feature_active, s)
    suffix = (
        "; consumed human-authorized rerun credit"
        if used_manual else ""
    )
    return Decision(
        kind=DecisionKind.LOCAL_REVISE, gate=gate,
        stage_to_rerun=producer, feedback_paths=[_gate_verdict_filename(gate)],
        state=s, would_rerun=producer,
        reason=(
            f"{dispatch.reason}; L[{gate}]={s.L[gate]}/{L_MAX}{suffix}"
        ),
    )


def consume_pending_feedback(feature_active: Path, stage: str) -> list[str]:
    """Pop pending feedback paths for ``stage`` and persist state."""
    s = load_state(feature_active)
    rel = s.pending_feedback.pop(stage, [])
    if rel:
        write_state(feature_active, s)
    out: list[str] = []
    for r in rel:
        p = feature_active / r
        out.append(str(p))
    return out


@dataclass
class RouteDecision:
    """g-24 — outcome of a dev-driven diagnostic route attempt."""
    kind: DecisionKind                      # LOCAL_REVISE or HALT_FOR_HUMAN
    layer: str                              # "scope" | "plan" | "test-plan" | "prd" | "ambiguous"
    stage_to_rerun: str | None = None
    feedback_paths: list[str] = field(default_factory=list)
    reason: str = ""
    state: RevisionState | None = None


def route_to_layer(
    feature_active: Path,
    layer: str,
    *,
    trigger_ref: str,
) -> RouteDecision:
    """g-24 — orchestrator calls this when a build halt names a
    defective upstream layer.

    v3-core R4: routes consume the same L[gate] budget as panel reruns
    (per PRD: "a route is a rerun attempt and consumes the same budget
    as a blocking verdict"). No separate G ceiling.
    """
    if layer in ("prd", "ambiguous"):
        return RouteDecision(
            kind=DecisionKind.HALT_FOR_HUMAN, layer=layer,
            reason=(
                f"diagnostic routing target {layer!r} is not auto-rerunnable; "
                f"halt for human (PRD amendment required)"
                if layer == "prd" else
                f"diagnostic routing target is ambiguous; halt for human"
            ),
        )

    try:
        gate = gate_for_layer(layer)
        producer = producer_stage_for_layer(layer)
    except KeyError:
        return RouteDecision(
            kind=DecisionKind.HALT_FOR_HUMAN, layer=layer,
            reason=f"unknown routable layer {layer!r}; halt for human",
        )

    s = load_state(feature_active)

    l = s.L.get(gate, 0)
    allowed, used_manual = _claim_rerun_slot(s, gate)
    if not allowed:
        return RouteDecision(
            kind=DecisionKind.HALT_FOR_HUMAN, layer=layer, state=s,
            reason=(
                f"L[{gate}]={l} already at L_MAX={L_MAX}; cannot route to "
                f"{layer}; require `autodev update --amendment`"
            ),
        )

    fb = [trigger_ref]
    # v3-core: pending_feedback no longer populated; build.json lives on
    # disk and is picked up by CONTEXT_ARTIFACTS during design re-run.
    write_state(feature_active, s)

    suffix = (
        "; consumed human-authorized rerun credit"
        if used_manual else ""
    )

    return RouteDecision(
        kind=DecisionKind.LOCAL_REVISE, layer=layer, state=s,
        stage_to_rerun=producer, feedback_paths=fb,
        reason=(
            f"route to {layer} (gate={gate}, L={s.L[gate]}/{L_MAX}); "
            f"re-run {producer} with build-feedback{suffix}"
        ),
    )
