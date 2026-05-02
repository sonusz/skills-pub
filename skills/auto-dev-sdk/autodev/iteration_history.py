"""Iteration history manifest — temporal view of artifact / gate events.

Producer rerun stages (design / build) need to know not just which files
to read, but *when* each was last written and what happened around it.
A panel verdict produced before a PRD amendment is historical context;
the next design pass should treat its findings as signals that may have
been addressed already, not as fresh blockers.

The manifest is built by walking ``log.jsonl`` and selecting events that
correspond to durable state changes:

- ``stage-complete``    — a producer (design / build / spec) finished
- ``artifact-written``  — a harness-authored artifact was written
- ``panel-done``        — a gate verdict was recorded
- ``revision-loop-triggered`` — a producer was re-dispatched
- ``prd-amended``       — emitted by ``autodev update``; marks a cycle
                          boundary so downstream consumers can see
                          which prior events are stale

Routine progress events (subprocess-dispatch / subprocess-start /
iteration-recorded) are intentionally excluded — they would drown the
manifest in noise without adding decision-relevant signal.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from autodev.state.hashing import hash_file


# Event types that produce a manifest entry, in priority order. Events
# not in this set are skipped.
_RELEVANT_EVENTS: set[tuple[str, str]] = {
    ("orchestrator", "prd-amended"),
    ("orchestrator", "pipeline-done"),
    ("gate", "panel-done"),
    ("gate", "revision-loop-triggered"),
    ("gate", "revision-loop-halt"),
    ("design-packet", "artifact-written"),
    ("accepted-design", "artifact-written"),
    ("implementation-index", "artifact-written"),
    ("prd-checklist", "artifact-written"),
    ("design", "stage-complete"),
    ("build", "stage-complete"),
    ("spec", "stage-complete"),
    ("ralph-review", "stage-complete"),
}


# Event-to-artifact mapping for events that produce or supersede a
# named artifact. Used to attach the current on-disk hash to each
# manifest entry.
_EVENT_ARTIFACT: dict[tuple[str, str], str] = {
    ("orchestrator", "prd-amended"): "prd.md",
    ("design", "stage-complete"): "design.md",
    ("build", "stage-complete"): "build.json",
    ("spec", "stage-complete"): "implemented-spec.md",
    ("ralph-review", "stage-complete"): "ralph-review.json",
    ("design-packet", "artifact-written"): "design-packet.json",
    ("accepted-design", "artifact-written"): "accepted-design.json",
    ("implementation-index", "artifact-written"): "implementation-index.json",
    ("prd-checklist", "artifact-written"): "prd-checklist.json",
}


@dataclass(frozen=True)
class HistoryEntry:
    """One row in the iteration manifest.

    ``ts`` is the ISO-8601 UTC timestamp from the log event.
    ``label`` is a one-line human-readable description rendered into
    the prompt.
    ``artifact`` is the basename of the file this event produced /
    superseded, if any.
    ``current_hash`` is ``hash_file()`` of that artifact at manifest
    build time, or None if the file no longer exists.
    """

    ts: str
    label: str
    artifact: str | None = None
    current_hash: str | None = None


def _label_for(stage: str, event: str, detail: dict) -> str:
    """Render a one-line summary for a (stage, event) tuple."""
    if (stage, event) == ("orchestrator", "prd-amended"):
        return "prd.md AMENDED via `autodev update --amendment`"
    if (stage, event) == ("orchestrator", "pipeline-done"):
        return "pipeline reached `done`"
    if (stage, event) == ("gate", "panel-done"):
        gate = detail.get("gate", "?")
        verdict = detail.get("verdict", "?")
        return f"panel-{gate} done verdict={verdict}"
    if (stage, event) == ("gate", "revision-loop-triggered"):
        gate = detail.get("gate", "?")
        rerun = detail.get("would_rerun") or detail.get("stage_to_rerun") or "?"
        return f"panel-{gate} blocking → re-dispatch {rerun}"
    if (stage, event) == ("gate", "revision-loop-halt"):
        gate = detail.get("gate", "?")
        return f"panel-{gate} halted for human"
    if event == "stage-complete":
        return f"{stage} stage-complete"
    if event == "artifact-written":
        path = detail.get("artifact", "")
        name = Path(path).name if path else f"{stage} artifact"
        return f"{name} written"
    return f"{stage} {event}"


def build_iteration_history(feature_active: Path) -> list[HistoryEntry]:
    """Walk ``log.jsonl`` and return a chronological list of manifest
    entries (oldest first).

    Returns an empty list if ``log.jsonl`` does not exist, is empty, or
    contains no relevant events.
    """
    log_path = feature_active / "log.jsonl"
    if not log_path.exists():
        return []
    entries: list[HistoryEntry] = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            stage = rec.get("stage", "")
            event = rec.get("event", "")
            if (stage, event) not in _RELEVANT_EVENTS:
                continue
            detail = rec.get("detail") or {}
            ts = rec.get("ts", "")
            artifact = _EVENT_ARTIFACT.get((stage, event))
            current_hash: str | None = None
            if artifact is not None:
                p = feature_active / artifact
                if p.exists():
                    try:
                        current_hash = hash_file(p)
                    except OSError:
                        current_hash = None
            entries.append(HistoryEntry(
                ts=ts,
                label=_label_for(stage, event, detail),
                artifact=artifact,
                current_hash=current_hash,
            ))
    # Sort by timestamp ascending (oldest first). Real autodev runs
    # always append in chronological order, but a defensive sort makes
    # the manifest robust to manual log edits and to tests that emit
    # rows out of order.
    entries.sort(key=lambda e: e.ts)
    return entries


def render_iteration_history(entries: list[HistoryEntry]) -> str:
    """Render manifest as a markdown table for inclusion in a prompt.

    Returns an empty string when the list is empty (caller decides
    whether to include a placeholder).
    """
    if not entries:
        return ""
    lines = [
        "| # | Time (UTC) | Event | Artifact | Current hash |",
        "|---|------------|-------|----------|--------------|",
    ]
    for i, e in enumerate(entries, 1):
        artifact = e.artifact or "—"
        ch = e.current_hash[:23] + "…" if e.current_hash else "—"
        lines.append(f"| {i} | {e.ts} | {e.label} | {artifact} | {ch} |")
    return "\n".join(lines)
