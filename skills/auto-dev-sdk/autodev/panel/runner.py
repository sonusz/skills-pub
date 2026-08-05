"""G15 panel runner — internal panel-review implementation.

Flow:
  1. Compose per-reviewer prompt (gate-specific review-<gate>.md +
     file manifest with paths + hashes for the artifact and consulted docs).
  2. Dispatch the configured reviewers (claude / grok / codex/openai, per
     vendors.yml panel config) in parallel.
  3. Invoke the configured synthesizer with the three reviewer
     outputs under a pinned synthesize.md prompt and the pinned JSON
     schema. Parse structured output.
  4. Build PanelVerdict (with per_vendor_raw audit field) and write
     panel-verdict.json atomically.

Failure modes and their handling:
  - Reviewer timeout / empty output → cache successful reviewer outputs,
    halt with GatePending, and retry only missing reviewers on restart.
  - Synthesizer timeout / non-JSON / schema-invalid output → write a
    harness-authored audit verdict and halt instead of routing a content
    revision.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from autodev import __version__ as HARNESS_VERSION
from autodev.artifacts.revision_state import load_state
from autodev.artifacts.verdict import (
    NO_RESPONSE_PREFIX,
    PanelFinding,
    PanelVerdict,
    ReviewDecision,
    load_verdict,
    panel_verdict_transport_incomplete,
    write_verdict,
)
from autodev.errors import ConfigError, GatePending, QuotaHalt, SchemaError
from autodev.panel.anchor_filter import filter_anchor_findings
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
        "consulted_filter": {"design.md", "scope.json", "prd.md"},
    },
    {
        "name": "trace-review",
        "prompt_file": "review-trace-review.md",
        "verdict_file": "panel-trace-review.json",
        # Restrict to behavioral artifacts only
        "consulted_filter": {"trace.md", "test-plan.md", "prd.md"},
    },
)

# The panel gate is only valid when every configured reviewer has produced a
# review. Missing reviewers are panel transport failures, not content
# findings, so the harness caches successful reviewer outputs and halts until
# the operator restarts the gate. The restart dispatches only the missing
# reviewers.
REVIEWER_CACHE_SCHEMA_VERSION = 1

DOCTOR_SCRIPT = SHARED_VENDORS_DIR / "scripts" / "doctor.sh"

# Override: path to a fake invoker script used by tests. When set, both
# reviewer and synthesizer calls route through the fake instead of the
# real shared-vendors call. The fake receives (vendor, role, prompt) via env vars
# + stdin and writes markdown or JSON to stdout.
FAKE_INVOKER_ENV = "AUTODEV_PANEL_FAKE_INVOKER"


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
    """Load reusable successful reviewer outputs for this exact panel input.

    Returns (cached_results, failure_audit). Failure audit is only for
    preserving diagnostics in the next cache write; failed reviewers are never
    reused.
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
                "run_ts": now,
            }
        else:
            failures[result.vendor] = {
                "vendor": result.vendor,
                "model": spec.model,
                "failure_detail": result.failure_detail,
                "elapsed_sec": result.elapsed_sec,
                "run_ts": now,
            }
    for vendor, failure in prior_failures.items():
        if vendor not in failures and vendor not in reviewers:
            failures[vendor] = failure
    atomic_write_json(
        _reviewer_cache_path(feature_active, gate_label),
        {**metadata, "reviewers": reviewers, "failures": failures},
    )


def _missing_reviewers(
    reviewer_specs: tuple[PanelReviewerSpec, ...],
    reviewer_results: list[ReviewerResult],
) -> list[PanelReviewerSpec]:
    ok_vendors = {r.vendor for r in reviewer_results if r.ok and r.output.strip()}
    return [spec for spec in reviewer_specs if spec.vendor not in ok_vendors]


def _panel_incomplete_message(
    *,
    gate_label: str,
    reviewer_specs: tuple[PanelReviewerSpec, ...],
    reviewer_results: list[ReviewerResult],
) -> str:
    ok_vendors = [r.vendor for r in reviewer_results if r.ok and r.output.strip()]
    missing = _missing_reviewers(reviewer_specs, reviewer_results)
    details = {
        r.vendor: r.failure_detail
        for r in reviewer_results
        if not r.ok and r.failure_detail
    }
    return (
        f"panel {gate_label} incomplete: {len(ok_vendors)} of "
        f"{len(reviewer_specs)} reviewers responded; missing "
        f"{[m.vendor for m in missing]!r}. Successful reviewers are cached; "
        "restart the run to retry only the missing reviewer(s). "
        f"Debug: {_doctor_hint()}. Details: {details!r}"
    )


