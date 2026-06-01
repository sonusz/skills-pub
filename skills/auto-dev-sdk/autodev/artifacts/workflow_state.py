"""Minimal workflow-state support for design-packet authority."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autodev.errors import SchemaError
from autodev.paths import find_repo_root
from autodev.state.atomic import atomic_write_json

WORKFLOW_STATE_FILENAME = "workflow-state.json"


def workflow_state_path(feature_active: Path) -> Path:
    return Path(feature_active) / WORKFLOW_STATE_FILENAME


def _validate_root_context_paths(paths: Any) -> list[str]:
    if not isinstance(paths, list) or not paths:
        raise SchemaError("workflow-state.root_context_paths must be a non-empty list")
    normalized: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise SchemaError("workflow-state.root_context_paths entries must be non-empty strings")
        item = raw.strip()
        if item in normalized:
            raise SchemaError(f"workflow-state.root_context_paths duplicates path {item!r}")
        normalized.append(item)
    return normalized


def load_workflow_state(feature_active: Path) -> dict[str, Any]:
    path = workflow_state_path(feature_active)
    if not path.exists():
        raise SchemaError("workflow-state.json missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SchemaError(f"workflow-state.json invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise SchemaError("workflow-state.json must be a JSON object")
    raw["root_context_paths"] = _validate_root_context_paths(raw.get("root_context_paths"))
    return raw


def _extract_architecture_input_section(text: str) -> list[str]:
    in_section = False
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            header = stripped.lstrip("#").strip().lower()
            if header == "architecture input":
                in_section = True
                continue
            if in_section:
                break
        if not in_section:
            continue
        if stripped.startswith("- "):
            value = stripped[2:].strip().strip("`").strip()
            if value:
                items.append(value)
    if not in_section:
        raise SchemaError('architecture.md missing "Architecture Input" section')
    if not items:
        raise SchemaError('architecture.md has empty "Architecture Input" section')
    return items


def _extract_base_ref(text: str) -> str | None:
    """Parse the optional ``## Base ref`` section of architecture.md.

    Returns the declared git ref (branch or commit) the design's grounding
    context is anchored to. Grounding-file freshness is then measured against
    THIS ref, not the working tree: only the base advancing (e.g. ``main``
    gaining commits that touch a grounding file) invalidates the design —
    the feature branch's own build commits do not.

    Returns ``None`` when the section is absent (back-compat: callers fall
    back to working-tree hashing, the pre-base-ref behavior).
    """
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if in_section:
                break
            header = stripped.lstrip("#").strip().lower()
            if header in ("base ref", "base"):
                in_section = True
            continue
        if in_section and stripped:
            # First non-empty content line. Tolerate "- `main`", "`main`", "main".
            value = stripped.lstrip("-").strip().strip("`").strip()
            if value:
                return value
    return None


def _normalize_context_path(repo_root: Path, raw: str) -> str:
    declared = Path(raw)
    if declared.is_absolute():
        raise SchemaError(f'Architecture Input path {raw!r} must be repo-relative')
    resolved = (repo_root / declared).resolve()
    try:
        rel = resolved.relative_to(repo_root.resolve())
    except ValueError as e:
        raise SchemaError(f'Architecture Input path {raw!r} resolves outside the repository root') from e
    if not resolved.exists():
        raise SchemaError(f'Architecture Input path {raw!r} does not exist')
    try:
        resolved.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise SchemaError(f'Architecture Input path {raw!r} does not exist') from e
    except OSError as e:
        raise SchemaError(f'Architecture Input path {raw!r} is unreadable: {e}') from e
    return rel.as_posix()


def discover_root_context_paths(feature_active: Path) -> list[str]:
    feature_active = Path(feature_active)
    repo_root = find_repo_root(feature_active)
    arch_path = feature_active / "architecture.md"
    if not arch_path.exists():
        raise SchemaError("workflow-state bootstrap requires feature-local architecture.md")
    try:
        text = arch_path.read_text(encoding="utf-8")
    except OSError as e:
        raise SchemaError(f"feature-local architecture.md unreadable: {e}") from e
    declared = _extract_architecture_input_section(text)
    normalized = [_normalize_context_path(repo_root, item) for item in declared]
    if len(normalized) != len(set(normalized)):
        raise SchemaError("Architecture Input declares duplicate root context paths")
    return normalized


def discover_base_ref(feature_active: Path) -> str | None:
    """Read the optional ``## Base ref`` declaration from the feature-local
    architecture.md. Returns None when the file or section is absent."""
    arch_path = Path(feature_active) / "architecture.md"
    if not arch_path.exists():
        return None
    try:
        text = arch_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _extract_base_ref(text)


def write_workflow_state(
    feature_active: Path,
    root_context_paths: list[str],
    *,
    base_ref: str | None = None,
) -> Path:
    path = workflow_state_path(feature_active)
    payload: dict[str, Any] = {
        "kind": "workflow-state",
        "schema_version": 1,
        "root_context_paths": _validate_root_context_paths(root_context_paths),
    }
    if base_ref:
        payload["base_ref"] = base_ref
    atomic_write_json(path, payload)
    return path


def bootstrap_workflow_state(feature_active: Path) -> Path:
    return write_workflow_state(
        feature_active,
        discover_root_context_paths(feature_active),
        base_ref=discover_base_ref(feature_active),
    )


def ensure_workflow_state(feature_active: Path) -> dict[str, Any]:
    path = workflow_state_path(feature_active)
    if not path.exists():
        bootstrap_workflow_state(feature_active)
    return load_workflow_state(feature_active)
