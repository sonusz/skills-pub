"""G15 panel runner — internal panel-review implementation.

Flow:
  1. Compose per-reviewer prompt (gate-specific review-<gate>.md +
     file manifest with paths + hashes for the artifact and consulted docs).
  2. Dispatch the configured reviewers (for example claude / grok / codex,
     vendors.yml panel config) in parallel.
  3. Invoke the configured synthesizer with the responding reviewer
     outputs under a pinned synthesize.md prompt and the pinned JSON
     schema. Parse structured output.
  4. Build PanelVerdict (with per_vendor_raw audit field) and write
     panel-verdict.json atomically.

Failure modes and their handling:
  - Reviewer timeout / empty output → retry once, force-refresh quota, then
    either use the configured response quorum for confirmed exhaustion or
    cache successes and halt for unknown/non-quota failures.
  - Synthesizer timeout / non-JSON / schema-invalid output → write a
    harness-authored audit verdict and halt instead of routing a content
    revision.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shlex
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from autodev import __version__ as HARNESS_VERSION
from autodev.artifacts.revision_state import load_state
from autodev.artifacts.verdict import (
    IssueCluster,
    NO_RESPONSE_PREFIX,
    PanelFinding,
    PanelVerdict,
    ReviewDecision,
    load_verdict,
    panel_verdict_transport_incomplete,
    write_verdict,
)
from autodev.artifacts.fingerprint_history import prior_cluster_catalog
from autodev.errors import ConfigError, GatePending, QuotaHalt, SchemaError
from autodev.panel.anchor_filter import filter_anchor_findings
from autodev.panel.rigor_filter import (
    RigorFilterResult, apply_rigor_filter, has_effective_blocking,
)
from autodev.panel.precheck import run_precheck
from autodev.panel.schemas import synthesizer_output_schema_json
from autodev.state.hashing import hash_file
from autodev.state.atomic import atomic_write_json
from autodev.state.log import JsonlLog
from autodev.state.process_registry import registry_path

from autodev.vendors.config import (
    ProbeConfig,
    PanelConfig, PanelReviewerSpec, PanelSynthesizerSpec,
)
from autodev.vendors.fallback import build_candidates, resolve_candidate
from autodev.vendors.quota import get_remaining as get_quota_remaining
from autodev.vendors.session_keys import DEFAULT_SESSION_MAX_TURNS
from autodev.vendors.shared_call import SHARED_VENDORS_DIR, call_shared_vendor
from autodev.vendors.subprocess_runner import (
    _build_idle_callback,
    _hard_backstop_sec,
)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

REVIEW_PROMPT_FILE = {
    "design-review": "review-design-review.md",
    "trace-review":  "review-trace-review.md",
    "close-approval": "review-close-approval.md",
}
SYNTHESIZE_PROMPT_FILENAME = "synthesize.md"

# When gate=="design-review" the panel runs TWO reviewer groups in parallel.
# Each group has its own prompt file and consulted_docs filter. Both groups
# share the same primary_artifact (design-packet.json). After the reviewers
# finish, each group runs its own synthesizer; the harness writes TWO verdict
# files (panel-design-review.json + panel-trace-review.json) referencing the
# same packet. The orchestrator merges findings from both files when routing.
DESIGN_REVIEW_GROUPS: tuple[dict, ...] = (
    {
        "name": "design-review",
        "prompt_file": "review-design-review.md",
        "verdict_file": "panel-design-review.json",
        # Restrict to the design reviewer's own inputs: design.md + scope.json
        # + prd.md (~250KB). The pass-through `None` handed it ALL consulted
        # docs (~985KB across 33 files: design+scope+trace+test + ~14 referenced
        # source files + the 151KB changelog + arch/deploy), overflowing agentic
        # reviewers (codex). trace.md + test-plan.md are the parallel
        # trace-review group's job (see review-design-review.md, which tells
        # this panel not to audit them), so they are intentionally excluded.
        # Verified: codex survives at this size AND the design reviewer stays
        # design-focused (no spurious test-coverage findings).
        "consulted_filter": {
            "design.md", "scope.json", "prd.md", "panel-coverage-map.json",
        },
    },
    {
        "name": "trace-review",
        "prompt_file": "review-trace-review.md",
        "verdict_file": "panel-trace-review.json",
        # Restrict to behavioral artifacts only
        "consulted_filter": {
            "trace.md", "test-plan.md", "prd.md", "panel-coverage-map.json",
        },
    },
)

# Reviewer transport failures get one immediate retry. A panel may proceed with
# its configured response quorum only when every omitted reviewer is positively
# confirmed quota-exhausted; unknown and non-quota failures remain blocking.
# Successful responses and quota-skip audit are cached for exact panel inputs;
# only successful responses are reusable on a later run.
REVIEWER_CACHE_SCHEMA_VERSION = 1
REVIEWER_ATTEMPTS_PER_RUN = 2
QUOTA_SKIPPED_PREFIX = "[SKIPPED: quota confirmed"

DOCTOR_SCRIPT = SHARED_VENDORS_DIR / "scripts" / "doctor.sh"

# Override: path to a fake invoker script used by tests. When set, both
# reviewer and synthesizer calls route through the fake instead of the
# real shared-vendors call. The fake receives (vendor, role, prompt) via env vars
# + stdin and writes markdown or JSON to stdout.
FAKE_INVOKER_ENV = "AUTODEV_PANEL_FAKE_INVOKER"


def _finding_from_synth(vendor: str, f: dict) -> PanelFinding:
    """One synthesizer finding entry → PanelFinding. Must carry ALL
    structured fields through — dropping the rigor-tier fields here
    silently fail-closes the rigor filter on every finding."""
    return PanelFinding(
        severity=f.get("severity", "opinion"),
        vendor=vendor,
        summary=f.get("summary", ""),
        targets=list(f.get("targets", [])),
        category=f.get("category"),
        evidence_refs=[str(x) for x in f.get("evidence_refs", [])],
        failure_class=f.get("failure_class"),
        missized_direction=f.get("missized_direction"),
        priority=f.get("priority"),
        finding_id=f.get("finding_id"),
    )


def _apply_rigor_to_findings(
    findings: list[PanelFinding], *, feature_active: Path,
) -> tuple[RigorFilterResult, str]:
    """Load the Assurance map (prd.md) + scope.json tolerantly and run
    the rigor filter. Any load failure degrades to the all-strict
    no-op (== current behavior), never to a crash inside verdict
    assembly."""
    from autodev.artifacts.scope import load_scope
    from autodev.assurance import AssuranceMap, parse_assurance

    assurance = AssuranceMap()
    prd_path = feature_active / "prd.md"
    try:
        if prd_path.exists():
            assurance, _ = parse_assurance(
                prd_path.read_text(encoding="utf-8")
            )
    except Exception:
        assurance = AssuranceMap()
    scope = None
    scope_path = feature_active / "scope.json"
    try:
        if scope_path.exists():
            scope = load_scope(scope_path)
    except Exception:
        scope = None
    return apply_rigor_filter(findings, assurance, scope), assurance.release_threshold


def _build_issue_clusters(
    findings: list[PanelFinding], payload: dict, *, feature_active: Path,
    gate: str,
) -> list[IssueCluster]:
    """Validate synthesizer clustering without making semantic judgments."""
    ids = [f.finding_id for f in findings]
    if any(not finding_id for finding_id in ids):
        raise SchemaError("synthesizer finding missing finding_id")
    if len(set(ids)) != len(ids):
        raise SchemaError("synthesizer finding_id values are not unique")
    by_id = {f.finding_id: f for f in findings}
    raw_clusters = payload.get("issue_clusters")
    if not isinstance(raw_clusters, list):
        raise SchemaError("synthesizer output missing issue_clusters array")
    prior_ids = {
        c["cluster_id"] for c in prior_cluster_catalog(feature_active, gate)
        if isinstance(c.get("cluster_id"), str)
    }
    assigned: set[str] = set()
    reused: set[str] = set()
    result: list[IssueCluster] = []
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    for index, raw in enumerate(raw_clusters):
        member_ids = raw.get("finding_ids") if isinstance(raw, dict) else None
        if not isinstance(member_ids, list) or not member_ids:
            raise SchemaError(f"issue_clusters[{index}] has no finding_ids")
        if len(set(member_ids)) != len(member_ids):
            raise SchemaError(f"issue_clusters[{index}] repeats a finding_id")
        unknown = set(member_ids) - set(by_id)
        if unknown:
            raise SchemaError(
                f"issue_clusters[{index}] references unknown finding ids "
                f"{sorted(unknown)!r}"
            )
        duplicate = set(member_ids) & assigned
        if duplicate:
            raise SchemaError(
                f"findings assigned to multiple issue clusters: {sorted(duplicate)!r}"
            )
        prior_id = raw.get("prior_cluster_id")
        if prior_id is not None:
            if prior_id not in prior_ids:
                raise SchemaError(
                    f"issue_clusters[{index}] reuses unknown prior cluster "
                    f"{prior_id!r}"
                )
            if prior_id in reused:
                raise SchemaError(f"prior cluster {prior_id!r} reused more than once")
            cluster_id = prior_id
            reused.add(prior_id)
        else:
            identity = json.dumps({
                "summary": raw.get("summary", "").strip().lower(),
                "members": sorted(member_ids),
            }, sort_keys=True, separators=(",", ":"))
            cluster_id = "issue-" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:16]
        members = [by_id[i] for i in member_ids]
        priority = min(
            (f.effective_priority() for f in members),
            key=priority_rank.__getitem__,
        )
        summary = raw.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise SchemaError(f"issue_clusters[{index}] summary is empty")
        result.append(IssueCluster(
            cluster_id=cluster_id,
            finding_ids=list(member_ids),
            summary=summary.strip(),
            priority=priority,
        ))
        assigned.update(member_ids)
    missing = set(by_id) - assigned
    if missing:
        raise SchemaError(
            "issue_clusters must assign every raw finding exactly once; "
            f"missing {sorted(missing)!r}"
        )
    if len({c.cluster_id for c in result}) != len(result):
        raise SchemaError("issue cluster IDs are not unique")
    return result


def _trim_issue_clusters(
    clusters: list[IssueCluster], findings: list[PanelFinding],
) -> list[IssueCluster]:
    """Remove anchor-dropped members while retaining validated grouping."""
    by_id = {f.finding_id: f for f in findings if f.finding_id}
    rank = {"P0": 0, "P1": 1, "P2": 2}
    trimmed: list[IssueCluster] = []
    for cluster in clusters:
        member_ids = [i for i in cluster.finding_ids if i in by_id]
        if not member_ids:
            continue
        priority = min(
            (by_id[i].effective_priority() for i in member_ids),
            key=rank.__getitem__,
        )
        trimmed.append(IssueCluster(
            cluster_id=cluster.cluster_id, finding_ids=member_ids,
            summary=cluster.summary, priority=priority,
        ))
    return trimmed


def _doctor_hint() -> str:
    """Human-readable pointer to the bundled shared-vendors doctor."""
    if DOCTOR_SCRIPT.exists():
        return f"run `{DOCTOR_SCRIPT}` to probe shared vendor calls"
    return (
        f"install shared/vendors at `{SHARED_VENDORS_DIR}`, "
        "or probe each vendor CLI manually"
    )


@dataclass
class ReviewerResult:
    vendor: str
    model: str
    ok: bool
    output: str
    elapsed_sec: float
    failure_detail: str = ""
    attempt_count: int = 1
    failure_history: tuple[str, ...] = ()
    quota_skipped: bool = False
    quota_confirmation: dict | None = None


def review_prompt_path(gate: str) -> Path:
    if gate not in REVIEW_PROMPT_FILE:
        raise ConfigError(f"unknown panel gate: {gate!r}")
    return PROMPTS_DIR / REVIEW_PROMPT_FILE[gate]


def synthesize_prompt_path() -> Path:
    return PROMPTS_DIR / SYNTHESIZE_PROMPT_FILENAME


def _hash_or_missing(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return hash_file(path)


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reviewer_cache_path(feature_active: Path, gate_label: str) -> Path:
    return feature_active / f"panel-{gate_label}.reviewers.json"


def _is_no_response_text(value: str) -> bool:
    return value.lstrip().startswith(NO_RESPONSE_PREFIX)


def _reviewer_cache_metadata(
    *,
    gate_label: str,
    primary_artifact: Path,
    prompt_file_for_audit: Path,
    reviewer_prompt: str,
    consulted_docs: list[dict],
) -> dict:
    return {
        "kind": "panel-reviewer-cache",
        "schema_version": REVIEWER_CACHE_SCHEMA_VERSION,
        "gate": gate_label,
        "source": str(primary_artifact),
        "source_hash": hash_file(primary_artifact),
        "prompt_file": str(prompt_file_for_audit),
        "prompt_hash": hash_file(prompt_file_for_audit),
        "reviewer_prompt_hash": _hash_text(reviewer_prompt),
        "consulted_docs": consulted_docs,
    }


def _cache_matches(payload: dict, metadata: dict) -> bool:
    for key in (
        "kind",
        "schema_version",
        "gate",
        "source",
        "source_hash",
        "prompt_file",
        "prompt_hash",
        "reviewer_prompt_hash",
        "consulted_docs",
    ):
        if payload.get(key) != metadata.get(key):
            return False
    return True


def _load_reviewer_cache(
    *,
    feature_active: Path,
    gate_label: str,
    reviewer_specs: tuple[PanelReviewerSpec, ...],
    metadata: dict,
) -> tuple[list[ReviewerResult], dict[str, dict]]:
    """Load reusable successful reviewer slots for this exact panel input.

    Failures and quota skips remain audit-only and are retried on the next run.
    In particular, a quota skip cannot stay settled after ``quota-resume`` has
    confirmed recovery.
    """
    path = _reviewer_cache_path(feature_active, gate_label)
    payload: dict = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    if not payload or not _cache_matches(payload, metadata):
        payload = _legacy_cache_payload_from_verdict(
            feature_active=feature_active,
            gate_label=gate_label,
            reviewer_specs=reviewer_specs,
            metadata=metadata,
        )
    if not payload or not _cache_matches(payload, metadata):
        return [], {}

    specs_by_vendor = {spec.vendor: spec for spec in reviewer_specs}
    results: list[ReviewerResult] = []
    reviewers = payload.get("reviewers", {})
    if isinstance(reviewers, dict):
        for vendor, entry in reviewers.items():
            if not isinstance(entry, dict):
                continue
            spec = specs_by_vendor.get(vendor)
            if spec is None or entry.get("model") != spec.model:
                continue
            output = entry.get("output")
            if not isinstance(output, str) or not output.strip():
                continue
            results.append(
                ReviewerResult(
                    vendor=vendor,
                    model=spec.model,
                    ok=True,
                    output=output.strip(),
                    elapsed_sec=float(entry.get("elapsed_sec") or 0.0),
                    attempt_count=int(entry.get("attempt_count") or 1),
                    failure_history=tuple(entry.get("failure_history") or ()),
                )
            )

    failures = payload.get("failures", {})
    return results, failures if isinstance(failures, dict) else {}


def _legacy_cache_payload_from_verdict(
    *,
    feature_active: Path,
    gate_label: str,
    reviewer_specs: tuple[PanelReviewerSpec, ...],
    metadata: dict,
) -> dict:
    """Convert a pre-fix incomplete final verdict into reviewer cache.

    Old runs persisted missing reviewer transport failures inside
    panel-<gate>.json. Reusing the successful reviewer raw outputs lets the
    next restart retry only the reviewers that failed in that old run.
    """
    path = feature_active / f"panel-{gate_label}.json"
    try:
        v = load_verdict(path)
    except Exception:
        return {}
    if v.gate != gate_label:
        return {}
    if v.source != metadata["source"] or v.source_hash != metadata["source_hash"]:
        return {}
    if v.consulted_docs != metadata["consulted_docs"]:
        return {}
    if not panel_verdict_transport_incomplete(v):
        return {}
    specs_by_vendor = {spec.vendor: spec for spec in reviewer_specs}
    reviewers: dict[str, dict] = {}
    failures: dict[str, dict] = {}
    for vendor, raw in v.per_vendor_raw.items():
        spec = specs_by_vendor.get(vendor)
        if spec is None:
            continue
        if _is_no_response_text(raw):
            failures[vendor] = {
                "vendor": vendor,
                "model": spec.model,
                "failure_detail": raw,
                "run_ts": v.run_ts,
            }
            continue
        if raw.strip():
            reviewers[vendor] = {
                "vendor": vendor,
                "model": spec.model,
                "output": raw.strip(),
                "elapsed_sec": 0.0,
                "run_ts": v.run_ts,
            }
    if not reviewers:
        return {}
    return {**metadata, "reviewers": reviewers, "failures": failures}


def _write_reviewer_cache(
    *,
    feature_active: Path,
    gate_label: str,
    reviewer_specs: tuple[PanelReviewerSpec, ...],
    metadata: dict,
    reviewer_results: list[ReviewerResult],
    prior_failures: dict[str, dict],
) -> None:
    specs_by_vendor = {spec.vendor: spec for spec in reviewer_specs}
    reviewers: dict[str, dict] = {}
    quota_skipped: dict[str, dict] = {}
    failures: dict[str, dict] = {}
    now = datetime.now(timezone.utc).isoformat()
    for result in reviewer_results:
        spec = specs_by_vendor.get(result.vendor)
        if spec is None:
            continue
        if result.ok and result.output.strip():
            reviewers[result.vendor] = {
                "vendor": result.vendor,
                "model": spec.model,
                "output": result.output.strip(),
                "elapsed_sec": result.elapsed_sec,
                "attempt_count": result.attempt_count,
                "failure_history": list(result.failure_history),
                "run_ts": now,
            }
        elif result.quota_skipped and result.quota_confirmation:
            quota_skipped[result.vendor] = {
                "vendor": result.vendor,
                "model": spec.model,
                "failure_detail": result.failure_detail,
                "elapsed_sec": result.elapsed_sec,
                "attempt_count": result.attempt_count,
                "failure_history": list(result.failure_history),
                "quota_confirmation": result.quota_confirmation,
                "run_ts": now,
            }
        else:
            failures[result.vendor] = {
                "vendor": result.vendor,
                "model": spec.model,
                "failure_detail": result.failure_detail,
                "elapsed_sec": result.elapsed_sec,
                "attempt_count": result.attempt_count,
                "failure_history": list(result.failure_history),
                "run_ts": now,
            }
    for vendor, failure in prior_failures.items():
        if (
            vendor not in failures
            and vendor not in reviewers
            and vendor not in quota_skipped
        ):
            failures[vendor] = failure
    atomic_write_json(
        _reviewer_cache_path(feature_active, gate_label),
        {
            **metadata,
            "reviewers": reviewers,
            "quota_skipped": quota_skipped,
            "failures": failures,
        },
    )


def _missing_reviewers(
    reviewer_specs: tuple[PanelReviewerSpec, ...],
    reviewer_results: list[ReviewerResult],
) -> list[PanelReviewerSpec]:
    settled_vendors = {
        r.vendor
        for r in reviewer_results
        if (r.ok and r.output.strip()) or r.quota_skipped
    }
    return [spec for spec in reviewer_specs if spec.vendor not in settled_vendors]


def _panel_incomplete_message(
    *,
    gate_label: str,
    reviewer_specs: tuple[PanelReviewerSpec, ...],
    reviewer_results: list[ReviewerResult],
    min_responding_reviewers: int,
) -> str:
    ok_vendors = [r.vendor for r in reviewer_results if r.ok and r.output.strip()]
    missing = _missing_reviewers(reviewer_specs, reviewer_results)
    quota_skipped = [r.vendor for r in reviewer_results if r.quota_skipped]
    details = {
        r.vendor: r.failure_detail
        for r in reviewer_results
        if not r.ok and r.failure_detail
    }
    return (
        f"panel {gate_label} incomplete: {len(ok_vendors)} reviewers responded "
        f"(quorum={min_responding_reviewers}, configured={len(reviewer_specs)}); "
        f"unsettled={[m.vendor for m in missing]!r}; "
        f"quota_skipped={quota_skipped!r}. Each unsettled reviewer was retried "
        f"at least once in this run. Successful reviewers are cached; restart "
        "the run to retry only unsettled reviewer(s). "
        f"Debug: {_doctor_hint()}. Details: {details!r}"
    )


def _file_ref_line(*, label: str, path: Path, hash_value: str | None = None) -> str:
    exists = path.exists()
    size = path.stat().st_size if exists else 0
    h = hash_value or _hash_or_missing(path)
    return f"- {label}: `{path}` hash=`{h}` size_bytes={size}"


def _design_revision_delta_block(
    *, gate: str, feature_active: Path | None, repo_root: Path | None,
) -> str:
    """Render package Git refs as revision-navigation input for reviewers."""
    if (
        gate not in {"design-review", "trace-review"}
        or feature_active is None
        or repo_root is None
    ):
        return ""
    from autodev.artifacts.design_package_history import (
        latest_design_revision_refs,
    )

    revision = latest_design_revision_refs(feature_active)
    if revision is None:
        return ""
    current_ref = revision["current_ref"]
    previous_ref = revision["previous_ref"]
    out = "\n## Design revision delta\n\n"
    out += f"- CURRENT_DESIGN_PACKAGE: `{revision['current_package']}`\n"
    out += f"- CURRENT_DESIGN_REF: `{current_ref}`\n"
    if not previous_ref:
        out += "- PREVIOUS_DESIGN_REF: `(none — initial package)`\n"
        return out

    out += f"- PREVIOUS_DESIGN_REF: `{previous_ref}`\n"
    active_rel = feature_active.resolve().relative_to(repo_root.resolve())
    names = (
        ("design.md", "scope.json", "design-changelog.json")
        if gate == "design-review"
        else ("trace.md", "test-plan.md", "design-changelog.json")
    )
    paths = [(active_rel / name).as_posix() for name in names]
    base = [
        "git", "-C", str(repo_root), "diff", "--no-ext-diff",
        "--find-renames", str(previous_ref), str(current_ref), "--",
        *paths,
    ]
    stat = base[:4] + ["--stat", *base[4:]]
    out += f"- DIFF_STAT_COMMAND: `{shlex.join(stat)}`\n"
    out += f"- DIFF_COMMAND: `{shlex.join(base)}`\n"
    out += (
        "\nRead the revision diff first. Use it to navigate to the changed "
        "current sections, then inspect enough unchanged current context to "
        "check cross-section consistency. The diff is navigation evidence, "
        "not a substitute for judging the authoritative current files.\n"
    )
    return out


def _compose_reviewer_prompt(
    *, gate: str, artifact_path: Path, consulted_docs: list[dict],
    feature_active: Path | None = None, repo_root: Path | None = None,
    continuation: bool = False,
) -> str:
    """Build an initial or continuation prompt sent to each reviewer.

    Large review inputs are passed by file reference, not inlined. The
    verdict records the same paths + hashes, so the harness can invalidate
    stale panel results without relying on prompt-sized document snapshots.
    """
    if continuation:
        prompt_text = (
            f"# Continue the existing `{gate}` reviewer session\n\n"
            "Keep the review role, severity rules, output contract, and "
            "independence established by the initial turn. Re-read the "
            "referenced files: their current hashes and contents are "
            "authoritative. Review the current revision afresh, retain "
            "unresolved findings, and do not repeat findings that the files "
            "now resolve. If REVIEW_PROMPT_HASH changed, re-read "
            "REVIEW_PROMPT_FILE before judging. Return a complete reviewer "
            "response for this turn. When a Design revision delta is provided "
            "below, use it before re-reading affected current sections; do not "
            "spend context re-reading unchanged files wholesale unless needed "
            "to validate a cross-section invariant."
        )
    else:
        prompt_text = review_prompt_path(gate).read_text(encoding="utf-8")
    prompt_text += "\n\n---\n\n## Orchestrator context\n\n"
    if feature_active is not None:
        prompt_text += f"- FEATURE_ACTIVE: `{feature_active}`\n"
    if repo_root is not None:
        prompt_text += f"- REPO_ROOT: `{repo_root}`\n"
    prompt_text += f"- GATE: `{gate}`\n"
    prompt_file = review_prompt_path(gate)
    prompt_text += f"- REVIEW_PROMPT_FILE: `{prompt_file}`\n"
    prompt_text += f"- REVIEW_PROMPT_HASH: `{hash_file(prompt_file)}`\n"
    prompt_text += _design_revision_delta_block(
        gate=gate,
        feature_active=feature_active,
        repo_root=repo_root,
    )
    prompt_text += "\n## Required file inputs\n\n"
    prompt_text += (
        "The files you must judge are listed below as paths (with hash and "
        "size_bytes) — their contents are NOT inlined. Read them yourself "
        "with your CLI's file tools (Read / shell). Use the size_bytes to "
        "plan your reading order and depth as you see fit. Do not infer from "
        "this manifest alone. If a required file cannot be read, report a "
        "`risk` finding targeting the inaccessible file.\n\n"
    )
    prompt_text += _file_ref_line(label="PRIMARY_ARTIFACT", path=artifact_path) + "\n"
    if consulted_docs:
        prompt_text += "\n### Consulted documents\n\n"
        for doc_meta in consulted_docs:
            d = Path(doc_meta["path"])
            priority = doc_meta.get("priority", "consulted")
            prompt_text += _file_ref_line(
                label=f"{priority.upper()}:{d.name}",
                path=d,
                hash_value=doc_meta.get("hash"),
            ) + "\n"
    return prompt_text


def _invoke_reviewer(
    spec: PanelReviewerSpec, prompt: str, probe_interval_sec: int,
    *, cwd: Path | None = None,
    feature_active: Path | None = None,
    probe_config: ProbeConfig | None = None,
    log_emit: Callable[[dict], None] | None = None,
    session_key: str | None = None,
    resume_prompt: str | None = None,
    force_quota: bool = False,
) -> ReviewerResult:
    """Run one reviewer CLI. Captures stdout; empty on failure.

    `probe_interval_sec` is the idle-probe threshold (the same field
    the stage subprocesses use). When `probe_config` is supplied, the
    polling loop in `call_shared_vendor` consults `run_idle_probe`
    after this many seconds of stream-output silence; otherwise the
    legacy blocking path is used and the same value backs the bash
    wall-clock timeout.
    """
    import time
    fake = os.environ.get(FAKE_INVOKER_ENV)
    t0 = time.monotonic()
    timeout_sec = probe_interval_sec  # for fake-mode subprocess + error messages
    # Quota gate (fail-closed; QuotaHalt propagates to the panel runner, which
    # cancels siblings and re-raises). Resolved BEFORE the try so it is never
    # swallowed as a reviewer failure. Skipped in fake/test mode. On a fallback,
    # `spec` is rebound to the chosen LLM so every downstream reference (the call
    # AND the recorded ReviewerResult vendor/model) reflects what actually ran.
    if not fake:
        _cand = resolve_candidate(
            build_candidates(spec),
            role=f"reviewer:{spec.vendor}",
            logger=(lambda m: log_emit({"event": "quota", "role": "reviewer", "msg": m}))
            if log_emit
            else None,
            force=force_quota,
        )
        spec = PanelReviewerSpec(
            vendor=_cand.vendor, model=_cand.model, effort=_cand.effort
        )
    try:
        if fake:
            env = os.environ.copy()
            env["AUTODEV_PANEL_FAKE_ROLE"] = "reviewer"
            env["AUTODEV_PANEL_FAKE_VENDOR"] = spec.vendor
            env["AUTODEV_PANEL_FAKE_MODEL"] = spec.model
            proc = subprocess.run(
                [fake], input=prompt, capture_output=True, text=True,
                env=env, timeout=timeout_sec,
            )
        else:
            idle_callback = (
                _build_idle_callback(
                    stage=f"panel-reviewer-{spec.vendor}",
                    stage_probe_interval_sec=probe_interval_sec,
                    stdout_path=Path("/dev/null"),
                    stderr_path=Path("/dev/null"),
                    probe_config=probe_config,
                    log_emit=log_emit,
                    process_registry=(
                        registry_path(feature_active)
                        if feature_active is not None else None
                    ),
                )
                if probe_config is not None
                else None
            )
            hard_backstop = (
                _hard_backstop_sec(probe_interval_sec)
                if probe_config is not None
                else probe_interval_sec
            )
            result = call_shared_vendor(
                vendor=spec.vendor,
                model=spec.model,
                prompt=prompt,
                output_id=f"reviewer-{spec.vendor}",
                timeout_sec=hard_backstop,
                cwd=cwd,
                effort=spec.effort,
                # Reviewers run yolo: sandbox bypassed so they can read
                # cross-repo material and reach the network. They do NOT
                # need to write (deliverable is stdout); the write-integrity
                # guard in panel/__init__.py backstops any stray mutation.
                yolo=True,
                idle_callback=idle_callback,
                process_registry=(
                    registry_path(feature_active)
                    if feature_active is not None else None
                ),
                process_label=f"panel-reviewer:{spec.vendor}",
                session_key=session_key,
                session_max_turns=(
                    DEFAULT_SESSION_MAX_TURNS
                    if session_key is not None else None
                ),
                resume_prompt=resume_prompt,
            )
            if getattr(result, "session_auto_reset", False) and log_emit is not None:
                log_emit({
                    "event": "session-auto-reset",
                    "role": "reviewer",
                    "vendor": spec.vendor,
                    "turn": getattr(result, "session_turn", None),
                    "max_turns": getattr(result, "session_max_turns", None),
                })
            elapsed = time.monotonic() - t0
            if result.returncode != 0:
                detail = (
                    f"timeout after {timeout_sec}s"
                    if result.timed_out
                    else f"exit {result.returncode}: "
                         f"{(result.log or result.summary_stderr)[-500:]}"
                )
                return ReviewerResult(
                    vendor=spec.vendor, model=spec.model, ok=False, output="",
                    elapsed_sec=elapsed,
                    failure_detail=detail,
                )
            output = result.output.strip()
            if not output:
                return ReviewerResult(
                    vendor=spec.vendor, model=spec.model, ok=False, output="",
                    elapsed_sec=elapsed, failure_detail="empty stdout",
                )
            return ReviewerResult(
                vendor=spec.vendor, model=spec.model, ok=True, output=output,
                elapsed_sec=elapsed,
            )
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            return ReviewerResult(
                vendor=spec.vendor, model=spec.model, ok=False, output="",
                elapsed_sec=elapsed,
                failure_detail=f"exit {proc.returncode}: {proc.stderr[-500:]}",
            )
        output = proc.stdout.strip()
        if not output:
            return ReviewerResult(
                vendor=spec.vendor, model=spec.model, ok=False, output="",
                elapsed_sec=elapsed, failure_detail="empty stdout",
            )
        return ReviewerResult(
            vendor=spec.vendor, model=spec.model, ok=True, output=output,
            elapsed_sec=elapsed,
        )
    except subprocess.TimeoutExpired:
        return ReviewerResult(
            vendor=spec.vendor, model=spec.model, ok=False, output="",
            elapsed_sec=time.monotonic() - t0,
            failure_detail=f"timeout after {timeout_sec}s",
        )
    except FileNotFoundError as e:
        return ReviewerResult(
            vendor=spec.vendor, model=spec.model, ok=False, output="",
            elapsed_sec=time.monotonic() - t0,
            failure_detail=str(e),
        )


def _quota_halt_confirmation(exc: QuotaHalt) -> dict:
    """Turn a quota preflight halt into a positive/unknown audit record."""
    diagnostics = [d for d in exc.diagnostics if isinstance(d, dict)]
    confirmed = bool(diagnostics) and all(
        isinstance(d.get("remaining_pct"), (int, float))
        and not isinstance(d.get("remaining_pct"), bool)
        and isinstance(d.get("min_quota_pct"), (int, float))
        and not isinstance(d.get("min_quota_pct"), bool)
        and float(d["remaining_pct"]) < float(d["min_quota_pct"])
        for d in diagnostics
    )
    return {
        "confirmed": confirmed,
        "source": "quota-preflight",
        "diagnostics": diagnostics,
        "resume_at": exc.resume_at.isoformat() if exc.resume_at else None,
    }


def _quota_floor_for_result(
    spec: PanelReviewerSpec, result: ReviewerResult,
) -> float:
    """Return the configured floor for the candidate that actually ran."""
    for candidate in build_candidates(spec):
        if candidate.vendor == result.vendor and candidate.model == result.model:
            return float(candidate.min_quota_pct or 0.0)
    return float(spec.min_quota_pct or 0.0)


def _confirm_quota_after_failures(
    spec: PanelReviewerSpec, result: ReviewerResult,
) -> dict:
    """Force-refresh quota after a real retry; unknown never means confirmed."""
    configured_floor = _quota_floor_for_result(spec, result)
    # Ungated reviewers still need a positive recovery threshold in a quota
    # pause record; otherwise 0% would satisfy ``remaining >= 0`` immediately.
    recovery_floor = configured_floor if configured_floor > 0.0 else 0.000001
    quota = get_quota_remaining(result.vendor, result.model, force=True)
    remaining = quota.remaining_pct
    confirmed = remaining is not None and float(remaining) < recovery_floor
    return {
        "confirmed": confirmed,
        "source": "post-retry-quota-refresh",
        "vendor": result.vendor,
        "model": result.model,
        "remaining_pct": remaining,
        "min_quota_pct": recovery_floor,
        "configured_min_quota_pct": configured_floor,
        "resets_at": quota.resets_at.isoformat() if quota.resets_at else None,
        "fetched_at": quota.fetched_at.isoformat(),
        "detail": quota.detail,
        "error": quota.error,
    }


def _prior_failure_audit(prior_failure: dict | None) -> tuple[int, list[str]]:
    if not prior_failure:
        return 0, []
    raw_count = prior_failure.get("attempt_count", 1)
    count = raw_count if isinstance(raw_count, int) and raw_count >= 0 else 1
    history = prior_failure.get("failure_history")
    if isinstance(history, list):
        return count, [str(item) for item in history]
    detail = prior_failure.get("failure_detail")
    return count, [str(detail)] if detail else []


def _invoke_reviewer_with_retry(
    spec: PanelReviewerSpec,
    prompt: str,
    probe_interval_sec: int,
    *,
    prior_failure: dict | None = None,
    cwd: Path | None = None,
    feature_active: Path | None = None,
    probe_config: ProbeConfig | None = None,
    log_emit: Callable[[dict], None] | None = None,
    session_key: str | None = None,
    resume_prompt: str | None = None,
) -> ReviewerResult:
    """Try a reviewer twice, then omit it only on confirmed quota exhaustion."""
    prior_attempts, history = _prior_failure_audit(prior_failure)
    elapsed = 0.0
    last_result: ReviewerResult | None = None

    for attempt in range(1, REVIEWER_ATTEMPTS_PER_RUN + 1):
        try:
            result = _invoke_reviewer(
                spec,
                prompt,
                probe_interval_sec,
                cwd=cwd,
                feature_active=feature_active,
                probe_config=probe_config,
                log_emit=log_emit,
                session_key=session_key,
                resume_prompt=resume_prompt,
                force_quota=attempt > 1,
            )
        except QuotaHalt as exc:
            confirmation = _quota_halt_confirmation(exc)
            history.append(
                f"attempt {attempt}: quota preflight halted "
                f"(confirmed={confirmation['confirmed']})"
            )
            if (
                confirmation["confirmed"]
                and attempt == REVIEWER_ATTEMPTS_PER_RUN
            ):
                return ReviewerResult(
                    vendor=spec.vendor,
                    model=spec.model,
                    ok=False,
                    output="",
                    elapsed_sec=elapsed,
                    failure_detail="quota exhausted before reviewer launch",
                    attempt_count=prior_attempts + attempt,
                    failure_history=tuple(history),
                    quota_skipped=True,
                    quota_confirmation=confirmation,
                )
            if attempt < REVIEWER_ATTEMPTS_PER_RUN:
                continue
            return ReviewerResult(
                vendor=spec.vendor,
                model=spec.model,
                ok=False,
                output="",
                elapsed_sec=elapsed,
                failure_detail=(
                    "reviewer quota could not be positively confirmed; "
                    "quota remained unknown after retry"
                ),
                attempt_count=prior_attempts + attempt,
                failure_history=tuple(history),
            )

        elapsed += result.elapsed_sec
        if result.ok and result.output.strip():
            result.elapsed_sec = elapsed
            result.attempt_count = prior_attempts + attempt
            result.failure_history = tuple(history)
            return result

        last_result = result
        history.append(f"attempt {attempt}: {result.failure_detail or 'no output'}")
        if attempt < REVIEWER_ATTEMPTS_PER_RUN:
            continue

    assert last_result is not None
    confirmation = _confirm_quota_after_failures(spec, last_result)
    return ReviewerResult(
        vendor=last_result.vendor,
        model=last_result.model,
        ok=False,
        output="",
        elapsed_sec=elapsed,
        failure_detail=last_result.failure_detail,
        attempt_count=prior_attempts + REVIEWER_ATTEMPTS_PER_RUN,
        failure_history=tuple(history),
        quota_skipped=bool(confirmation["confirmed"]),
        quota_confirmation=confirmation,
    )


def _reviewer_session_key_for_spec(
    *,
    feature_active: Path,
    gate: str,
    reviewer_specs: Sequence[PanelReviewerSpec],
    spec: PanelReviewerSpec,
) -> str:
    """Return the stable session key for a configured reviewer slot."""

    from autodev.vendors.session_keys import reviewer_session_key

    for slot, configured in enumerate(reviewer_specs):
        if configured == spec:
            return reviewer_session_key(
                feature_active,
                gate=gate,
                slot=slot,
                configured_vendor=configured.vendor,
                configured_model=configured.model,
            )
    raise ValueError(f"reviewer spec is not present in panel config: {spec!r}")


def _dispatch_reviewer_slots(
    *,
    gate_label: str,
    reviewer_prompt: str,
    reviewer_resume_prompt: str,
    reviewer_results: list[ReviewerResult],
    prior_failures: dict[str, dict],
    panel_config: PanelConfig,
    feature_active: Path,
    vendor_cwd: Path,
    probe_config: ProbeConfig | None,
    log_emit: Callable[[dict], None] | None,
) -> None:
    """Dispatch unsettled slots in parallel; each slot owns its retry."""
    to_run = _missing_reviewers(panel_config.reviewers, reviewer_results)
    if not to_run:
        return
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(len(to_run), 1),
        thread_name_prefix=f"panel-{gate_label}",
    ) as pool:
        futures = [
            pool.submit(
                _invoke_reviewer_with_retry,
                spec,
                reviewer_prompt,
                panel_config.reviewer_probe_interval_sec,
                prior_failure=prior_failures.get(spec.vendor),
                cwd=vendor_cwd,
                feature_active=feature_active,
                probe_config=probe_config,
                log_emit=log_emit,
                session_key=_reviewer_session_key_for_spec(
                    feature_active=feature_active,
                    gate=gate_label,
                    reviewer_specs=panel_config.reviewers,
                    spec=spec,
                ),
                resume_prompt=reviewer_resume_prompt,
            )
            for spec in to_run
        ]
        for future in concurrent.futures.as_completed(futures):
            reviewer_results.append(future.result())


def _quota_skip_raw(result: ReviewerResult) -> str:
    confirmation = result.quota_confirmation or {}
    source = confirmation.get("source", "quota-check")
    return (
        f"{QUOTA_SKIPPED_PREFIX} after {result.attempt_count} dispatch "
        f"attempt(s); source={source}]"
    )


def _per_vendor_raw(reviewer_results: list[ReviewerResult]) -> dict[str, str]:
    raw = {r.vendor: r.output for r in reviewer_results if r.ok}
    for result in reviewer_results:
        if result.quota_skipped:
            raw[result.vendor] = _quota_skip_raw(result)
        elif not result.ok:
            raw[result.vendor] = (
                f"{NO_RESPONSE_PREFIX} — {result.failure_detail}]"
            )
    return raw


def _quota_halt_from_skips(
    gate_label: str, skipped: list[ReviewerResult],
) -> QuotaHalt:
    diagnostics: list[dict] = []
    reset_times: list[datetime] = []
    for result in skipped:
        confirmation = result.quota_confirmation or {}
        nested = confirmation.get("diagnostics")
        if isinstance(nested, list):
            diagnostics.extend(d for d in nested if isinstance(d, dict))
        else:
            diagnostics.append({
                "vendor": result.vendor,
                "model": result.model,
                "min_quota_pct": confirmation.get("min_quota_pct", 0.0),
                "remaining_pct": confirmation.get("remaining_pct"),
                "resets_at": confirmation.get("resets_at"),
                "error": confirmation.get("error"),
            })
        for value in (
            confirmation.get("resume_at"), confirmation.get("resets_at"),
        ):
            if not isinstance(value, str) or not value:
                continue
            try:
                reset_times.append(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                pass
    return QuotaHalt(
        role=f"panel-reviewers:{gate_label}",
        diagnostics=diagnostics,
        resume_at=min(reset_times) if reset_times else None,
    )


def _require_panel_quorum(
    *,
    gate_label: str,
    reviewer_results: list[ReviewerResult],
    panel_config: PanelConfig,
    log_emit: Callable[[dict], None] | None,
) -> None:
    responded = [
        r for r in reviewer_results if r.ok and r.output.strip()
    ]
    skipped = [r for r in reviewer_results if r.quota_skipped]
    missing = _missing_reviewers(panel_config.reviewers, reviewer_results)
    if missing:
        if log_emit is not None:
            log_emit({
                "event": "panel-incomplete",
                "stage": "gate",
                "gate": gate_label,
                "responded": len(responded),
                "quorum": panel_config.min_responding_reviewers,
                "missing_reviewers": [spec.vendor for spec in missing],
            })
        raise GatePending(
            f"panel-{gate_label}",
            _panel_incomplete_message(
                gate_label=gate_label,
                reviewer_specs=panel_config.reviewers,
                reviewer_results=reviewer_results,
                min_responding_reviewers=panel_config.min_responding_reviewers,
            ),
        )
    if len(responded) < panel_config.min_responding_reviewers:
        # Every absent slot is positively quota-confirmed, but the transport
        # quorum is still not met. Preserve quota-pause semantics so the
        # orchestrator can resume after a reset instead of asking for content
        # changes or treating this as a manual panel failure.
        raise _quota_halt_from_skips(gate_label, skipped)
    if skipped and log_emit is not None:
        log_emit({
            "event": "panel-quorum",
            "stage": "gate",
            "gate": gate_label,
            "responded": len(responded),
            "quorum": panel_config.min_responding_reviewers,
            "quota_skipped_reviewers": [r.vendor for r in skipped],
        })


def _compose_synthesizer_prompt(
    *, gate: str, artifact_path: Path, reviewer_results: list[ReviewerResult],
    feature_active: Path | None = None,
) -> str:
    """Build the prompt for the synthesizer given reviewer outputs."""
    base = synthesize_prompt_path().read_text(encoding="utf-8")
    responded = [r.vendor for r in reviewer_results if r.ok]
    quota_skipped = [r.vendor for r in reviewer_results if r.quota_skipped]
    missing = [
        r.vendor for r in reviewer_results if not r.ok and not r.quota_skipped
    ]

    out = base + "\n\n---\n\n"
    out += f"## Context for this synthesis\n\n"
    out += f"- **Gate**: `{gate}`\n"
    out += f"- **Artifact path**: `{artifact_path}`\n"
    if artifact_path.exists():
        out += f"- **Artifact hash**: `{hash_file(artifact_path)}`\n"
    out += f"- **Reviewers who responded**: {', '.join(responded) if responded else '(none)'}\n"
    if missing:
        out += f"- **Reviewers who did NOT respond**: {', '.join(missing)}\n"
    if quota_skipped:
        out += (
            "- **Reviewers omitted after retry + confirmed quota exhaustion**: "
            f"{', '.join(quota_skipped)}\n"
        )
    catalog: list[dict] = []
    if feature_active is not None:
        catalog = prior_cluster_catalog(feature_active, gate)
    out += "\n## Prior semantic issue clusters\n\n"
    if catalog:
        out += (
            "Reuse a `cluster_id` below only when a current finding is the "
            "same underlying issue. Otherwise use null.\n\n```json\n"
            + json.dumps(catalog, sort_keys=True, indent=2)
            + "\n```\n"
        )
    else:
        out += "No prior clusters exist for this gate; use null for every prior_cluster_id.\n"
    out += (
        "\nThe synthesizer extracts and clusters; it is not a reviewer. Do not "
        "re-review the artifact and do not add or drop findings.\n"
    )
    for r in reviewer_results:
        if r.ok:
            out += f"### Reviewer: {r.vendor} ({r.model})\n\n{r.output}\n\n"
        elif not r.quota_skipped:
            out += f"### Reviewer: {r.vendor} — NO RESPONSE ({r.failure_detail})\n\n"
    return out


def _read_only_native_args(vendor: str) -> tuple[str, ...]:
    """Read-only sandbox hints for the SYNTHESIZER.

    The synthesizer only reads the reviewer outputs handed to it in its
    prompt and emits a verdict — it needs no repo access, network, or write.
    So it stays sandboxed read-only (unlike reviewers, which run yolo to read
    cross-repo material and reach the network). Shared vendors only maps
    model/effort/yolo natively; per-vendor read-only is passed through here.
    """
    raw = vendor.strip().lower()
    if raw in {"openai", "codex", "gpt"}:
        return ("--sandbox", "read-only")
    if raw in {"claude", "anthropic"}:
        return ("--allowedTools", "Read,Glob,Grep,LS")
    if raw in {"agy", "antigravity"}:
        return ("--mode", "plan")
    return ()


def _invoke_synthesizer(
    spec: PanelSynthesizerSpec, prompt: str, probe_interval_sec: int,
    *, cwd: Path | None = None,
    debug_dir: Path | None = None,
    feature_active: Path | None = None,
    probe_config: ProbeConfig | None = None,
    log_emit: Callable[[dict], None] | None = None,
) -> tuple[bool, dict | None, str]:
    """Run the synthesizer CLI with native JSON-schema output.

    Returns (ok, parsed_dict, failure_detail). parsed_dict is the
    schema-validated synthesizer output, or None on failure.

    `probe_interval_sec` semantics match `_invoke_reviewer`: probe
    consultation threshold when `probe_config` is supplied, otherwise
    a wall-clock cap.
    """
    fake = os.environ.get(FAKE_INVOKER_ENV)
    schema_json = synthesizer_output_schema_json()
    timeout_sec = probe_interval_sec  # for fake-mode subprocess + error messages
    # Quota gate (fail-closed; QuotaHalt propagates). Config validation guarantees
    # synthesizer fallbacks are schema-capable, so the schema-vendor guard below
    # still holds after rebinding. Skipped in fake/test mode.
    if not fake:
        _cand = resolve_candidate(
            build_candidates(spec),
            role="synthesizer",
            logger=(lambda m: log_emit({"event": "quota", "role": "synthesizer", "msg": m}))
            if log_emit
            else None,
        )
        spec = PanelSynthesizerSpec(
            vendor=_cand.vendor, model=_cand.model, effort=_cand.effort
        )
    try:
        if fake:
            env = os.environ.copy()
            env["AUTODEV_PANEL_FAKE_ROLE"] = "synthesizer"
            env["AUTODEV_PANEL_FAKE_VENDOR"] = spec.vendor
            env["AUTODEV_PANEL_FAKE_MODEL"] = spec.model
            env["AUTODEV_PANEL_FAKE_SCHEMA"] = schema_json
            proc = subprocess.run(
                [fake], input=prompt, capture_output=True, text=True,
                env=env, timeout=timeout_sec,
            )
            if proc.returncode != 0:
                return False, None, f"fake synth exit {proc.returncode}: {proc.stderr[-500:]}"
            try:
                raw = json.loads(proc.stdout.strip())
            except json.JSONDecodeError as e:
                return False, None, f"fake synth non-JSON: {e}"
            # Allow fake to emit either envelope-form (with structured_output)
            # or the bare schema payload.
            parsed = raw.get("structured_output") if isinstance(raw, dict) and "structured_output" in raw else raw
            return True, parsed, ""

        if spec.vendor in {"agy", "cursor"}:
            return False, None, (
                f"synthesizer vendor {spec.vendor!r} not supported: "
                f"{spec.vendor} CLI has no native JSON-schema enforcement"
            )
        idle_callback = (
            _build_idle_callback(
                stage=f"panel-synthesizer-{spec.vendor}",
                stage_probe_interval_sec=probe_interval_sec,
                stdout_path=Path("/dev/null"),
                stderr_path=Path("/dev/null"),
                probe_config=probe_config,
                log_emit=log_emit,
                process_registry=(
                    registry_path(feature_active)
                    if feature_active is not None else None
                ),
            )
            if probe_config is not None
            else None
        )
        hard_backstop = (
            _hard_backstop_sec(probe_interval_sec)
            if probe_config is not None
            else probe_interval_sec
        )
        result = call_shared_vendor(
            vendor=spec.vendor,
            model=spec.model,
            prompt=prompt,
            output_id="synthesizer",
            timeout_sec=hard_backstop,
            cwd=cwd,
            effort=spec.effort,
            schema_json=schema_json,
            native_args=_read_only_native_args(spec.vendor),
            idle_callback=idle_callback,
            process_registry=(
                registry_path(feature_active)
                if feature_active is not None else None
            ),
            process_label=f"panel-synthesizer:{spec.vendor}",
        )
        if result.returncode != 0:
            if result.timed_out:
                return False, None, f"timeout after {timeout_sec}s"
            return False, None, (
                f"exit {result.returncode}: "
                f"{(result.log or result.summary_stderr)[-500:]}"
            )
        try:
            envelope = json.loads(result.output.strip())
        except json.JSONDecodeError as e:
            # Persist all available diagnostics for debugging — by the
            # time the caller writes the failure verdict, the tempfile
            # dir is gone, so we'd have nothing to inspect post-halt.
            if debug_dir is not None:
                try:
                    (debug_dir / "panel-synthesizer-raw.txt").write_text(
                        f"--- synthesizer raw output ({len(result.output)} chars) ---\n"
                        f"{result.output}\n"
                        f"--- end raw ---\n\n"
                        f"--- shared-call returncode: {result.returncode} ---\n"
                        f"--- timed_out: {result.timed_out} ---\n\n"
                        f"--- shared-call status ({len(repr(result.status))} chars) ---\n"
                        f"{result.status!r}\n"
                        f"--- end status ---\n\n"
                        f"--- shared-call log ({len(result.log)} chars) ---\n"
                        f"{result.log}\n"
                        f"--- end log ---\n\n"
                        f"--- summary_stderr ({len(result.summary_stderr)} chars) ---\n"
                        f"{result.summary_stderr}\n"
                        f"--- end stderr ---\n",
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            return False, None, f"envelope non-JSON: {e}"
        # shared/vendors envelope: {"structured_output": {...}}.
        # Claude returns it natively; for codex, vendor-launch.sh wraps the
        # --output-schema JSON into the same shape so callers see one contract.
        if not isinstance(envelope, dict):
            return False, None, "envelope not object"
        parsed = envelope.get("structured_output")
        if parsed is None:
            return False, None, "envelope missing structured_output"
        return True, parsed, ""
    except subprocess.TimeoutExpired:
        return False, None, f"timeout after {timeout_sec}s"
    except FileNotFoundError as e:
        return False, None, str(e)


def _derive_overall_verdict(per_vendor_verdicts: list[str]) -> str:
    """Harness-side, deterministic: worst-verdict-wins across reviewers.

    The synthesizer extracts per-reviewer verdicts verbatim; the harness
    derives the overall verdict from them. No LLM judgment involved here.
    """
    if not per_vendor_verdicts:
        return "fail"
    if any(v == "fail" for v in per_vendor_verdicts):
        return "fail"
    if any(v == "needs_revision" for v in per_vendor_verdicts):
        return "needs_revision"
    if all(v == "pass" for v in per_vendor_verdicts):
        return "pass"
    return "needs_revision"


def _decision_outcome_to_legacy_verdict(outcome: str) -> str:
    if outcome == "pass":
        return "pass"
    if outcome == "retry_design":
        return "needs_revision"
    if outcome == "halt_for_human":
        return "fail"
    raise SchemaError(f"unsupported design-review decision outcome {outcome!r}")


def _validate_design_review_targets(findings: list[PanelFinding]) -> None:
    for finding in findings:
        for target in finding.targets:
            if target == "anchor.architecture-proposal.md":
                raise SchemaError(
                    "design-review target label 'anchor.architecture-proposal.md' is not supported"
                )


def _normalize_design_review_decision(
    *,
    feature_active: Path,
    decision_payload: dict,
    findings: list[PanelFinding],
) -> ReviewDecision:
    node = decision_payload.get("node")
    outcome = decision_payload.get("outcome")
    blocking = decision_payload.get("blocking")
    severity = decision_payload.get("severity")
    summary = decision_payload.get("summary")
    prd_targeted = any(
        target in {"anchor.prd.md", "primary_pair.prd.md"}
        for finding in findings
        for target in finding.targets
    )
    decision = ReviewDecision(
        node=node,
        outcome=outcome,
        blocking=blocking,
        severity=severity,
        summary=summary,
        prd_targeted=prd_targeted,
    )
    # Reuse verdict-layer validation so runner and loader agree.
    decision_dict = decision.to_dict()
    from autodev.artifacts.verdict import _validate_decision  # local import avoids widening public API

    _validate_decision(decision_dict, "design-review")
    _validate_design_review_targets(findings)

    if decision.outcome == "halt_for_human" and prd_targeted:
        streak = load_state(feature_active).prd_target_streak.get("design-review", 0)
        if streak == 0:
            return ReviewDecision(
                node="design_review",
                outcome="retry_design",
                blocking=False,
                severity=decision.severity,
                summary=decision.summary,
                prd_targeted=True,
            )
    return decision


def _mechanical_fallback(
    reviewer_results: list[ReviewerResult],
) -> tuple[str, list[PanelFinding], str]:
    """Conservative union rule when synthesizer fails.

    Scans each reviewer's markdown for obvious verdict / severity
    keywords. Intentionally crude — the whole point of G15 is to not
    rely on this path, but we still need *some* result if the
    synthesizer is down.
    """
    import re

    responded = [r for r in reviewer_results if r.ok]
    if not responded:
        return "fail", [PanelFinding(
            severity="invariant_violation", vendor="harness",
            summary="no panel reviewer produced output",
        )], "no reviewers responded"

    per_vendor_verdict: dict[str, str] = {}
    findings: list[PanelFinding] = []
    for r in responded:
        low = r.output.lower()
        # Verdict keyword
        m = re.search(
            r"verdict[^a-z0-9]{0,100}?\b(pass|fail|proceed|reject|revise|needs[-_]revision)\b",
            low, re.DOTALL,
        )
        if m:
            kw = m.group(1)
            mapped = {"pass": "pass", "proceed": "pass",
                      "fail": "fail", "reject": "fail",
                      "revise": "needs_revision",
                      "needs_revision": "needs_revision",
                      "needs-revision": "needs_revision"}.get(kw, "needs_revision")
            per_vendor_verdict[r.vendor] = mapped
        else:
            per_vendor_verdict[r.vendor] = "needs_revision"
        # Severity keyword scan (invariant_violation only — others would
        # be too noisy without structure).
        for vm in re.finditer(
            r"invariant[_\- ]violation[:\s\-\u2014]+([^\n]{10,300})",
            r.output, re.IGNORECASE,
        ):
            findings.append(PanelFinding(
                severity="invariant_violation", vendor=r.vendor,
                summary=vm.group(1).strip()[:300],
            ))

    if any(v == "fail" for v in per_vendor_verdict.values()) or any(
        f.severity == "invariant_violation" for f in findings
    ):
        overall = "fail"
    elif all(v == "pass" for v in per_vendor_verdict.values()):
        overall = "pass"
    else:
        overall = "needs_revision"

    findings.append(PanelFinding(
        severity="opinion", vendor="harness",
        summary="synthesizer_failed — mechanical fallback used",
    ))
    return overall, findings, "mechanical fallback"


def _filter_consulted_docs(
    consulted_docs: list[dict], filter_set: set[str] | None,
) -> list[dict]:
    """Restrict ``consulted_docs`` to entries whose basename is in
    ``filter_set``. ``None`` passes through unchanged."""
    if filter_set is None:
        return list(consulted_docs)
    return [
        d for d in consulted_docs
        if isinstance(d.get("path"), str)
        and Path(d["path"]).name in filter_set
    ]


def _synthesize_and_build_verdict(
    *,
    gate_label: str,
    reviewer_results: list[ReviewerResult],
    primary_artifact: Path,
    prompt_file_for_audit: Path,
    consulted_docs: list[dict],
    per_vendor_raw: dict[str, str],
    panel_config: PanelConfig,
    feature_active: Path,
    vendor_cwd: Path,
    probe_config: ProbeConfig | None,
    log_emit: Callable[[dict], None] | None,
) -> tuple[PanelVerdict, Path, str | None]:
    """Run the synthesizer for a single reviewer group and build the
    PanelVerdict. Returns (verdict, output_path, synth_infra_error_or_None).

    The caller writes the verdict, decides whether to raise PreflightError
    based on ``synth_infra_error``, and is responsible for orchestrating
    multiple group calls.
    """
    cfg = panel_config

    synth_prompt = _compose_synthesizer_prompt(
        gate=gate_label, artifact_path=primary_artifact,
        reviewer_results=reviewer_results,
        feature_active=feature_active,
    )

    synth_ok, synth_parsed, synth_detail = _invoke_synthesizer(
        cfg.synthesizer, synth_prompt, cfg.synthesizer_probe_interval_sec,
        cwd=vendor_cwd,
        debug_dir=feature_active,
        feature_active=feature_active,
        probe_config=probe_config,
        log_emit=log_emit,
    )

    findings: list[PanelFinding] = []
    issue_clusters: list[IssueCluster] = []
    coverage_map: dict[str, list[dict]] = {}
    decision: ReviewDecision | None = None
    synth_infra_error: str | None = None
    if synth_ok and synth_parsed:
        try:
            per_reviewer = synth_parsed.get("per_reviewer", [])
            expected_reviewers = Counter(
                r.vendor for r in reviewer_results if r.ok
            )
            actual_reviewers = Counter(
                entry.get("vendor") for entry in per_reviewer
                if isinstance(entry, dict)
            )
            if actual_reviewers != expected_reviewers:
                raise SchemaError(
                    "synthesizer per_reviewer must exactly match responding "
                    f"reviewers: expected {dict(expected_reviewers)!r}, "
                    f"got {dict(actual_reviewers)!r}"
                )
            per_vendor_verdicts: list[str] = []
            for entry in per_reviewer:
                vendor = entry.get("vendor", "unknown")
                per_vendor_verdicts.append(entry.get("verdict", "needs_revision"))
                if entry.get("coverage"):
                    coverage_map[vendor] = list(entry.get("coverage", []))
                for f in entry.get("findings", []):
                    findings.append(_finding_from_synth(vendor, f))
            issue_clusters = _build_issue_clusters(
                findings, synth_parsed, feature_active=feature_active,
                gate=gate_label,
            )
            verdict_str = _derive_overall_verdict(per_vendor_verdicts)
        except (SchemaError, TypeError, ValueError) as exc:
            synth_ok = False
            synth_detail = f"invalid synthesized issue structure: {exc}"
            findings = []
            issue_clusters = []
            coverage_map = {}
            decision = None
    if (synth_ok and synth_parsed and gate_label == "design-review"
            and isinstance(synth_parsed.get("decision"), dict)):
        decision = _normalize_design_review_decision(
            feature_active=feature_active,
            decision_payload=synth_parsed["decision"],
            findings=findings,
        )
        verdict_str = _decision_outcome_to_legacy_verdict(decision.outcome)
    if not synth_ok or not synth_parsed:
        findings = [
            PanelFinding(
                severity="invariant_violation",
                priority="P0",
                finding_id="harness:synthesizer-failed",
                vendor="harness",
                summary=(
                    f"synthesizer_failed: {synth_detail[:300]}. The "
                    f"harness cannot determine a verdict because the "
                    f"synthesizer's output was not parseable JSON. "
                    f"Re-running the producer will not help — fix the "
                    f"synthesizer prompt, the synthesizer model "
                    f"choice, or `autodev escalate` for human review."
                ),
                targets=[],
            ),
        ]
        verdict_str = "fail"
        synth_infra_error = synth_detail or "synthesizer call failed"

    # v3-core R3 — anchor-filter post-processing. Findings whose targets
    # are all-anchor move to dropped_findings[]; unknown/malformed target
    # strings default to primary_pair and are logged as warnings.
    feature_log = JsonlLog(feature_active / "log.jsonl")
    warnings: list[str] = []
    kept_findings, dropped = filter_anchor_findings(
        findings, warn=warnings.append,
    )
    issue_clusters = _trim_issue_clusters(issue_clusters, kept_findings)
    if decision is not None:
        _validate_design_review_targets(kept_findings + [  # keep invariant after audit-only filtering
            PanelFinding(
                severity=df.severity,
                vendor=df.vendor,
                summary=df.summary,
                targets=df.targets,
            )
            for df in dropped
        ])
    for w in warnings:
        feature_log.emit(stage="gate", event="anchor-filter-warning",
                         feature=feature_active.parent.name,
                         detail={"gate": gate_label, "message": w})

    # Rigor severity filter (docs/proposals/rigor-tier.md) — rewrites
    # each finding's `severity` slot to its effective value per the
    # PRD's `## Assurance` map; the reviewer's original is preserved in
    # `severity_reported`. No-op when the PRD has no Assurance section
    # (all-strict default == pre-rigor behavior).
    decision_overridden_by_rigor: dict | None = None
    decision_overridden_by_policy: dict | None = None
    rigor_result, release_threshold = _apply_rigor_to_findings(
        kept_findings, feature_active=feature_active,
    )
    for e in rigor_result.events:
        feature_log.emit(
            stage="gate", event=f"rigor-filter-{e.kind}",
            feature=feature_active.parent.name,
            detail={
                "gate": gate_label, "reason": e.reason, "vendor": e.vendor,
                "severity_reported": e.severity_reported, "rigor": e.rigor,
                "summary": e.summary,
            },
        )

    if synth_ok and synth_parsed and decision is None:
        verdict_str = (
            "needs_revision" if has_effective_blocking(
                kept_findings, release_threshold,
            )
            else "pass"
        )
    elif decision is not None:
        blocks = has_effective_blocking(kept_findings, release_threshold)
        # A canonical decision cannot bypass explicit release policy. The
        # original decision remains in the audit fields. Under the legacy P1
        # threshold, halt_for_human retains its historical fail-closed rule.
        policy_override = (
            not blocks and decision.outcome != "pass"
            and release_threshold != "P1"
        )
        rigor_override = (
            not blocks and decision.outcome == "retry_design"
            and rigor_result.downgraded > 0
        )
        finding_override = blocks and decision.outcome == "pass"
        if finding_override:
            original = decision.to_dict()
            decision_overridden_by_policy = {
                **original,
                "release_threshold": release_threshold,
                "reason": "blocking findings override pass decision",
            }
            decision = ReviewDecision(
                node=decision.node, outcome="retry_design", blocking=False,
                severity=decision.severity, summary=decision.summary,
                prd_targeted=decision.prd_targeted,
            )
            verdict_str = _decision_outcome_to_legacy_verdict("retry_design")
            feature_log.emit(
                stage="gate", event="release-policy-decision-override",
                feature=feature_active.parent.name,
                detail={
                    "gate": gate_label, "from_outcome": "pass",
                    "to_outcome": "retry_design",
                    "release_threshold": release_threshold,
                },
            )
        elif policy_override or rigor_override:
            original = decision.to_dict()
            if policy_override:
                decision_overridden_by_policy = {
                    **original, "release_threshold": release_threshold,
                }
            if rigor_override:
                decision_overridden_by_rigor = original
            decision = ReviewDecision(
                node=decision.node, outcome="pass", blocking=False,
                severity=decision.severity, summary=decision.summary,
                prd_targeted=decision.prd_targeted,
            )
            verdict_str = _decision_outcome_to_legacy_verdict("pass")
            feature_log.emit(
                stage="gate", event="release-policy-decision-override",
                feature=feature_active.parent.name,
                detail={
                    "gate": gate_label, "from_outcome": original["outcome"],
                    "to_outcome": "pass", "downgraded": rigor_result.downgraded,
                    "release_threshold": release_threshold,
                },
            )

    v = PanelVerdict(
        gate=gate_label, verdict=verdict_str,  # type: ignore[arg-type]
        findings=kept_findings,
        source=str(primary_artifact),
        source_hash=hash_file(primary_artifact),
        prompt_file=str(prompt_file_for_audit),
        prompt_hash=hash_file(prompt_file_for_audit),
        harness_version=HARNESS_VERSION,
        run_ts=datetime.now(timezone.utc).isoformat(),
        consulted_docs=consulted_docs,
        per_vendor_raw=per_vendor_raw,
        dropped_findings=dropped,
        coverage_map=coverage_map,
        decision=decision,
        decision_overridden_by_rigor=decision_overridden_by_rigor,
        issue_clusters=issue_clusters,
        release_threshold=release_threshold,  # type: ignore[arg-type]
        decision_overridden_by_policy=decision_overridden_by_policy,
    )
    out_path = feature_active / f"panel-{gate_label}.json"
    return v, out_path, synth_infra_error


def _run_one_group_pipeline(
    *,
    group_spec: dict,
    primary_artifact: Path,
    prompt_file_for_audit: Path,
    panel_config: PanelConfig,
    feature_active: Path,
    vendor_cwd: Path,
    probe_config: ProbeConfig | None,
    log_emit: Callable[[dict], None] | None,
) -> tuple[PanelVerdict, Path, str | None]:
    """Run one reviewer group: retried reviewers, quorum, then synthesis.

    Returns (verdict, output_path, synth_infra_error_or_None). The caller
    writes the verdict (kept out of this helper so dual-group orchestration
    can decide error-handling policy across groups).
    """
    cfg = panel_config
    metadata = _reviewer_cache_metadata(
        gate_label=group_spec["name"],
        primary_artifact=primary_artifact,
        prompt_file_for_audit=group_spec["prompt_file_for_audit"],
        reviewer_prompt=group_spec["reviewer_prompt"],
        consulted_docs=group_spec["consulted_docs"],
    )
    reviewer_results, prior_failures = _load_reviewer_cache(
        feature_active=feature_active,
        gate_label=group_spec["name"],
        reviewer_specs=cfg.reviewers,
        metadata=metadata,
    )
    _dispatch_reviewer_slots(
        gate_label=group_spec["name"],
        reviewer_prompt=group_spec["reviewer_prompt"],
        reviewer_resume_prompt=group_spec["reviewer_resume_prompt"],
        reviewer_results=reviewer_results,
        prior_failures=prior_failures,
        panel_config=cfg,
        feature_active=feature_active,
        vendor_cwd=vendor_cwd,
        probe_config=probe_config,
        log_emit=log_emit,
    )

    # Sort by configured vendor order for stable audit output.
    order = {r.vendor: i for i, r in enumerate(cfg.reviewers)}
    reviewer_results.sort(key=lambda r: order.get(r.vendor, 999))

    per_vendor_raw = _per_vendor_raw(reviewer_results)

    out_path = feature_active / group_spec["verdict_file"]
    _write_reviewer_cache(
        feature_active=feature_active,
        gate_label=group_spec["name"],
        reviewer_specs=cfg.reviewers,
        metadata=metadata,
        reviewer_results=reviewer_results,
        prior_failures=prior_failures,
    )
    _require_panel_quorum(
        gate_label=group_spec["name"],
        reviewer_results=reviewer_results,
        panel_config=cfg,
        log_emit=log_emit,
    )

    # Healthy → synthesize immediately (no barrier waiting for other group).
    return _synthesize_and_build_verdict(
        gate_label=group_spec["name"],
        reviewer_results=reviewer_results,
        primary_artifact=primary_artifact,
        prompt_file_for_audit=group_spec["prompt_file_for_audit"],
        consulted_docs=group_spec["consulted_docs"],
        per_vendor_raw=per_vendor_raw,
        panel_config=panel_config,
        feature_active=feature_active,
        vendor_cwd=vendor_cwd,
        probe_config=probe_config,
        log_emit=log_emit,
    )


def _run_dual_group_design_review(
    *,
    feature_active: Path,
    primary_artifact: Path,
    prompt_file_for_audit: Path,
    consulted_docs: list[dict],
    panel_config: PanelConfig,
    repo_root: Path | None,
    probe_config: ProbeConfig | None,
    log_emit: Callable[[dict], None] | None,
) -> PanelVerdict:
    """Run the design-review gate as two reviewer groups (design-review +
    trace-review) sharing one primary_artifact (design-packet.json).

    Each group runs its own pipeline (reviewer quorum → synthesizer) concurrently
    and independently. Whichever group's reviewers finish first immediately
    starts its synthesizer; neither group waits on the other. Writes BOTH
    verdict files; returns the design-review group's PanelVerdict. The
    orchestrator merges findings from both files when routing.
    """
    vendor_cwd = repo_root or feature_active

    # Build per-group reviewer prompts and context files up-front.
    group_specs: list[dict] = []
    for group in DESIGN_REVIEW_GROUPS:
        group_docs = _filter_consulted_docs(consulted_docs, group["consulted_filter"])
        group_specs.append({
            "name": group["name"],
            "prompt_file": group["prompt_file"],
            "prompt_file_for_audit": PROMPTS_DIR / group["prompt_file"],
            "verdict_file": group["verdict_file"],
            "consulted_docs": group_docs,
            "reviewer_prompt": _compose_reviewer_prompt(
                gate=group["name"],
                artifact_path=primary_artifact,
                consulted_docs=group_docs,
                feature_active=feature_active,
                repo_root=repo_root,
            ),
            "reviewer_resume_prompt": _compose_reviewer_prompt(
                gate=group["name"],
                artifact_path=primary_artifact,
                consulted_docs=group_docs,
                feature_active=feature_active,
                repo_root=repo_root,
                continuation=True,
            ),
        })

    # Run two group pipelines concurrently; each is reviewers → synthesizer
    # in sequence WITHIN the group, but groups run independently — no barrier.
    pipeline_results: dict[str, tuple[PanelVerdict, Path, str | None]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(group_specs),
        thread_name_prefix="panel-group",
    ) as pool:
        future_to_name = {
            pool.submit(
                _run_one_group_pipeline,
                group_spec=gs,
                primary_artifact=primary_artifact,
                prompt_file_for_audit=prompt_file_for_audit,
                panel_config=panel_config,
                feature_active=feature_active,
                vendor_cwd=vendor_cwd,
                probe_config=probe_config,
                log_emit=log_emit,
            ): gs["name"]
            for gs in group_specs
        }
        for fut in concurrent.futures.as_completed(future_to_name):
            pipeline_results[future_to_name[fut]] = fut.result()

    # Write verdicts in declared order; collect any synth infra errors.
    written: dict[str, PanelVerdict] = {}
    infra_errors: list[tuple[str, str]] = []
    for gs in group_specs:
        v, out_path, err = pipeline_results[gs["name"]]
        write_verdict(out_path, v)
        written[gs["name"]] = v
        if err is not None:
            infra_errors.append((gs["name"], err))

    if infra_errors:
        from autodev.errors import PreflightError
        names = ", ".join(n for n, _ in infra_errors)
        first_err = infra_errors[0][1]
        raise PreflightError(
            f"panel design-review: synthesizer infrastructure error in "
            f"group(s) {names} — {first_err[:200]}. Verdict files written "
            f"for audit. Cannot determine content verdict; halting run. "
            f"Fix the synthesizer prompt/model or `autodev escalate`."
        )

    return written["design-review"]


def run_panel_gate_internal(
    *,
    gate: str,
    feature_active: Path,
    primary_artifact: Path,
    prompt_file_for_audit: Path,
    consulted_docs: list[dict],
    panel_config: PanelConfig,
    repo_root: Path | None = None,
    probe_config: ProbeConfig | None = None,
    log_emit: Callable[[dict], None] | None = None,
) -> PanelVerdict:
    """G15 main entry: dispatch reviewers, synthesize, write verdict.

    `prompt_file_for_audit` is hashed and recorded in the verdict for
    SC3 change-control. For G15 this is the gate-specific review prompt.
    """
    cfg = panel_config

    # v3-core R5 — mechanical pre-check. Halt before any vendor dispatch.
    # Test override: AUTODEV_PANEL_SKIP_PRECHECK=1 for tests that stub
    # run_panel_gate_internal with synthetic artifacts.
    skip_precheck = os.environ.get("AUTODEV_PANEL_SKIP_PRECHECK") == "1"
    pre = None if skip_precheck else run_precheck(gate, feature_active, consulted_docs)
    if pre is not None and not pre.ok:
        finding = PanelFinding(
            severity="invariant_violation", vendor="harness",
            summary=pre.message,
        )
        v = PanelVerdict(
            gate=gate, verdict="needs_revision",  # type: ignore[arg-type]
            findings=[finding],
            source=str(primary_artifact),
            source_hash=hash_file(primary_artifact),
            prompt_file=str(prompt_file_for_audit),
            prompt_hash=hash_file(prompt_file_for_audit),
            harness_version=HARNESS_VERSION,
            run_ts=datetime.now(timezone.utc).isoformat(),
            consulted_docs=consulted_docs,
        )
        out_path = feature_active / f"panel-{gate}.json"
        write_verdict(out_path, v)
        return v

    # design-review runs TWO reviewer groups (design-review + trace-review),
    # writing two verdict files. close-approval uses the single-group path.
    if gate == "design-review":
        return _run_dual_group_design_review(
            feature_active=feature_active,
            primary_artifact=primary_artifact,
            prompt_file_for_audit=prompt_file_for_audit,
            consulted_docs=consulted_docs,
            panel_config=panel_config,
            repo_root=repo_root,
            probe_config=probe_config,
            log_emit=log_emit,
        )

    reviewer_prompt = _compose_reviewer_prompt(
        gate=gate, artifact_path=primary_artifact, consulted_docs=consulted_docs,
        feature_active=feature_active, repo_root=repo_root,
    )
    reviewer_resume_prompt = _compose_reviewer_prompt(
        gate=gate, artifact_path=primary_artifact, consulted_docs=consulted_docs,
        feature_active=feature_active, repo_root=repo_root,
        continuation=True,
    )
    vendor_cwd = repo_root or feature_active
    metadata = _reviewer_cache_metadata(
        gate_label=gate,
        primary_artifact=primary_artifact,
        prompt_file_for_audit=prompt_file_for_audit,
        reviewer_prompt=reviewer_prompt,
        consulted_docs=consulted_docs,
    )
    reviewer_results, prior_failures = _load_reviewer_cache(
        feature_active=feature_active,
        gate_label=gate,
        reviewer_specs=cfg.reviewers,
        metadata=metadata,
    )
    _dispatch_reviewer_slots(
        gate_label=gate,
        reviewer_prompt=reviewer_prompt,
        reviewer_resume_prompt=reviewer_resume_prompt,
        reviewer_results=reviewer_results,
        prior_failures=prior_failures,
        panel_config=cfg,
        feature_active=feature_active,
        vendor_cwd=vendor_cwd,
        probe_config=probe_config,
        log_emit=log_emit,
    )
    # Preserve reviewer order as declared in config (stable audit).
    order = {r.vendor: i for i, r in enumerate(cfg.reviewers)}
    reviewer_results.sort(key=lambda r: order.get(r.vendor, 999))

    per_vendor_raw = _per_vendor_raw(reviewer_results)

    _write_reviewer_cache(
        feature_active=feature_active,
        gate_label=gate,
        reviewer_specs=cfg.reviewers,
        metadata=metadata,
        reviewer_results=reviewer_results,
        prior_failures=prior_failures,
    )
    _require_panel_quorum(
        gate_label=gate,
        reviewer_results=reviewer_results,
        panel_config=cfg,
        log_emit=log_emit,
    )

    v, out_path, synth_infra_error = _synthesize_and_build_verdict(
        gate_label=gate,
        reviewer_results=reviewer_results,
        primary_artifact=primary_artifact,
        prompt_file_for_audit=prompt_file_for_audit,
        consulted_docs=consulted_docs,
        per_vendor_raw=per_vendor_raw,
        panel_config=panel_config,
        feature_active=feature_active,
        vendor_cwd=vendor_cwd,
        probe_config=probe_config,
        log_emit=log_emit,
    )
    write_verdict(out_path, v)
    if synth_infra_error is not None:
        # Synthesizer infrastructure error: verdict written for audit,
        # but the run cannot proceed. Raise so the orchestrator halts
        # instead of letting revision_loop's indeterminate-target
        # fallback dispatch a fresh producer rerun.
        from autodev.errors import PreflightError
        raise PreflightError(
            f"panel {gate}: synthesizer infrastructure error — "
            f"{synth_infra_error[:200]}. Verdict written to {out_path}. "
            f"Cannot determine content verdict; halting run. Fix the "
            f"synthesizer prompt/model or `autodev escalate`."
        )
    return v
