"""Human-feedback input model and validation (core R4-R5, detail §1.1-§1.2).

Split out of ``autodev/human_feedback.py`` to keep that module under the
repo's ~500-line-per-file guideline. This module owns:

- the ``HumanFeedback`` file model (§1.1) and its filename convention,
- ``validate_feedback`` and the three family-specific validators, which
  reuse the target package's OWN existing validators rather than
  inventing a parallel schema (core R5).

``autodev/human_feedback.py`` imports and re-exports everything here, so
callers only ever need ``import autodev.human_feedback``.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autodev import ralph
from autodev.artifacts.arch_review import _validate as _validate_arch_review
from autodev.artifacts.verdict import PanelFinding, _load_targets
from autodev.errors import SchemaError
from autodev.panel.anchor_filter import filter_anchor_findings

# core R2 — the five review points a human's feedback can be routed to.
REVIEW_POINTS: tuple[str, ...] = (
    "arch-review", "design-review", "trace-review", "close-approval",
    "ralph-review",
)
PANEL_POINTS: frozenset[str] = frozenset(
    {"design-review", "trace-review", "close-approval"}
)

_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------
# §1.1 — feedback file model
# ---------------------------------------------------------------------


@dataclass
class HumanFeedback:
    kind: str
    schema_version: int
    feedback_id: str
    review_point: str
    written: str
    verdict: str
    findings: list[dict[str, Any]]
    status: str = "pending"
    consumed_at: str | None = None
    consumed_into: str | None = None
    rejected_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "feedback_id": self.feedback_id,
            "review_point": self.review_point,
            "written": self.written,
            "verdict": self.verdict,
            "findings": [dict(f) for f in self.findings],
            "status": self.status,
            "consumed_at": self.consumed_at,
            "consumed_into": self.consumed_into,
        }
        if self.rejected_reason is not None:
            d["rejected_reason"] = self.rejected_reason
        return d

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "HumanFeedback":
        return HumanFeedback(
            kind=raw.get("kind", "human-feedback"),
            schema_version=raw.get("schema_version", _SCHEMA_VERSION),
            feedback_id=raw["feedback_id"],
            review_point=raw["review_point"],
            written=raw["written"],
            verdict=raw["verdict"],
            findings=[dict(f) for f in raw.get("findings", [])],
            status=raw.get("status", "pending"),
            consumed_at=raw.get("consumed_at"),
            consumed_into=raw.get("consumed_into"),
            rejected_reason=raw.get("rejected_reason"),
        )


def _feedback_path(active: Path, point: str) -> Path:
    return Path(active) / f"human-feedback-{point}.json"


def _new_feedback_id() -> str:
    # Second-resolution timestamp PLUS a 6-hex-char random suffix (detail
    # §10, closing-review pin): two feedbacks validated within the same
    # wall-clock second must not collide. Exposed as a module-level
    # function (rather than inlined) so tests can monkeypatch it.
    return (
        "hf-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-" + secrets.token_hex(3)
    )


# ---------------------------------------------------------------------
# §1.2 — input validation (family-specific, reusing existing validators)
# ---------------------------------------------------------------------


def _anchor_precheck(findings: list[dict[str, Any]]) -> None:
    """The one rule this module adds beyond reusing existing validators
    (detail §1.2, D15): reject up front any finding that the anchor
    filter would silently drop, so every accepted human finding is
    guaranteed to land in findings[] (core R6)."""
    for idx, raw in enumerate(findings):
        pf = PanelFinding(
            severity=raw.get("severity", ""), vendor="human",
            summary=raw.get("summary", ""), targets=_load_targets(raw),
        )
        _kept, dropped = filter_anchor_findings([pf])
        if dropped:
            raise SchemaError(
                f"finding #{idx + 1} only targets anchor artifacts (e.g. "
                "prd.md) and is opinion severity -- the anchor filter "
                "would silently drop it; confirm which primary artifact "
                "it targets and add that target, or if the PRD itself is "
                "wrong, follow R1 to amend the PRD instead of injecting "
                "this feedback"
            )


def _validate_finding_shapes(findings: list[Any]) -> None:
    """Reject malformed finding entries before any family-specific
    validator (which assumes a dict) or merge code (which assumes
    ``cited_artifact_span`` is a mapping) gets to see them -- a non-object
    finding or a non-dict ``cited_artifact_span`` must surface as
    SchemaError, not a TypeError/ValueError deep in a merge function
    (detail §10 d10 carry-over)."""
    for idx, raw in enumerate(findings):
        if not isinstance(raw, dict):
            raise SchemaError(f"findings[{idx}] must be a JSON object")
        span = raw.get("cited_artifact_span")
        if span is not None and not isinstance(span, dict):
            raise SchemaError(
                f"findings[{idx}].cited_artifact_span must be a JSON object"
            )


_PANEL_VERDICTS = {"pass", "needs_revision", "fail"}


def _validate_panel_family(point: str, payload: dict[str, Any]) -> None:
    verdict = payload.get("verdict")
    if verdict not in _PANEL_VERDICTS:
        raise SchemaError(
            f"human feedback verdict must be one of {sorted(_PANEL_VERDICTS)!r} "
            f"for review point {point!r}"
        )
    findings = payload["findings"]
    placeholder_findings = []
    for i, f in enumerate(findings):
        d = dict(f)
        d["vendor"] = "human"
        d["finding_id"] = f"human:{i + 1}"
        placeholder_findings.append(d)
    placeholder = {
        "gate": point,
        "verdict": verdict,
        "findings": placeholder_findings,
        "source": "placeholder", "source_hash": "sha256:" + "0" * 64,
        "prompt_file": "placeholder", "prompt_hash": "sha256:" + "0" * 64,
        "harness_version": "human-feedback", "run_ts": "1970-01-01T00:00:00Z",
    }
    from autodev.artifacts.verdict import _validate as _validate_panel_verdict
    _validate_panel_verdict(placeholder)

    if point == "design-review":
        from autodev.panel.runner import _validate_design_review_targets
        finding_objs = [
            PanelFinding(
                severity=f.get("severity", ""), vendor="human",
                summary=f.get("summary", ""), targets=_load_targets(f),
            )
            for f in findings
        ]
        _validate_design_review_targets(finding_objs)

    _anchor_precheck(findings)


def _validate_arch_review_family(payload: dict[str, Any]) -> None:
    placeholder = {
        "kind": "arch-review",
        "source": "placeholder",
        "source_hash": "sha256:" + "0" * 64,
        "prd_hash": "sha256:" + "0" * 64,
        "written": "1970-01-01T00:00:00Z",
        "verdict": payload["verdict"],
        "findings": payload["findings"],
    }
    _validate_arch_review(placeholder)


def _validate_ralph_review_family(active: Path, payload: dict[str, Any]) -> None:
    scope_path = Path(active) / "scope.json"
    if not scope_path.exists():
        raise SchemaError("ralph-review not injectable yet: scope.json missing")
    active_ids = ralph.active_scope_ids(scope_path)
    classifications = [
        {"req_id": f"{sid}.human", "scope_id": sid, "classification": "Fully"}
        for sid in sorted(active_ids)
    ]
    placeholder = {
        "classifications": classifications,
        "design_conformance": {
            "verdict": payload["verdict"],
            "findings": payload["findings"],
        },
    }
    ralph._parse_ralph_review_json(placeholder)


def validate_feedback(
    active: Path, point: str, payload: dict[str, Any],
) -> HumanFeedback:
    """Validate a raw ``{"verdict": ..., "findings": [...]}`` payload
    against the target package's existing structure/validators (core R5,
    detail §1.2). Raises SchemaError on any failure; never writes."""
    active = Path(active)
    if point not in REVIEW_POINTS:
        raise SchemaError(
            f"unknown review point {point!r}; must be one of {REVIEW_POINTS!r}"
        )
    if not isinstance(payload, dict) or set(payload) != {"verdict", "findings"}:
        raise SchemaError(
            "human feedback payload must be a JSON object with exactly "
            "the keys 'verdict' and 'findings'"
        )
    findings = payload.get("findings")
    if not isinstance(findings, list) or not findings:
        raise SchemaError("human feedback findings must be a non-empty list")
    _validate_finding_shapes(findings)
    if not isinstance(payload.get("verdict"), str):
        # Guards every family's `verdict not in {...}` membership check
        # below against an unhashable verdict (e.g. a list/dict) raising
        # TypeError instead of the intended SchemaError.
        raise SchemaError("human feedback 'verdict' must be a string")

    if point in PANEL_POINTS:
        _validate_panel_family(point, payload)
    elif point == "arch-review":
        _validate_arch_review_family(payload)
    else:  # ralph-review
        _validate_ralph_review_family(active, payload)

    return HumanFeedback(
        kind="human-feedback",
        schema_version=_SCHEMA_VERSION,
        feedback_id=_new_feedback_id(),
        review_point=point,
        written=datetime.now(timezone.utc).isoformat(),
        verdict=payload["verdict"],
        findings=[dict(f) for f in findings],
        status="pending",
        consumed_at=None,
        consumed_into=None,
    )
