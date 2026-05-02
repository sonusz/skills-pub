"""v3-core R3 — anchor-filter post-processing.

Runs after the synthesizer returns structured JSON. Findings whose
``targets`` entries are ALL anchor entries are moved out of the
verdict's ``findings[]`` into ``dropped_findings[]`` when they are
opinion-only. Blocking anchor findings are kept so PRD contradictions
or missing critical requirements can halt for human decision.

For opinion findings, a single anchor target drops as wordsmithing; if
a single finding emits ≥2 anchor targets (cross-anchor OR repeated same
anchor), it is kept as a substantive upstream signal.

Findings that target any primary-pair entry stay in ``findings[]``.

Target string grammar:
    primary_pair.<filename>   primary — keep
    anchor.<filename>         anchor  — keep if blocking; opinion drops
                                      if count==1, keeps if count>=2
    <anything else>           unknown — keep (primary-pair default), log warning
    (empty targets list)      keep (primary-pair default, conservative)
"""
from __future__ import annotations

from typing import Callable

from autodev.artifacts.verdict import DroppedFinding, PanelFinding


def _classify_target(t: str) -> str:
    """Return ``"primary"`` | ``"anchor"`` | ``"unknown"`` for one target."""
    if t.startswith("primary_pair."):
        return "primary"
    if t.startswith("anchor."):
        return "anchor"
    return "unknown"


def filter_anchor_findings(
    findings: list[PanelFinding],
    *,
    warn: Callable[[str], None] | None = None,
) -> tuple[list[PanelFinding], list[DroppedFinding]]:
    """Split ``findings`` into (kept, dropped).

    Drop rule: every target is ``"anchor"`` or ``"anchor.<filename>"``.
    Empty ``targets`` → kept (primary_pair default).
    Unknown targets → kept (primary_pair default) + warn.
    """
    kept: list[PanelFinding] = []
    dropped: list[DroppedFinding] = []
    for f in findings:
        if not f.targets:
            kept.append(f)
            continue
        classes = [_classify_target(t) for t in f.targets]
        unknowns = [t for t, c in zip(f.targets, classes) if c == "unknown"]
        if unknowns and warn is not None:
            warn(
                f"unknown target string(s) {unknowns!r} on finding "
                f"(vendor={f.vendor}, severity={f.severity}); "
                f"defaulting to primary_pair (kept)"
            )
        # Keep if any target classifies as primary OR unknown (unknown
        # defaults to primary_pair per R3).
        if any(c in ("primary", "unknown") for c in classes):
            kept.append(f)
            continue
        # All targets are anchor. Blocking anchor findings must stay in
        # findings[] so revision_loop can halt for human on PRD or other
        # upstream-anchor problems. Opinion anchor findings still use
        # the original wordsmithing filter.
        if f.severity in ("invariant_violation", "risk"):
            kept.append(f)
            continue
        # For opinions, total anchor target count ≥2 means the reviewer
        # emitted multiple anchor signals on this finding (cross-anchor
        # OR repeated same anchor) — both readings are substantive enough
        # to keep. Count-1 means a lone anchor pointer → drop.
        if len(f.targets) >= 2:
            kept.append(f)
            continue
        dropped.append(DroppedFinding(
            severity=f.severity, vendor=f.vendor, summary=f.summary,
            cited_artifact_span=f.cited_artifact_span,
            targets=list(f.targets),
            drop_reason="all-anchor-targets",
        ))
    return kept, dropped
