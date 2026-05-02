"""build.json — v2 schema includes R2b evidence fields.

Phase-5 / g-24: deviations may carry an optional ``diagnosis`` sub-object
naming the defective upstream layer. Schema-validated here; scope_id
existence cross-check is the orchestrator's responsibility.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json

# g-24 — allowed values for deviations[i].diagnosis.defective_layer.
DIAGNOSIS_LAYERS: tuple[str, ...] = (
    "prd", "design", "ambiguous",
)

# g-24 — minimum evidence length (schema-level anti-blank-prose).
DIAGNOSIS_EVIDENCE_MIN_CHARS = 16

# g-24 — layers that the orchestrator can auto-route to. prd and
# ambiguous halt-for-human.
ROUTABLE_LAYERS: tuple[str, ...] = ("design",)

# g-24 — canonicalization: the "layer" dimension as used by the
# unified L counter.
LAYER_CANONICAL: dict[str, str] = {
    "design": "design",
}


@dataclass
class BuildReport:
    source: str
    source_hash: str
    written: str
    # R2b evidence fields — required, not optional
    test_cmd_run: str               # e.g. "pytest tests/v2/ -v"
    test_exit_code: int             # 0 → tests green
    test_results: dict[str, Any]    # {passed, failed, skipped}
    files_changed: list[str]        # stage write scope audit
    # Retained from v0.1
    lint: dict[str, Any] = field(default_factory=lambda: {"passed": True})
    deviations: list[dict[str, Any]] = field(default_factory=list)
    blocking: bool = False
    # v2 workspace audit
    workspace_dirty_at_stage_end: bool = False
    # Captures current git HEAD after build's WIP commits land. Without
    # this, build.json content can be byte-identical across reruns
    # (same test_results, same files_changed list, etc.) even when the
    # LLM made real code changes that resulted in new WIP commits.
    # Cascade then thinks build is fresh and emits pipeline-done while
    # close-approval is still needs_revision. Filled by the orchestrator
    # post-subprocess, not by the LLM (subprocess can't reliably read
    # the just-committed HEAD before its own .tmp rename).
    sealed_ref: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_hash": self.source_hash,
            "written": self.written,
            "test_cmd_run": self.test_cmd_run,
            "test_exit_code": self.test_exit_code,
            "test_results": self.test_results,
            "files_changed": self.files_changed,
            "lint": self.lint,
            "deviations": self.deviations,
            "blocking": self.blocking,
            "workspace_dirty_at_stage_end": self.workspace_dirty_at_stage_end,
            "sealed_ref": self.sealed_ref,
        }


def _validate_diagnosis(dx: Any, *, deviation_index: int) -> None:
    """g-24 — validate the optional diagnosis sub-object shape.

    Cross-check against scope.json for scope_id existence is the
    orchestrator's job (scope.json isn't loaded here)."""
    if not isinstance(dx, dict):
        raise SchemaError(
            f"build.json.deviations[{deviation_index}].diagnosis must be object"
        )
    layer = dx.get("defective_layer")
    if layer not in DIAGNOSIS_LAYERS:
        raise SchemaError(
            f"build.json.deviations[{deviation_index}].diagnosis.defective_layer "
            f"must be one of {DIAGNOSIS_LAYERS}, got {layer!r}"
        )
    ev = dx.get("evidence")
    if not isinstance(ev, str) or len(ev.strip()) < DIAGNOSIS_EVIDENCE_MIN_CHARS:
        raise SchemaError(
            f"build.json.deviations[{deviation_index}].diagnosis.evidence "
            f"must be a non-empty string of at least "
            f"{DIAGNOSIS_EVIDENCE_MIN_CHARS} characters"
        )
    rerun_from = dx.get("proposed_rerun_from")
    if rerun_from is not None and not isinstance(rerun_from, str):
        raise SchemaError(
            f"build.json.deviations[{deviation_index}].diagnosis."
            f"proposed_rerun_from must be a string when present"
        )


def _validate(obj: dict) -> None:
    required = (
        "source", "source_hash", "written",
        "test_cmd_run", "test_exit_code", "test_results", "files_changed",
    )
    for k in required:
        if k not in obj:
            raise SchemaError(f"build.json missing {k}")
    if not isinstance(obj["test_cmd_run"], str) or not obj["test_cmd_run"].strip():
        raise SchemaError("build.json.test_cmd_run must be a non-empty string")
    if not isinstance(obj["test_exit_code"], int):
        raise SchemaError("build.json.test_exit_code must be an int")
    tr = obj["test_results"]
    for k in ("passed", "failed", "skipped"):
        if k not in tr:
            raise SchemaError(f"build.json.test_results missing {k}")
    # g-24 — optional diagnosis on each deviation.
    deviations = obj.get("deviations", [])
    if not isinstance(deviations, list):
        raise SchemaError("build.json.deviations must be a list")
    for i, dev in enumerate(deviations):
        if not isinstance(dev, dict):
            raise SchemaError(f"build.json.deviations[{i}] must be object")
        if "diagnosis" in dev:
            _validate_diagnosis(dev["diagnosis"], deviation_index=i)


def load_build(path: Path) -> BuildReport:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(raw)
    return BuildReport(
        source=raw["source"], source_hash=raw["source_hash"], written=raw["written"],
        test_cmd_run=raw["test_cmd_run"], test_exit_code=raw["test_exit_code"],
        test_results=raw["test_results"], files_changed=raw["files_changed"],
        lint=raw.get("lint", {"passed": True}),
        deviations=raw.get("deviations", []),
        blocking=raw.get("blocking", False),
        workspace_dirty_at_stage_end=raw.get("workspace_dirty_at_stage_end", False),
        sealed_ref=raw.get("sealed_ref", ""),
    )


def write_build(path: Path, report: BuildReport) -> None:
    if not report.written:
        report.written = date.today().isoformat()
    d = report.to_dict()
    _validate(d)
    atomic_write_json(Path(path), d)