def _file_ref_line(*, label: str, path: Path, hash_value: str | None = None) -> str:
    exists = path.exists()
    size = path.stat().st_size if exists else 0
    h = hash_value or _hash_or_missing(path)
    return f"- {label}: `{path}` hash=`{h}` size_bytes={size}"


def _compose_reviewer_prompt(
    *, gate: str, artifact_path: Path, consulted_docs: list[dict],
    feature_active: Path | None = None, repo_root: Path | None = None,
) -> str:
    """Build the full prompt sent to each reviewer.

    Large review inputs are passed by file reference, not inlined. The
    verdict records the same paths + hashes, so the harness can invalidate
    stale panel results without relying on prompt-sized document snapshots.
    """
    prompt_text = review_prompt_path(gate).read_text(encoding="utf-8")
    prompt_text += "\n\n---\n\n## Orchestrator context\n\n"
    if feature_active is not None:
        prompt_text += f"- FEATURE_ACTIVE: `{feature_active}`\n"
    if repo_root is not None:
        prompt_text += f"- REPO_ROOT: `{repo_root}`\n"
    prompt_text += f"- GATE: `{gate}`\n"
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
            )
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


def _compose_synthesizer_prompt(
    *, gate: str, artifact_path: Path, reviewer_results: list[ReviewerResult],
    feature_active: Path | None = None,
) -> str:
    """Build the prompt for the synthesizer given reviewer outputs."""
    del feature_active  # retained for call-site compatibility
    base = synthesize_prompt_path().read_text(encoding="utf-8")
    responded = [r.vendor for r in reviewer_results if r.ok]
    missing = [r.vendor for r in reviewer_results if not r.ok]

    out = base + "\n\n---\n\n"
    out += f"## Context for this synthesis\n\n"
    out += f"- **Gate**: `{gate}`\n"
    out += f"- **Artifact path**: `{artifact_path}`\n"
    if artifact_path.exists():
        out += f"- **Artifact hash**: `{hash_file(artifact_path)}`\n"
    out += f"- **Reviewers who responded**: {', '.join(responded) if responded else '(none)'}\n"
    if missing:
        out += f"- **Reviewers who did NOT respond**: {', '.join(missing)}\n"
    out += (
        "\nThe synthesizer is an extractor, not a reviewer. Do not re-review "
        "the artifact and do not add findings absent from reviewer outputs.\n"
    )
    for r in reviewer_results:
        if r.ok:
            out += f"### Reviewer: {r.vendor} ({r.model})\n\n{r.output}\n\n"
        else:
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
    openai_compatible = (
        not fake
        and spec.vendor.strip().lower() in {"openai", "codex", "gpt"}
    )
    schema_json = synthesizer_output_schema_json(
        openai_compatible=openai_compatible,
    )
    if openai_compatible:
        prompt += (
            "\n\n## OpenAI strict-schema field discipline\n\n"
            "Emit every field declared by the schema. Use [] when a reviewer "
            "has no findings, targets, or coverage; use an empty string when "
            "a coverage row has no notes; and always emit prd_targeted for a "
            "design decision. When the Gate in the context is not "
            "design-review, emit the top-level decision field as null.\n"
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
    coverage_map: dict[str, list[dict]] = {}
    decision: ReviewDecision | None = None
    synth_infra_error: str | None = None
    if synth_ok and synth_parsed:
        per_reviewer = synth_parsed.get("per_reviewer", [])
        per_vendor_verdicts: list[str] = []
        for entry in per_reviewer:
            vendor = entry.get("vendor", "unknown")
            per_vendor_verdicts.append(entry.get("verdict", "needs_revision"))
            if entry.get("coverage"):
                coverage_map[vendor] = list(entry.get("coverage", []))
            for f in entry.get("findings", []):
                findings.append(PanelFinding(
                    severity=f.get("severity", "opinion"),
                    vendor=vendor,
                    summary=f.get("summary", ""),
                    targets=list(f.get("targets", [])),
                ))
        if gate_label == "design-review" and isinstance(synth_parsed.get("decision"), dict):
            decision = _normalize_design_review_decision(
                feature_active=feature_active,
                decision_payload=synth_parsed["decision"],
                findings=findings,
            )
            verdict_str = _decision_outcome_to_legacy_verdict(decision.outcome)
        else:
            verdict_str = _derive_overall_verdict(per_vendor_verdicts)
    else:
        findings = [
            PanelFinding(
                severity="invariant_violation",
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
    if synth_ok and synth_parsed and decision is None:
        has_blocking = any(
            f.severity in ("invariant_violation", "risk") for f in kept_findings
        )
        verdict_str = "needs_revision" if has_blocking else "pass"

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
    """Run one reviewer group's complete pipeline: 3 reviewers in parallel,
    then synthesize once every reviewer has responded.

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
    to_run = _missing_reviewers(cfg.reviewers, reviewer_results)

    # Dispatch only reviewers that are not already cached for this exact
    # artifact/prompt hash. This makes restart retry the failed panel slots
    # without spending more calls on successful reviewers.
    if to_run:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(len(to_run), 1),
            thread_name_prefix=f"panel-{group_spec['name']}",
        ) as pool:
            futs = [
                pool.submit(
                    _invoke_reviewer,
                    r,
                    group_spec["reviewer_prompt"],
                    cfg.reviewer_probe_interval_sec,
                    cwd=vendor_cwd,
                    feature_active=feature_active,
                    probe_config=probe_config,
                    log_emit=log_emit,
                )
                for r in to_run
            ]
            for fut in concurrent.futures.as_completed(futs):
                reviewer_results.append(fut.result())

    # Sort by configured vendor order for stable audit output.
    order = {r.vendor: i for i, r in enumerate(cfg.reviewers)}
    reviewer_results.sort(key=lambda r: order.get(r.vendor, 999))

    per_vendor_raw = {r.vendor: r.output for r in reviewer_results if r.ok}
    for r in reviewer_results:
        if not r.ok:
            per_vendor_raw[r.vendor] = f"{NO_RESPONSE_PREFIX} — {r.failure_detail}]"

    out_path = feature_active / group_spec["verdict_file"]
    _write_reviewer_cache(
        feature_active=feature_active,
        gate_label=group_spec["name"],
        reviewer_specs=cfg.reviewers,
        metadata=metadata,
        reviewer_results=reviewer_results,
        prior_failures=prior_failures,
    )
    missing = _missing_reviewers(cfg.reviewers, reviewer_results)
    if missing:
        if log_emit is not None:
            log_emit({
                "event": "panel-incomplete",
                "stage": "gate",
                "gate": group_spec["name"],
                "missing_reviewers": [m.vendor for m in missing],
            })
        raise GatePending(
            f"panel-{group_spec['name']}",
            _panel_incomplete_message(
                gate_label=group_spec["name"],
                reviewer_specs=cfg.reviewers,
                reviewer_results=reviewer_results,
            ),
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

    Each group runs its own pipeline (3 reviewers → synthesizer) concurrently
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
    to_run = _missing_reviewers(cfg.reviewers, reviewer_results)
    if to_run:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(len(to_run), 1)
        ) as pool:
            futs = {
                pool.submit(
                    _invoke_reviewer,
                    r,
                    reviewer_prompt,
                    cfg.reviewer_probe_interval_sec,
                    cwd=vendor_cwd,
                    feature_active=feature_active,
                    probe_config=probe_config,
                    log_emit=log_emit,
                ): r
                for r in to_run
            }
            for fut in concurrent.futures.as_completed(futs):
                reviewer_results.append(fut.result())
    # Preserve reviewer order as declared in config (stable audit).
    order = {r.vendor: i for i, r in enumerate(cfg.reviewers)}
    reviewer_results.sort(key=lambda r: order.get(r.vendor, 999))

    per_vendor_raw = {r.vendor: r.output for r in reviewer_results if r.ok}
    for r in reviewer_results:
        if not r.ok:
            per_vendor_raw[r.vendor] = f"{NO_RESPONSE_PREFIX} — {r.failure_detail}]"

    _write_reviewer_cache(
        feature_active=feature_active,
        gate_label=gate,
        reviewer_specs=cfg.reviewers,
        metadata=metadata,
        reviewer_results=reviewer_results,
        prior_failures=prior_failures,
    )
    missing = _missing_reviewers(cfg.reviewers, reviewer_results)
    if missing:
        if log_emit is not None:
            log_emit({
                "event": "panel-incomplete",
                "stage": "gate",
                "gate": gate,
                "missing_reviewers": [m.vendor for m in missing],
            })
        raise GatePending(
            f"panel-{gate}",
            _panel_incomplete_message(
                gate_label=gate,
                reviewer_specs=cfg.reviewers,
                reviewer_results=reviewer_results,
            ),
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
