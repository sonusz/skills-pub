"""panel-verdict.json — v2's structured panel-review output (R4a)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json

InvariantViolation = "invariant_violation"  # re-exported for convenience
Severity = Literal["invariant_violation", "risk", "opinion"]
Verdict = Literal["pass", "needs_revision", "fail", "skipped"]
Gate = Literal["design-review", "trace-review", "close-approval"]
DecisionOutcome = Literal["pass", "retry_design", "halt_for_human"]

_VALID_SEVERITY = {"invariant_violation", "risk", "opinion"}
_VALID_VERDICT = {"pass", "needs_revision", "fail", "skipped"}
_VALID_GATE = {"design-review", "trace-review", "close-approval"}
_VALID_DECISION_SEVERITY = {"invariant_violation", "risk", "opinion"}
_VALID_DESIGN_REVIEW_OUTCOME = {"pass", "retry_design", "halt_for_human"}

NO_RESPONSE_PREFIX = "[NO RESPONSE"
_TRANSPORT_SUMMARY_MARKERS = ("panel degraded:", "synthesizer_failed")


def _is_no_response_text(value: Any) -> bool:
    return isinstance(value, str) and value.lstrip().startswith(NO_RESPONSE_PREFIX)


def _is_transport_summary(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    low = value.strip().lower()
    return any(marker in low for marker in _TRANSPORT_SUMMARY_MARKERS)


def panel_payload_transport_incomplete(raw: dict[str, Any]) -> bool:
    """True when a persisted panel verdict represents panel transport
    failure rather than a content review result.

    Older harness versions wrote missing reviewer / synthesizer failures as
    harness-authored invariant violations. Treat those as non-verdicts so the
    orchestrator retries the panel instead of spending a design revision.
    """
    per_vendor = raw.get("per_vendor_raw", {})
    if isinstance(per_vendor, dict) and any(
        _is_no_response_text(v) for v in per_vendor.values()
    ):
        return True
    findings = raw.get("findings", [])
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            if finding.get("vendor") == "harness" and _is_transport_summary(
                finding.get("summary")
            ):
                return True
    return False


def panel_verdict_transport_incomplete(v: "PanelVerdict") -> bool:
    if any(_is_no_response_text(v) for v in v.per_vendor_raw.values()):
        return True
    return any(
        f.vendor == "harness" and _is_transport_summary(f.summary)
        for f in v.findings
    )


@dataclass
class ReviewDecision:
    node: str
    outcome: DecisionOutcome
    blocking: bool
    severity: Severity
    summary: str
    prd_targeted: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "node": self.node,
            "outcome": self.outcome,
            "blocking": self.blocking,
            "severity": self.severity,
            "summary": self.summary,
        }
        if self.prd_targeted is not None:
            data["prd_targeted"] = self.prd_targeted
        return data


FindingCategory = Literal[
    "missing", "invented", "ambiguous", "undelivered", "missized",
    "untestable", "underspecified-contract", "other",
]
FailureClass = Literal["mainline", "edge"]
MissizedDirection = Literal["coarse", "fine"]

_VALID_CATEGORY = {
    "missing", "invented", "ambiguous", "undelivered", "missized",
    "untestable", "underspecified-contract", "other",
}
_VALID_FAILURE_CLASS = {"mainline", "edge"}
_VALID_MISSIZED_DIRECTION = {"coarse", "fine"}


@dataclass
class PanelFinding:
    severity: Severity
    vendor: str                        # claude / agy / codex
    summary: str
    cited_artifact_span: dict[str, Any] = field(default_factory=dict)
    # v3-core R1: filename-qualified target strings — "primary_pair.<filename>"
    # or "anchor.<filename>". Bare "primary_pair"/"anchor" supported read-only
    # for pre-extension files; v3 reviewers always emit filename-qualified.
    targets: list[str] = field(default_factory=list)
    # Rigor-tier structured fields (docs/proposals/rigor-tier.md).
    # `severity` holds the EFFECTIVE severity after the rigor filter;
    # `severity_reported` preserves the reviewer's original when the
    # filter changed it (absent ⇒ effective == reported).
    category: str | None = None                 # FindingCategory
    evidence_refs: list[str] = field(default_factory=list)
    failure_class: str | None = None            # FailureClass
    missized_direction: str | None = None       # MissizedDirection
    severity_reported: Severity | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "severity": self.severity,
            "vendor": self.vendor,
            "summary": self.summary,
            "cited_artifact_span": self.cited_artifact_span,
        }
        if self.targets:
            d["targets"] = list(self.targets)
        if self.category is not None:
            d["category"] = self.category
        if self.evidence_refs:
            d["evidence_refs"] = list(self.evidence_refs)
        if self.failure_class is not None:
            d["failure_class"] = self.failure_class
        if self.missized_direction is not None:
            d["missized_direction"] = self.missized_direction
        if self.severity_reported is not None and (
            self.severity_reported != self.severity
        ):
            d["severity_reported"] = self.severity_reported
        return d


@dataclass
class DroppedFinding:
    """v3-core R3: anchor-only-target finding moved out of findings[] into
    panel-verdict.json's dropped_findings[] audit array. Same schema as
    PanelFinding plus drop_reason."""
    severity: Severity
    vendor: str
    summary: str
    cited_artifact_span: dict[str, Any] = field(default_factory=dict)
    targets: list[str] = field(default_factory=list)
    drop_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "vendor": self.vendor,
            "summary": self.summary,
            "cited_artifact_span": self.cited_artifact_span,
            "targets": list(self.targets),
            "drop_reason": self.drop_reason,
        }


@dataclass
class PanelVerdict:
    gate: Gate
    verdict: Verdict
    findings: list[PanelFinding]
    # Upstream artifact this verdict validates
    source: str
    source_hash: str
    # R4b audit fields
    prompt_file: str
    prompt_hash: str
    harness_version: str
    run_ts: str
    # Audit trail of docs the panel consulted (hash-pinned). Populated
    # for every gate that reads anchors (scope.json, trace.md, prd.md,
    # prd-checklist.json, etc.); used by the cascade to detect when a doc the
    # verdict depends on has changed.
    consulted_docs: list[dict[str, str]] = field(default_factory=list)
    # R4d (skipped verdict — when user invoked skip-gate)
    skip_reason: str | None = None
    skip_who: str | None = None
    # G15 audit: raw reviewer markdown per vendor (empty pre-G15)
    per_vendor_raw: dict[str, str] = field(default_factory=dict)
    # v3-core R3: findings whose targets are all-anchor are moved here
    # after the synthesizer returns (harness post-processing).
    dropped_findings: list["DroppedFinding"] = field(default_factory=list)
    # Close-approval may include per-reviewer PRD coverage judgments.
    # This is review output, not an input to the gate.
    coverage_map: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    decision: ReviewDecision | None = None
    # Rigor filter audit: when the filter downgraded every blocking
    # finding and the harness therefore overrode a canonical
    # `retry_design` decision to `pass`, the original decision dict is
    # preserved here. `halt_for_human` is never overridden.
    decision_overridden_by_rigor: dict[str, Any] | None = None

    def has_invariant_violation(self) -> bool:
        return any(f.severity == "invariant_violation" for f in self.findings)

    def effectively_blocks(self) -> bool:
        """True if this verdict should block gate advance."""
        if self.verdict == "skipped":
            return False  # override path — surfaces in status; does not block
        if self.verdict == "pass" and not self.has_invariant_violation():
            return False
        return True

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "gate": self.gate,
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
            "source": self.source,
            "source_hash": self.source_hash,
            "prompt_file": self.prompt_file,
            "prompt_hash": self.prompt_hash,
            "harness_version": self.harness_version,
            "run_ts": self.run_ts,
        }
        if self.consulted_docs:
            d["consulted_docs"] = self.consulted_docs
        if self.skip_reason is not None:
            d["skip_reason"] = self.skip_reason
        if self.skip_who is not None:
            d["skip_who"] = self.skip_who
        if self.per_vendor_raw:
            d["per_vendor_raw"] = dict(self.per_vendor_raw)
        if self.dropped_findings:
            d["dropped_findings"] = [df.to_dict() for df in self.dropped_findings]
        if self.coverage_map:
            d["coverage_map"] = self.coverage_map
        if self.decision is not None:
            d["decision"] = self.decision.to_dict()
        if self.decision_overridden_by_rigor is not None:
            d["decision_overridden_by_rigor"] = dict(
                self.decision_overridden_by_rigor
            )
        return d


def _validate_decision(decision: dict[str, Any], gate: str) -> None:
    required = {"node", "outcome", "blocking", "severity", "summary"}
    missing = required.difference(decision)
    if missing:
        raise SchemaError(f"decision missing fields {sorted(missing)!r}")
    if gate != "design-review":
        raise SchemaError("canonical decision currently supported only for design-review")
    if decision["node"] != "design_review":
        raise SchemaError("design-review decision.node must be 'design_review'")
    if decision["outcome"] not in _VALID_DESIGN_REVIEW_OUTCOME:
        raise SchemaError(
            "design-review decision.outcome must be one of "
            f"{sorted(_VALID_DESIGN_REVIEW_OUTCOME)!r}"
        )
    if decision["severity"] not in _VALID_DECISION_SEVERITY:
        raise SchemaError(
            "design-review decision.severity must be one of "
            f"{sorted(_VALID_DECISION_SEVERITY)!r}"
        )
    if not isinstance(decision["blocking"], bool):
        raise SchemaError("design-review decision.blocking must be bool")
    if not isinstance(decision["summary"], str) or not decision["summary"].strip():
        raise SchemaError("design-review decision.summary must be a non-empty string")
    if "prd_targeted" in decision and not isinstance(decision["prd_targeted"], bool):
        raise SchemaError("design-review decision.prd_targeted must be bool")
    outcome = decision["outcome"]
    blocking = decision["blocking"]
    severity = decision["severity"]
    if outcome == "halt_for_human":
        if not blocking:
            raise SchemaError("design-review halt_for_human must be blocking=true")
        if severity not in {"invariant_violation", "risk"}:
            raise SchemaError(
                "design-review halt_for_human must use invariant_violation or risk severity"
            )
        return
    if blocking:
        raise SchemaError(
            "design-review blocking=true is valid only for halt_for_human decisions"
        )


def _validate(obj: dict) -> None:
    required = ("gate", "verdict", "findings", "source", "source_hash",
                "prompt_file", "prompt_hash", "harness_version", "run_ts")
    for k in required:
        if k not in obj:
            raise SchemaError(f"panel-verdict missing {k}")
    if obj["gate"] not in _VALID_GATE:
        raise SchemaError(f"panel-verdict.gate must be one of {_VALID_GATE}")
    if obj["verdict"] not in _VALID_VERDICT:
        raise SchemaError(f"panel-verdict.verdict must be one of {_VALID_VERDICT}")
    if not obj["source_hash"].startswith("sha256:"):
        raise SchemaError("source_hash must start with 'sha256:'")
    if not obj["prompt_hash"].startswith("sha256:"):
        raise SchemaError("prompt_hash must start with 'sha256:'")
    for i, f in enumerate(obj["findings"]):
        for k in ("severity", "vendor", "summary"):
            if k not in f:
                raise SchemaError(f"findings[{i}] missing {k}")
        if f["severity"] not in _VALID_SEVERITY:
            raise SchemaError(f"findings[{i}].severity must be one of {_VALID_SEVERITY}")
        if "category" in f and f["category"] not in _VALID_CATEGORY:
            raise SchemaError(
                f"findings[{i}].category must be one of {sorted(_VALID_CATEGORY)}"
            )
        if "failure_class" in f and f["failure_class"] not in _VALID_FAILURE_CLASS:
            raise SchemaError(
                f"findings[{i}].failure_class must be one of "
                f"{sorted(_VALID_FAILURE_CLASS)}"
            )
        if ("missized_direction" in f
                and f["missized_direction"] not in _VALID_MISSIZED_DIRECTION):
            raise SchemaError(
                f"findings[{i}].missized_direction must be one of "
                f"{sorted(_VALID_MISSIZED_DIRECTION)}"
            )
        if "severity_reported" in f and f["severity_reported"] not in _VALID_SEVERITY:
            raise SchemaError(
                f"findings[{i}].severity_reported must be one of {_VALID_SEVERITY}"
            )
    if "decision" in obj:
        if not isinstance(obj["decision"], dict):
            raise SchemaError("decision must be an object")
        _validate_decision(obj["decision"], obj["gate"])


def _load_targets(f: dict) -> list[str]:
    """Read ``targets`` from a finding dict. Absent/non-list → empty
    list (anchor-filter treats empty as primary-pair default, i.e.
    kept)."""
    t = f.get("targets")
    if isinstance(t, list):
        return [str(x) for x in t]
    return []


def load_verdict(path: Path) -> PanelVerdict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(raw)
    return PanelVerdict(
        gate=raw["gate"], verdict=raw["verdict"],
        findings=[
            PanelFinding(
                severity=f["severity"], vendor=f["vendor"],
                summary=f["summary"], cited_artifact_span=f.get("cited_artifact_span", {}),
                targets=_load_targets(f),
                category=f.get("category"),
                evidence_refs=[str(x) for x in f.get("evidence_refs", [])],
                failure_class=f.get("failure_class"),
                missized_direction=f.get("missized_direction"),
                severity_reported=f.get("severity_reported"),
            )
            for f in raw["findings"]
        ],
        source=raw["source"], source_hash=raw["source_hash"],
        prompt_file=raw["prompt_file"], prompt_hash=raw["prompt_hash"],
        harness_version=raw["harness_version"], run_ts=raw["run_ts"],
        consulted_docs=raw.get("consulted_docs", []),
        skip_reason=raw.get("skip_reason"),
        skip_who=raw.get("skip_who"),
        per_vendor_raw=raw.get("per_vendor_raw", {}),
        dropped_findings=[
            DroppedFinding(
                severity=df["severity"], vendor=df["vendor"],
                summary=df["summary"],
                cited_artifact_span=df.get("cited_artifact_span", {}),
                targets=list(df.get("targets", [])),
                drop_reason=df.get("drop_reason", ""),
            )
            for df in raw.get("dropped_findings", [])
        ],
        coverage_map=raw.get("coverage_map", {}),
        decision_overridden_by_rigor=raw.get("decision_overridden_by_rigor"),
        decision=(
            ReviewDecision(
                node=raw["decision"]["node"],
                outcome=raw["decision"]["outcome"],
                blocking=raw["decision"]["blocking"],
                severity=raw["decision"]["severity"],
                summary=raw["decision"]["summary"],
                prd_targeted=raw["decision"].get("prd_targeted"),
            )
            if isinstance(raw.get("decision"), dict) else None
        ),
    )


def write_verdict(path: Path, v: PanelVerdict) -> None:
    if not v.run_ts:
        v.run_ts = datetime.now(timezone.utc).isoformat()
    d = v.to_dict()
    _validate(d)
    atomic_write_json(Path(path), d)
