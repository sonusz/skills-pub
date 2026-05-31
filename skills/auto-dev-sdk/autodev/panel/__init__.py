"""G15 internal panel-review subpackage + public gate-driver API.

G15 replaces the previous `panel-review` skill + markdown regex wrapper
(`autodev/panel_wrapper.py`) with a harness-owned implementation:

  1. Parallel reviewer dispatch (claude / gemini / codex) with open-ended
     markdown prompts — reviewers write judgment prose, not JSON.
  2. Configured synthesizer LLM call that reads the three markdown
     outputs and produces one schema-constrained
     panel-verdict.
  3. Mechanical fallback when synthesis fails (conservative union rule).

Public entry points (back-compat with pre-G15 `autodev.panel` module):
  - `run_panel_gate` — gate driver used by the orchestrator.
  - `prompt_path(gate)` — resolve reviewer prompt for hash audit.
  - `verdict_exists_and_valid` — cache-freshness check used by cascade.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from autodev.artifacts.verdict import (
    PanelVerdict,
    load_verdict,
    panel_verdict_transport_incomplete,
)
from autodev.errors import ConfigError, GatePending
from autodev.panel import integrity
from autodev.panel.runner import (
    REVIEW_PROMPT_FILE, review_prompt_path, run_panel_gate_internal,
)
from autodev.vendors.config import PanelConfig, load_vendors_config

PANEL_BIN_ENV = "AUTODEV_PANEL_REVIEW_BIN"
"""Optional: path to a standalone panel-review binary. If it produces a
valid verdict, we prefer it over the internal runner (upstream-
conformance escape hatch). Unset by default."""


GATE_PROMPT_FILE = REVIEW_PROMPT_FILE  # back-compat alias


def prompt_path(gate: str) -> Path:
    """Return the reviewer prompt path for the gate (hash-audit source)."""
    return review_prompt_path(gate)


def _try_standalone_binary(
    *, gate: str, feature_active: Path, primary_artifact: Path,
    prompt_file: Path, docs_path: Path | None,
) -> PanelVerdict | None:
    explicit = os.environ.get(PANEL_BIN_ENV)
    if not explicit or not Path(explicit).exists():
        return None
    verdict_path = feature_active / f"panel-{gate}.json"
    args = [
        explicit, "--gate", gate,
        "--artifact", str(primary_artifact),
        "--prompt-file", str(prompt_file),
        "--out", str(verdict_path),
    ]
    if docs_path:
        args += ["--docs", str(docs_path)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not verdict_path.exists():
        return None
    try:
        return load_verdict(verdict_path)
    except Exception:
        return None


def run_panel_gate(
    *,
    gate: str,
    feature_active: Path,
    repo_root: Path,
    feature: str,
    primary_artifact: Path,
    panel_config: PanelConfig | None = None,
    probe_config: "ProbeConfig | None" = None,
    log_emit: "Callable[[dict], None] | None" = None,
) -> PanelVerdict:
    """Invoke the panel for one gate; write panel-verdict.json."""
    p_prompt = prompt_path(gate)
    if not p_prompt.exists():
        raise ConfigError(f"panel prompt file missing: {p_prompt}")

    consulted_docs: list[dict] = []
    docs_path: Path | None = None
    if gate == "design-review":
        from autodev.state.hashing import hash_file
        seen_paths: set[str] = set()

        def _add(path: Path, *, hash_value: str | None = None) -> None:
            key = str(path)
            if key in seen_paths or not path.exists():
                return
            seen_paths.add(key)
            consulted_docs.append({
                "path": key,
                "hash": hash_value or hash_file(path),
                "priority": "consulted",
            })

        if primary_artifact.exists():
            try:
                packet = json.loads(primary_artifact.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                packet = {}
            for artifact in packet.get("artifacts", []):
                if isinstance(artifact, dict):
                    art_path = artifact.get("path")
                    art_hash = artifact.get("hash")
                    if isinstance(art_path, str):
                        _add(Path(art_path), hash_value=art_hash if isinstance(art_hash, str) else None)
            packet_input = packet.get("input", {})
            if isinstance(packet_input, dict):
                prd_path = packet_input.get("path")
                prd_hash = packet_input.get("hash")
                if isinstance(prd_path, str):
                    _add(Path(prd_path), hash_value=prd_hash if isinstance(prd_hash, str) else None)
            for ref in packet.get("context_refs", []):
                if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                    _add(repo_root / ref["path"], hash_value=ref.get("hash") if isinstance(ref.get("hash"), str) else None)
            for ref in packet.get("response_to_feedback", []):
                if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                    _add(Path(ref["path"]), hash_value=ref.get("hash") if isinstance(ref.get("hash"), str) else None)
        if consulted_docs:
            from autodev.state.atomic import atomic_write_json
            docs_path = feature_active / f"panel-{gate}.docs.json"
            atomic_write_json(docs_path, consulted_docs)
    elif gate == "close-approval":
        # Close-approval reads implemented-spec.md as the primary code
        # fact surface plus PRD and a mechanical PRD checklist. No
        # precomputed coverage map is provided; coverage is the panel's
        # output judgment.
        from autodev.state.hashing import hash_file
        for name in ("prd.md", "prd-checklist.json"):
            p = feature_active / name
            if p.exists():
                consulted_docs.append(
                    {"path": str(p), "hash": hash_file(p), "priority": "consulted"}
                )
        if consulted_docs:
            from autodev.state.atomic import atomic_write_json
            docs_path = feature_active / f"panel-{gate}.docs.json"
            atomic_write_json(docs_path, consulted_docs)

    # Escape hatch: try standalone binary first (upstream conformance).
    standalone = _try_standalone_binary(
        gate=gate, feature_active=feature_active,
        primary_artifact=primary_artifact,
        prompt_file=p_prompt, docs_path=docs_path,
    )
    if standalone is not None:
        return standalone

    # Load panel config from vendors.yml if caller didn't pass one.
    if panel_config is None:
        vendors_yml = repo_root / "vendors.yml"
        if not vendors_yml.exists():
            raise ConfigError(
                "panel_config not provided and repo-root vendors.yml not found"
            )
        panel_config = load_vendors_config(vendors_yml).panel

    # Write-integrity guard. Reviewers run yolo (sandbox bypassed) and
    # concurrently against the same canonical artifacts, so a stray write by
    # one would silently poison the others. Snapshot the review surface
    # before dispatch; if it changed during the round, discard the round and
    # pause for a human decision (do NOT auto-revert — the change may be from
    # another system).
    canonical_files = [primary_artifact] + [
        Path(d["path"]) for d in consulted_docs if d.get("path")
    ]
    guard = integrity.snapshot_before(
        repo_root, gate, canonical_files, log_emit=log_emit,
    )
    try:
        verdict = run_panel_gate_internal(
            gate=gate,
            feature_active=feature_active,
            primary_artifact=primary_artifact,
            prompt_file_for_audit=p_prompt,
            consulted_docs=consulted_docs,
            panel_config=panel_config,
            repo_root=repo_root,
            probe_config=probe_config,
            log_emit=log_emit,
        )
    except BaseException:
        integrity.discard(guard)
        raise

    changes = integrity.detect_after(guard, canonical_files)
    if changes:
        # Round is tainted: drop its verdict + reviewer cache so resume
        # re-runs fresh, then pause. The restore point is preserved (NOT
        # discarded) so a human can roll back manually if it was a reviewer.
        _invalidate_panel_round(feature_active, gate)
        report = integrity.format_report(guard, changes)
        if log_emit is not None:
            log_emit({"event": "panel-integrity-violation",
                      "gate": gate, "changes": changes})
        (feature_active / ".pause").write_text(report, encoding="utf-8")
        raise GatePending(f"panel-{gate}", report)

    integrity.discard(guard)
    return verdict


def _invalidate_panel_round(feature_active: Path, gate: str) -> None:
    """Remove this round's verdict + reviewer-cache files so a resume re-runs
    the panel fresh instead of consuming a verdict built on mutated input."""
    if gate == "design-review":
        gates = ["design-review", "trace-review"]
    else:
        gates = [gate]
    for g in gates:
        for name in (f"panel-{g}.json", f"panel-{g}.reviewers.json"):
            p = feature_active / name
            try:
                p.unlink()
            except (FileNotFoundError, OSError):
                pass


def verdict_exists_and_valid(
    *, feature_active: Path, gate: str, current_source_hash: str
) -> PanelVerdict | None:
    p = feature_active / f"panel-{gate}.json"
    if not p.exists():
        return None
    try:
        v = load_verdict(p)
    except Exception:
        return None
    if panel_verdict_transport_incomplete(v):
        return None
    if v.source_hash != current_source_hash:
        return None  # stale by primary-artifact hash
    # v3-core: also validate every consulted_docs entry's hash matches
    # current file. This catches the multi-upstream primary-pair case
    # where (e.g.) scope.json regenerated but prd.md unchanged would
    # otherwise pass the source_hash check.
    from autodev.state.hashing import hash_file
    for cd in v.consulted_docs:
        cp = Path(cd.get("path", ""))
        if not cp.exists():
            return None
        if hash_file(cp) != cd.get("hash", ""):
            return None
    if gate == "design-review":
        trace_path = feature_active / "panel-trace-review.json"
        if not trace_path.exists():
            return v
        try:
            trace_v = load_verdict(trace_path)
        except Exception:
            return None
        if panel_verdict_transport_incomplete(trace_v):
            return None
        if trace_v.source != v.source or trace_v.source_hash != v.source_hash:
            return None
        for cd in trace_v.consulted_docs:
            cp = Path(cd.get("path", ""))
            if not cp.exists():
                return None
            if hash_file(cp) != cd.get("hash", ""):
                return None
    return v


__all__ = [
    "PANEL_BIN_ENV", "GATE_PROMPT_FILE", "prompt_path",
    "run_panel_gate", "verdict_exists_and_valid",
    "run_panel_gate_internal",
]
