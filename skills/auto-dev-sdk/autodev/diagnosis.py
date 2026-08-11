"""Panel rework-mode selection and finding-fingerprint telemetry.

Every blocking panel verdict records its finding fingerprints, then selects
the trust-region size for the next producer rerun:

- ``patch`` — few, narrowly anchored, fresh findings;
- ``root-cause`` — broad, unanchored, or recurring findings.

Recurrence is evidence that a broader correction is warranted, not proof that
the pipeline is deadlocked. The normal revision loop and its ``L_MAX`` budget
own convergence and stopping; fingerprints never halt the pipeline.
"""
from __future__ import annotations

import json
from pathlib import Path

from autodev.artifacts.fingerprint_history import record_verdict
from autodev.artifacts.verdict import PanelFinding, PanelVerdict
from autodev.state.atomic import atomic_write_json

MODE_ROOT_CAUSE = "root-cause"
MODE_PATCH = "patch"

# Patch mode requires at most this many blocking findings, all fresh
# and all narrowly anchored (non-empty targets).
PATCH_MAX_BLOCKING = 2

REWORK_MODE_FILENAME = "rework-mode.json"


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
        # A repeated finding gets a broader correction attempt. Recurrence
        # alone must never short-circuit the normal revision budget.
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


def prepare_panel_rework(
    feature_active: Path, gate: str, verdict: PanelVerdict,
) -> str:
    """Record one blocking verdict and select its producer rework mode.

    The caller always continues into the normal revision loop. A recurring
    fingerprint selects ``root-cause`` but has no stop semantics.
    """
    report = record_verdict(feature_active, gate, verdict)
    blocking = _blocking_findings(verdict)
    issue_count = len(verdict.blocking_issue_clusters())
    mode = select_rework_mode(
        blocking, report.recurring, issue_count=issue_count,
    )
    write_rework_mode(
        feature_active, mode, gate=gate, run_ts=verdict.run_ts,
        blocking_count=issue_count,
    )
    return mode
