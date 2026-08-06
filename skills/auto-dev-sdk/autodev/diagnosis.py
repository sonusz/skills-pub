"""Convergence diagnosis + rework-mode selection — mechanism 2 of
docs/proposals/rigor-tier.md.

Called by the orchestrator BEFORE ``handle_panel_verdict`` on every
blocking verdict (normative ordering: a diagnosed round never enters
the revision loop, so no L[gate] is consumed and no revision state is
written for it).

Flow per blocking verdict:

1. Record the verdict's blocking-finding fingerprints
   (fingerprint-history.json; idempotent per run_ts).
2. Any blocking fingerprint RECURRING (seen in an earlier round despite
   an intervening rerun) → stop rerunning, run the stall classifier,
   write diagnosis.json, and halt for human. Re-running would be
   ping-pong (a fix already ran and the finding came back).
3. Otherwise select the rework mode for the upcoming producer rerun and
   persist it (rework-mode.json) for prompt injection:
   - ``patch``      — few, narrowly-anchored, fresh findings: fix
                      exactly the cited findings, no restructuring.
   - ``root-cause`` — everything else: the current stage-design rework
                      protocol (consolidate, coherent redesign allowed).

Stall classification (mechanical, zero LLM):

- Counterfactually re-run the rigor filter with each implicated R
  lowered one level. Unblocks → **rigor-pivotal stall**: only the
  human's tolerance can move; draft the re-audit question from the
  Assurance rationale. All pivot pairs already declined → halt as
  accepted-known-blocker without re-asking.
- Still blocks → **coherence stall**: rigor is not the cause; draft
  amendment guidance. (LLM-assisted sub-classification over
  design-changelog + verdict history is an explicit follow-up.)
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from autodev.artifacts.fingerprint_history import (
    compute_fingerprint, load_history, record_verdict,
)
from autodev.artifacts.verdict import PanelFinding, PanelVerdict
from autodev.assurance import AssuranceMap, parse_assurance
from autodev.panel.rigor_filter import (
    apply_rigor_filter, has_effective_blocking, one_level_down, resolve_rs,
)
from autodev.state.atomic import atomic_write_json

MODE_ROOT_CAUSE = "root-cause"
MODE_PATCH = "patch"

# Patch mode requires at most this many blocking findings, all fresh
# and all narrowly anchored (non-empty targets).
PATCH_MAX_BLOCKING = 2

REWORK_MODE_FILENAME = "rework-mode.json"
DIAGNOSIS_FILENAME = "diagnosis.json"

_BLOCKING = ("invariant_violation", "risk")


@dataclass
class DiagnosisResult:
    classification: str          # "rigor-pivotal" | "coherence" | "accepted-known-blocker"
    pivot_rs: list[str]
    halt_reason: str
    diagnosis_path: Path


def _blocking_findings(v: PanelVerdict) -> list[PanelFinding]:
    return v.blocking_findings()


def select_rework_mode(
    blocking: list[PanelFinding], recurring: set[str], *,
    issue_count: int | None = None,
) -> str:
    """Trust-region step size for the next producer rerun. Computed by
    the harness — never left to the design agent's judgment (under gate
    pressure it would always choose patch)."""
    if recurring:
        # Caller diagnoses instead of rerunning; mode is moot, but keep
        # the conservative answer for any other caller.
        return MODE_ROOT_CAUSE
    if (
        (issue_count if issue_count is not None else len(blocking))
        <= PATCH_MAX_BLOCKING
        and blocking
        and all(f.targets for f in blocking)
    ):
        return MODE_PATCH
    return MODE_ROOT_CAUSE


def write_rework_mode(
    feature_active: Path, mode: str, *, gate: str, run_ts: str,
    blocking_count: int,
) -> None:
    atomic_write_json(feature_active / REWORK_MODE_FILENAME, {
        "mode": mode,
        "gate": gate,
        "verdict_run_ts": run_ts,
        "blocking_count": blocking_count,
    })


def read_rework_mode(feature_active: Path) -> str | None:
    p = Path(feature_active) / REWORK_MODE_FILENAME
    if not p.exists():
        return None
    try:
        mode = json.loads(p.read_text(encoding="utf-8")).get("mode")
    except Exception:
        return None
    return mode if mode in (MODE_PATCH, MODE_ROOT_CAUSE) else None


def _load_assurance_and_scope(feature_active: Path):
    assurance = AssuranceMap()
    prd = feature_active / "prd.md"
    try:
        if prd.exists():
            assurance, _ = parse_assurance(prd.read_text(encoding="utf-8"))
    except Exception:
        assurance = AssuranceMap()
    scope = None
    scope_path = feature_active / "scope.json"
    try:
        if scope_path.exists():
            from autodev.artifacts.scope import load_scope
            scope = load_scope(scope_path)
    except Exception:
        scope = None
    return assurance, scope


def classify_stall(
    feature_active: Path,
    recurring_findings: list[PanelFinding],
    all_blocking: list[PanelFinding],
) -> tuple[str, list[str]]:
    """Counterfactual downgrade test. Returns (classification, pivot_rs).

    Copies of ALL blocking findings are re-filtered with every
    implicated R lowered one level; the stall is rigor-pivotal iff the
    counterfactual verdict stops blocking. Restoring reviewer-reported
    severities first keeps the test meaningful for findings the real
    filter already touched.
    """
    assurance, scope = _load_assurance_and_scope(feature_active)
    implicated: set[str] = set()
    for f in recurring_findings:
        implicated.update(resolve_rs(f, scope, assurance.known_rs))
    if not implicated:
        return "coherence", []
    override = {
        r: one_level_down(assurance.level_for(r)) for r in sorted(implicated)
    }
    copies = [copy.deepcopy(f) for f in all_blocking]
    for c in copies:
        if c.severity_reported is not None:
            c.severity = c.severity_reported
    apply_rigor_filter(copies, assurance, scope, override_level=override)
    if has_effective_blocking(copies, assurance.release_threshold):
        return "coherence", sorted(implicated, key=lambda r: int(r[1:]))
    return "rigor-pivotal", sorted(implicated, key=lambda r: int(r[1:]))


def _re_audit_question(
    assurance: AssuranceMap, pivot_rs: list[str], rounds_stuck: int,
) -> str:
    parts: list[str] = []
    for r in pivot_rs:
        level = assurance.level_for(r)
        rationale = assurance.rationale.get(r, "(no recorded rationale)")
        parts.append(
            f"{r} is `{level}` — you accepted: \"{rationale}\". Holding it "
            f"has kept this gate stuck for {rounds_stuck} rounds. Keep the "
            f"tolerance, or lower {r} to `{one_level_down(level)}` via "
            f"`autodev update --amendment 'Assurance: {r} {level} -> "
            f"{one_level_down(level)}'`?"
        )
    return "\n".join(parts)


def check_and_diagnose(
    feature_active: Path, gate: str, verdict: PanelVerdict,
) -> DiagnosisResult | None:
    """Fingerprint bookkeeping + stall diagnosis for one blocking
    verdict. Returns None when the loop should proceed normally (no
    recurrence); then rework-mode.json has been refreshed for the
    upcoming rerun. Returns a DiagnosisResult (caller halts, consuming
    no L) on recurrence."""
    report = record_verdict(feature_active, gate, verdict)
    blocking = _blocking_findings(verdict)
    by_id = {f.finding_id: f for f in blocking if f.finding_id}
    recurring_findings: list[PanelFinding] = []
    seen_ids: set[int] = set()
    for cluster in verdict.issue_clusters:
        if cluster.cluster_id not in report.recurring_cluster_ids:
            continue
        for finding_id in cluster.finding_ids:
            finding = by_id.get(finding_id)
            if finding is not None and id(finding) not in seen_ids:
                recurring_findings.append(finding)
                seen_ids.add(id(finding))
    # Legacy/singleton verdicts have no persisted cluster membership.
    for finding in blocking:
        if (id(finding) not in seen_ids
                and compute_fingerprint(finding) in report.recurring):
            recurring_findings.append(finding)
            seen_ids.add(id(finding))
    if not recurring_findings:
        issue_count = len(verdict.blocking_issue_clusters())
        mode = select_rework_mode(
            blocking, report.recurring, issue_count=issue_count,
        )
        write_rework_mode(
            feature_active, mode, gate=gate, run_ts=verdict.run_ts,
            blocking_count=issue_count,
        )
        return None

    assurance, _scope = _load_assurance_and_scope(feature_active)
    history = load_history(feature_active)
    rounds_stuck = len(history.rounds.get(gate, []))
    classification, pivot_rs = classify_stall(
        feature_active, recurring_findings, blocking,
    )

    diagnosis: dict = {
        "gate": gate,
        "verdict_run_ts": verdict.run_ts,
        "classification": classification,
        "pivot_rs": pivot_rs,
        "recurring_fingerprints": sorted(report.recurring),
        "recurring_summaries": [f.summary[:200] for f in recurring_findings],
        "rounds_recorded": rounds_stuck,
    }

    if classification == "rigor-pivotal":
        pairs = {
            (compute_fingerprint(f), r)
            for f in recurring_findings for r in pivot_rs
        }
        if pairs and pairs <= history.declined_pairs():
            classification = "accepted-known-blocker"
            diagnosis["classification"] = classification
            diagnosis["note"] = (
                "re-audit for these (fingerprint, R) pairs was already "
                "declined; not re-asking"
            )
            halt_reason = (
                f"accepted-known-blocker: recurring finding(s) on "
                f"{', '.join(pivot_rs)} were already re-audited and the "
                f"tolerance was kept. Forward motion requires "
                f"`autodev update --amendment` or `autodev skip-gate {gate}`."
            )
        else:
            question = _re_audit_question(assurance, pivot_rs, rounds_stuck)
            diagnosis["re_audit_question"] = question
            halt_reason = (
                f"rigor-pivotal stall on {', '.join(pivot_rs)}: the same "
                f"finding(s) recurred despite a rerun, and lowering "
                f"{', '.join(pivot_rs)} one level would unblock. No design "
                f"rerun can fix this class — answer the re-audit question "
                f"in diagnosis.json (keep tolerance ⇒ `autodev skip-gate "
                f"{gate}` records the decline; lower it ⇒ `autodev update "
                f"--amendment`)."
            )
    else:
        diagnosis["amendment_guidance"] = (
            "Rigor is not the pivot (counterfactual downgrade still "
            "blocks). Likely causes: contradiction between requirements "
            "or requirement↔constraint, or a dispute not resolvable in "
            "document space (needs a spike). Review recurring_summaries "
            "against design-changelog.json history and draft a PRD "
            "amendment; LLM-assisted sub-classification is a follow-up."
        )
        halt_reason = (
            f"coherence stall at {gate}: recurring finding(s) survive even "
            f"a counterfactual rigor downgrade — rerunning would ping-pong. "
            f"See diagnosis.json (recurring findings + amendment guidance)."
        )

    path = feature_active / DIAGNOSIS_FILENAME
    atomic_write_json(path, diagnosis)
    return DiagnosisResult(
        classification=classification, pivot_rs=pivot_rs,
        halt_reason=halt_reason, diagnosis_path=path,
    )


def check_route_recurrence(
    feature_active: Path, layer: str, scope_ids: list[str],
    build_path: Path,
) -> DiagnosisResult | None:
    """Mechanism 4 deferral-bet settlement (rigor-tier Phase D.9).

    Build routes never produce a PanelVerdict, so the route path
    records a synthetic fingerprint keyed on (layer, implicated scope
    ids), source-hashed on the design package. Recurrence under a
    CHANGED design hash means the same items bounced back from build
    after a design rerun — the deferral bet lost twice. Halt with a
    depth-aware amendment draft instead of burning build invocations
    down to the generic L_MAX halt. Returns None to proceed with the
    normal route."""
    from autodev.artifacts.fingerprint_history import (
        FingerprintHistory, load_history, write_history,
    )
    from autodev.state.hashing import hash_file

    base = Path(feature_active)
    packet = base / "design-packet.json"
    design_md = base / "design.md"
    try:
        source_hash = hash_file(packet if packet.exists() else design_md)
        run_ts = f"route:{hash_file(build_path)}"
    except Exception:
        return None

    fp_key = json.dumps(
        ["build-route", layer, sorted(scope_ids)], separators=(",", ":"))
    import hashlib
    fp = hashlib.sha256(fp_key.encode("utf-8")).hexdigest()[:24]

    h = load_history(base)
    entries = h.rounds.setdefault("build-route", [])
    recurred = any(
        e["run_ts"] != run_ts
        and e.get("source_hash")
        and e["source_hash"] != source_hash
        and fp in e["fingerprints"]
        for e in entries
    )
    if not any(e["run_ts"] == run_ts for e in entries):
        entries.append({
            "run_ts": run_ts, "source_hash": source_hash,
            "fingerprints": [fp],
        })
        write_history(base, h)
    if not recurred:
        return None

    assurance, scope = _load_assurance_and_scope(base)
    rs: set[str] = set()
    if scope is not None:
        import re as _re
        for item in scope.in_scope:
            if item.id in scope_ids:
                rs.update(t for t in item.prd_ref if _re.fullmatch(r"R\d+", t))
    drafts = [
        f"Depth: {r} {assurance.depth_for(r)} -> upfront"
        for r in sorted(rs, key=lambda x: int(x[1:]))
        if assurance.depth_for(r) != "upfront"
    ]
    diagnosis = {
        "gate": "build-route",
        "classification": "deferral-falsified",
        "layer": layer,
        "scope_ids": sorted(scope_ids),
        "pivot_rs": sorted(rs, key=lambda x: int(x[1:])),
        "amendment_draft": drafts or [
            "(no non-upfront R resolvable from the routed items; "
            "inspect build.json deviations manually)"
        ],
        "note": (
            "the same scope item(s) bounced back from build after a "
            "design rerun — interior deferral for them is falsified; "
            "force upfront design via the amendment draft, or revise "
            "the contract itself"
        ),
    }
    path = base / DIAGNOSIS_FILENAME
    atomic_write_json(path, diagnosis)
    return DiagnosisResult(
        classification="deferral-falsified",
        pivot_rs=diagnosis["pivot_rs"],
        halt_reason=(
            f"deferral falsified: scope item(s) {sorted(scope_ids)!r} "
            f"routed back from build twice across a design change. "
            f"See diagnosis.json (amendment draft: "
            f"{'; '.join(diagnosis['amendment_draft'])}). Apply via "
            f"`autodev update --amendment`."
        ),
        diagnosis_path=path,
    )


def record_skip_as_declined(feature_active: Path, gate: str) -> int:
    """`autodev skip-gate` while a rigor-pivotal diagnosis is pending ⇒
    the human kept the tolerance. Persist the declined pairs so the same
    question is never asked again. Returns pairs recorded."""
    from autodev.artifacts.fingerprint_history import record_declined
    p = Path(feature_active) / DIAGNOSIS_FILENAME
    if not p.exists():
        return 0
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if d.get("gate") != gate or d.get("classification") != "rigor-pivotal":
        return 0
    pairs = {
        (fp, r)
        for fp in d.get("recurring_fingerprints", [])
        for r in d.get("pivot_rs", [])
    }
    record_declined(feature_active, pairs)
    return len(pairs)
