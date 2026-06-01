"""implementation-index.json -- code/test navigation for spec stage.

The index is harness-authored from build.json. It intentionally copies
only navigational evidence: changed files and test command/results. It
does not carry scope items, diagnoses, deviations, or intended behavior.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autodev.artifacts.build import load_build
from autodev.artifacts.workflow_state import discover_base_ref
from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json
from autodev.state.hashing import hash_file


@dataclass(frozen=True)
class ImplementationIndex:
    source: str
    source_hash: str
    written: str
    kind: str
    sealed_ref: str | None
    files_changed: list[str]
    test_cmd_run: str
    test_exit_code: int
    test_results: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_hash": self.source_hash,
            "written": self.written,
            "kind": self.kind,
            "sealed_ref": self.sealed_ref,
            "files_changed": list(self.files_changed),
            "test_cmd_run": self.test_cmd_run,
            "test_exit_code": self.test_exit_code,
            "test_results": dict(self.test_results),
        }


def _current_git_head(repo_root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    head = proc.stdout.strip()
    return head or None


def _validate(raw: dict[str, Any]) -> None:
    required = (
        "source", "source_hash", "written", "kind", "sealed_ref",
        "files_changed", "test_cmd_run", "test_exit_code", "test_results",
    )
    for key in required:
        if key not in raw:
            raise SchemaError(f"implementation-index.json missing {key}")
    if raw["kind"] != "implementation-index":
        raise SchemaError("implementation-index.json.kind must be implementation-index")
    if not isinstance(raw["source"], str) or not raw["source"]:
        raise SchemaError("implementation-index.json.source must be non-empty string")
    if not isinstance(raw["source_hash"], str) or not raw["source_hash"].startswith("sha256:"):
        raise SchemaError("implementation-index.json.source_hash must start with sha256:")
    if raw["sealed_ref"] is not None and not isinstance(raw["sealed_ref"], str):
        raise SchemaError("implementation-index.json.sealed_ref must be string or null")
    if not isinstance(raw["files_changed"], list) or not all(
        isinstance(p, str) for p in raw["files_changed"]
    ):
        raise SchemaError("implementation-index.json.files_changed must be list[str]")
    if not isinstance(raw["test_cmd_run"], str) or not raw["test_cmd_run"].strip():
        raise SchemaError("implementation-index.json.test_cmd_run must be non-empty string")
    if not isinstance(raw["test_exit_code"], int):
        raise SchemaError("implementation-index.json.test_exit_code must be int")
    if not isinstance(raw["test_results"], dict):
        raise SchemaError("implementation-index.json.test_results must be object")


def _cumulative_changed_files(repo_root: Path, base_ref: str) -> list[str] | None:
    """Files the feature branch changed vs ``base_ref`` — the WHOLE product
    change-set — EXCLUDING the auto-dev harness's own bookkeeping tree
    (``docs/features/**``).

    build.json records only the LAST build iteration's files, which
    under-represents a multi-iteration build; the spec (built from this index)
    must see the entire implementation, or it documents only a slice and
    close-approval flags the rest as "missing". Returns None when git or the
    base ref is unavailable, so the caller falls back to build.files_changed
    (pre-base-ref behavior).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--name-only",
             f"{base_ref}...HEAD", "--", ".", ":(exclude)docs/features/"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def build_implementation_index(
    feature_active: Path, *, repo_root: Path | None = None,
) -> ImplementationIndex:
    build_path = Path(feature_active) / "build.json"
    build = load_build(build_path)
    root = Path(repo_root) if repo_root else Path(feature_active)
    # Prefer the cumulative product change-set vs the declared base ref over
    # build.json's last-iteration files, so the spec sees the WHOLE feature.
    files_changed: list[str] | None = None
    base_ref = discover_base_ref(feature_active)
    if base_ref:
        files_changed = _cumulative_changed_files(root, base_ref)
    if files_changed is None:
        files_changed = list(build.files_changed)
    return ImplementationIndex(
        source=str(build_path),
        source_hash=hash_file(build_path),
        written=date.today().isoformat(),
        kind="implementation-index",
        sealed_ref=_current_git_head(root),
        files_changed=files_changed,
        test_cmd_run=build.test_cmd_run,
        test_exit_code=build.test_exit_code,
        test_results=dict(build.test_results),
    )


def write_implementation_index(
    feature_active: Path, *, repo_root: Path | None = None,
) -> Path:
    path = Path(feature_active) / "implementation-index.json"
    idx = build_implementation_index(feature_active, repo_root=repo_root)
    data = idx.to_dict()
    _validate(data)
    atomic_write_json(path, data)
    return path


def load_implementation_index(path: Path) -> ImplementationIndex:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(raw)
    return ImplementationIndex(
        source=raw["source"],
        source_hash=raw["source_hash"],
        written=raw["written"],
        kind=raw["kind"],
        sealed_ref=raw["sealed_ref"],
        files_changed=list(raw["files_changed"]),
        test_cmd_run=raw["test_cmd_run"],
        test_exit_code=raw["test_exit_code"],
        test_results=dict(raw["test_results"]),
    )
