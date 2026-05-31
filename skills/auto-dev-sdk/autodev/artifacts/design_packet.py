"""Harness-authored design packet and acceptance marker artifacts."""
from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autodev.artifacts.verdict import (
    load_verdict,
    panel_verdict_transport_incomplete,
)
from autodev.artifacts.workflow_state import ensure_workflow_state, load_workflow_state
from autodev.errors import SchemaError
from autodev.paths import find_repo_root
from autodev.state.atomic import atomic_write_json
from autodev.state.hashing import hash_bytes, hash_file

DESIGN_PACKET_FILENAME = "design-packet.json"
ACCEPTED_DESIGN_FILENAME = "accepted-design.json"

_DESIGN_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("design", "design.md"),
    ("scope", "scope.json"),
    ("trace", "trace.md"),
    ("test_plan", "test-plan.md"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_hash(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hash_bytes(payload)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SchemaError(f"{path.name} invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SchemaError(f"{path.name} must be a JSON object")
    return data


def _current_design_artifacts(active: Path) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for role, filename in _DESIGN_ARTIFACTS:
        path = active / filename
        if not path.exists():
            raise SchemaError(f"design packet missing input artifact: {filename}")
        artifacts.append({
            "role": role,
            "path": str(path),
            "hash": hash_file(path),
        })
    return artifacts


def _parse_validation_commands(design_path: Path) -> list[str]:
    text = design_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Validation commands:"):
            continue
        raw = stripped.split(":", 1)[1].strip()
        if not raw:
            raise SchemaError("design packet missing Validation commands declaration")
        try:
            parsed = ast.literal_eval(raw)
        except (SyntaxError, ValueError) as e:
            raise SchemaError(f"design packet has malformed Validation commands declaration: {e}") from e
        if not isinstance(parsed, list) or not parsed:
            raise SchemaError("design packet Validation commands must be a non-empty list")
        commands = [item for item in parsed if isinstance(item, str) and item.strip()]
        if len(commands) != len(parsed):
            raise SchemaError("design packet Validation commands must contain only non-empty strings")
        return commands
    raise SchemaError("design packet missing Validation commands declaration")


def _authoritative_context_refs(active: Path, *, bootstrap: bool = True) -> list[dict[str, str]]:
    state = ensure_workflow_state(active) if bootstrap else load_workflow_state(active)
    repo_root = find_repo_root(active)
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for rel_path in state["root_context_paths"]:
        if rel_path in seen:
            raise SchemaError(f"workflow-state.root_context_paths duplicates path {rel_path!r}")
        seen.add(rel_path)
        path = repo_root / rel_path
        if not path.exists():
            raise SchemaError(f"workflow-state root context path missing: {rel_path}")
        refs.append({"path": rel_path, "hash": hash_file(path)})
    if len(refs) != len(state["root_context_paths"]):
        raise SchemaError("workflow-state root context paths could not be resolved exactly")
    return refs


def _context_refs(active: Path, *, bootstrap: bool = True) -> list[dict[str, str]]:
    return _authoritative_context_refs(active, bootstrap=bootstrap)


def _feedback_consulted_epoch_matches(
    history_payload: dict[str, Any],
    *,
    prd_path: Path,
    prd_hash: str,
    current_context_refs: list[dict[str, str]],
) -> bool:
    consulted = history_payload.get("consulted_docs", [])
    if not isinstance(consulted, list):
        return False
    repo_root = find_repo_root(prd_path.parent)
    current_context = {
        str(prd_path): prd_hash,
        **{
            str(repo_root / ref["path"]): ref["hash"]
            for ref in current_context_refs
        },
    }
    seen: dict[str, str] = {}
    for entry in consulted:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", ""))
        hash_value = str(entry.get("hash", ""))
        if path in current_context and hash_value:
            seen[path] = hash_value
    return seen == current_context


def _response_to_feedback(
    active: Path,
    *,
    prd_path: Path,
    prd_hash: str,
    current_context_refs: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return the design-changelog.json reference if it exists.

    The changelog replaces accumulated per-round verdict archives in the
    design packet's response_to_feedback field. Legacy review-memory
    artifacts are no longer consumed by the active loop.

    The ``prd_path``, ``prd_hash``, and ``current_context_refs`` parameters
    are retained for call-site stability but are no longer consulted: the
    changelog is append-only and the agent is responsible for reading it
    in CONTEXT_ARTIFACTS, not the harness.
    """
    del prd_path, prd_hash, current_context_refs  # retained for signature stability
    changelog_path = active / "design-changelog.json"
    if not changelog_path.exists():
        return []
    return [{
        "path": str(changelog_path),
        "hash": hash_file(changelog_path),
    }]


def _input_fingerprint(
    *,
    prd_hash: str,
    context_refs: list[dict[str, str]],
    response_to_feedback: list[dict[str, str]],
) -> str:
    return _canonical_hash({
        "schema": "design-packet-input/v1",
        "prd_hash": prd_hash,
        "context_refs": [
            {"path": ref["path"], "hash": ref["hash"]}
            for ref in context_refs
        ],
        "response_to_feedback": [
            {"path": ref["path"], "hash": ref["hash"]}
            for ref in response_to_feedback
        ],
    })


def _review_subject_hash(
    *,
    prd_hash: str,
    artifacts: list[dict[str, str]],
    validation_commands: list[str],
    context_refs: list[dict[str, str]],
    response_to_feedback: list[dict[str, str]],
) -> str:
    return _canonical_hash({
        "schema": "design-review-subject/v2",
        "prd_hash": prd_hash,
        "artifacts": [
            {"role": a["role"], "hash": a["hash"]}
            for a in artifacts
        ],
        "validation_commands": list(validation_commands),
        "context_refs": [
            {"path": ref["path"], "hash": ref["hash"]}
            for ref in context_refs
        ],
        "response_to_feedback": [
            {"path": ref["path"], "hash": ref["hash"]}
            for ref in response_to_feedback
        ],
    })


def _build_design_packet(active: Path, *, bootstrap_workflow_state: bool) -> dict[str, Any]:
    """Create the current design packet payload without writing it."""
    active = Path(active)
    prd_path = active / "prd.md"
    if not prd_path.exists():
        raise SchemaError("design packet missing input artifact: prd.md")
    artifacts = _current_design_artifacts(active)
    prd_hash = hash_file(prd_path)
    context_refs = _context_refs(active, bootstrap=bootstrap_workflow_state)
    validation_error = _context_refs_validation_error(
        active,
        context_refs,
        bootstrap=bootstrap_workflow_state,
    )
    if validation_error is not None:
        raise SchemaError(validation_error)
    response_to_feedback = _response_to_feedback(
        active,
        prd_path=prd_path,
        prd_hash=prd_hash,
        current_context_refs=context_refs,
    )
    validation_commands = _parse_validation_commands(active / "design.md")
    input_fingerprint = _input_fingerprint(
        prd_hash=prd_hash,
        context_refs=context_refs,
        response_to_feedback=response_to_feedback,
    )
    subject_hash = _review_subject_hash(
        prd_hash=prd_hash,
        artifacts=artifacts,
        validation_commands=validation_commands,
        context_refs=context_refs,
        response_to_feedback=response_to_feedback,
    )
    design_path = active / "design.md"
    return {
        "kind": "design-packet",
        "schema_version": 1,
        "source": str(design_path),
        "source_hash": hash_file(design_path),
        "written": _utc_now(),
        "input": {
            "role": "prd",
            "path": str(prd_path),
            "hash": prd_hash,
        },
        "input_fingerprint": input_fingerprint,
        "artifacts": artifacts,
        "validation_commands": validation_commands,
        "response_to_feedback": response_to_feedback,
        "context_refs": context_refs,
        "review_subject_hash": subject_hash,
        "dev_input_hash": subject_hash,
    }


def build_design_packet(active: Path) -> dict[str, Any]:
    return _build_design_packet(active, bootstrap_workflow_state=True)


def write_design_packet(active: Path) -> Path:
    """Write ``design-packet.json`` atomically and return its path."""
    active = Path(active)
    out = active / DESIGN_PACKET_FILENAME
    atomic_write_json(out, build_design_packet(active))
    return out


def _context_refs_validation_error(
    active: Path,
    refs: Any,
    *,
    bootstrap: bool = False,
) -> str | None:
    if not isinstance(refs, list):
        return "design packet context_refs must be a list"
    try:
        expected = _authoritative_context_refs(active, bootstrap=bootstrap)
    except SchemaError as e:
        return str(e)
    expected_by_path = {ref["path"]: ref["hash"] for ref in expected}
    seen: set[str] = set()
    for entry in refs:
        if not isinstance(entry, dict):
            return "design packet context_refs entries must be objects"
        path = entry.get("path")
        hash_value = entry.get("hash")
        if not isinstance(path, str) or not isinstance(hash_value, str):
            return "design packet context_refs entries must contain string path/hash pairs"
        if path in seen:
            return f"design packet context_refs duplicates path {path!r}"
        seen.add(path)
        if path not in expected_by_path:
            return f"design packet context_refs includes unexpected path {path!r}"
        if expected_by_path[path] != hash_value:
            return f"design packet context_refs stale hash for path {path!r}"
    missing = sorted(set(expected_by_path).difference(seen))
    if missing:
        return f"design packet context_refs missing required paths {missing!r}"
    return None


def _validate_context_refs_payload(active: Path, refs: Any) -> bool:
    return _context_refs_validation_error(active, refs, bootstrap=False) is None


def design_packet_fresh(path: Path) -> bool:
    """Return true when ``design-packet.json`` matches current inputs."""
    path = Path(path)
    active = path.parent
    try:
        data = _load_json(path)
        expected = _build_design_packet(active, bootstrap_workflow_state=False)
    except SchemaError:
        return False

    if data.get("kind") != "design-packet":
        return False
    if data.get("schema_version") != 1:
        return False
    if not _validate_context_refs_payload(active, data.get("context_refs")):
        return False
    for key in (
        "source",
        "source_hash",
        "input",
        "input_fingerprint",
        "artifacts",
        "validation_commands",
        "response_to_feedback",
        "context_refs",
        "review_subject_hash",
        "dev_input_hash",
    ):
        if data.get(key) != expected.get(key):
            return False
    return True


def _verdict_fresh_against_packet(verdict_path: Path, packet_path: Path) -> bool:
    try:
        verdict = load_verdict(verdict_path)
    except Exception:
        return False
    if panel_verdict_transport_incomplete(verdict):
        return False
    if verdict.source != str(packet_path):
        return False
    if verdict.source_hash != hash_file(packet_path):
        return False
    for doc in verdict.consulted_docs:
        doc_path = Path(doc.get("path", ""))
        if not doc_path.exists():
            return False
        if hash_file(doc_path) != doc.get("hash", ""):
            return False
    return True


def write_accepted_design(
    active: Path,
    verdict_path: Path | None = None,
    trace_verdict_path: Path | None = None,
) -> Path:
    """Write ``accepted-design.json`` after both design-review and trace-review pass or are skipped."""
    active = Path(active)
    packet_path = active / DESIGN_PACKET_FILENAME
    verdict_path = Path(verdict_path) if verdict_path is not None else active / "panel-design-review.json"
    trace_verdict_path = Path(trace_verdict_path) if trace_verdict_path is not None else active / "panel-trace-review.json"
    if not packet_path.exists():
        raise SchemaError("accepted design missing design-packet.json")
    if not verdict_path.exists():
        raise SchemaError("accepted design missing panel-design-review.json")
    if not trace_verdict_path.exists():
        raise SchemaError("accepted design missing panel-trace-review.json")

    packet = _load_json(packet_path)
    verdict_obj = load_verdict(verdict_path)
    trace_verdict_obj = load_verdict(trace_verdict_path)

    if (
        panel_verdict_transport_incomplete(verdict_obj)
        or verdict_obj.effectively_blocks()
        or verdict_obj.verdict not in ("pass", "skipped")
    ):
        raise SchemaError(
            "accepted design requires non-blocking pass/skipped "
            f"design-review verdict, got {verdict_obj.verdict!r}"
        )
    if (
        panel_verdict_transport_incomplete(trace_verdict_obj)
        or trace_verdict_obj.effectively_blocks()
        or trace_verdict_obj.verdict not in ("pass", "skipped")
    ):
        raise SchemaError(
            "accepted design requires non-blocking pass/skipped "
            f"trace-review verdict, got {trace_verdict_obj.verdict!r}"
        )

    acceptance_mode = "skip_gate" if verdict_obj.verdict == "skipped" else "panel_pass"
    trace_acceptance_mode = "skip_gate" if trace_verdict_obj.verdict == "skipped" else "panel_pass"
    packet_hash = hash_file(packet_path)
    verdict_hash = hash_file(verdict_path)
    trace_verdict_hash = hash_file(trace_verdict_path)
    out = active / ACCEPTED_DESIGN_FILENAME
    atomic_write_json(out, {
        "kind": "accepted-design",
        "schema_version": 2,
        "written": _utc_now(),
        "gate": "design-review",
        "verdict": verdict_obj.verdict,
        "acceptance_mode": acceptance_mode,
        "trace_gate": "trace-review",
        "trace_verdict": trace_verdict_obj.verdict,
        "trace_acceptance_mode": trace_acceptance_mode,
        "source": str(verdict_path),
        "source_hash": verdict_hash,
        "trace_source": str(trace_verdict_path),
        "trace_source_hash": trace_verdict_hash,
        "design_packet_path": str(packet_path),
        "design_packet_hash": packet_hash,
        "review_subject_hash": packet.get("review_subject_hash"),
        "dev_input_hash": packet.get("dev_input_hash"),
    })
    return out


def accepted_design_fresh(path: Path) -> bool:
    """Return true when acceptance still points at the current verdict(s) and packet."""
    path = Path(path)
    active = path.parent
    packet_path = active / DESIGN_PACKET_FILENAME
    verdict_path = active / "panel-design-review.json"
    trace_verdict_path = active / "panel-trace-review.json"
    try:
        data = _load_json(path)
        verdict_obj = load_verdict(verdict_path)
        packet = _load_json(packet_path)
    except (SchemaError, OSError):
        return False
    if data.get("kind") != "accepted-design":
        return False
    schema_version = data.get("schema_version", 1)
    if schema_version not in (1, 2):
        return False
    if (
        panel_verdict_transport_incomplete(verdict_obj)
        or verdict_obj.effectively_blocks()
        or verdict_obj.verdict not in ("pass", "skipped")
    ):
        return False
    if data.get("verdict") != verdict_obj.verdict:
        return False
    if data.get("source") != str(verdict_path):
        return False
    if data.get("source_hash") != hash_file(verdict_path):
        return False
    if schema_version >= 2:
        # Check trace verdict freshness
        try:
            trace_verdict_obj = load_verdict(trace_verdict_path)
        except (SchemaError, OSError):
            return False
        if trace_verdict_obj.effectively_blocks() or trace_verdict_obj.verdict not in ("pass", "skipped"):
            return False
        if panel_verdict_transport_incomplete(trace_verdict_obj):
            return False
        if data.get("trace_source") != str(trace_verdict_path):
            return False
        if data.get("trace_source_hash") != hash_file(trace_verdict_path):
            return False
    if data.get("design_packet_path") != str(packet_path):
        return False
    if data.get("design_packet_hash") != hash_file(packet_path):
        return False
    if data.get("review_subject_hash") != packet.get("review_subject_hash"):
        return False
    if data.get("dev_input_hash") != packet.get("dev_input_hash"):
        return False
    if not design_packet_fresh(packet_path):
        return False
    if not _verdict_fresh_against_packet(verdict_path, packet_path):
        return False
    if not _verdict_fresh_against_packet(trace_verdict_path, packet_path):
        return False
    return True
