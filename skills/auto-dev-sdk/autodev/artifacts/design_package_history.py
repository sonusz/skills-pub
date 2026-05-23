"""Versioned snapshots of design-stage package artifacts."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autodev.errors import SchemaError
from autodev.state.atomic import atomic_write_json
from autodev.state.hashing import hash_bytes, hash_file

HISTORY_DIRNAME = "design-package-history"
DESIGN_PACKAGE_FILENAMES = (
    "design.md",
    "scope.json",
    "trace.md",
    "test-plan.md",
)
DESIGN_PACKAGE_SIDECARS = (
    "design-changelog.json",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _history_dir(feature_active: Path) -> Path:
    return Path(feature_active) / HISTORY_DIRNAME


def _package_hash(artifacts: list[dict[str, str]]) -> str:
    payload = json.dumps(
        [
            {"path": a["path"], "hash": a["hash"]}
            for a in artifacts
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hash_bytes(payload)


def _snapshot_number(path: Path) -> int | None:
    prefix = "package-"
    if not path.name.startswith(prefix):
        return None
    raw = path.name[len(prefix):]
    if not raw.isdigit():
        return None
    return int(raw)


def _existing_snapshots(history_dir: Path) -> list[Path]:
    if not history_dir.exists():
        return []
    snapshots = [
        p for p in history_dir.iterdir()
        if p.is_dir() and _snapshot_number(p) is not None
    ]
    return sorted(snapshots, key=lambda p: _snapshot_number(p) or 0)


def _load_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _current_artifacts(feature_active: Path) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for name in DESIGN_PACKAGE_FILENAMES:
        path = feature_active / name
        if not path.exists():
            raise SchemaError(f"design package missing artifact: {name}")
        artifacts.append({
            "path": name,
            "hash": hash_file(path),
            "role": "package",
        })
    for name in DESIGN_PACKAGE_SIDECARS:
        path = feature_active / name
        if path.exists():
            artifacts.append({
                "path": name,
                "hash": hash_file(path),
                "role": "sidecar",
            })
    return artifacts


def archive_design_package(feature_active: Path) -> Path:
    """Archive the current design package if this exact version is new."""
    feature_active = Path(feature_active)
    artifacts = _current_artifacts(feature_active)
    package_hash = _package_hash(artifacts)
    history_dir = _history_dir(feature_active)
    history_dir.mkdir(parents=True, exist_ok=True)

    snapshots = _existing_snapshots(history_dir)
    for snapshot in snapshots:
        manifest = _load_manifest(snapshot / "manifest.json")
        if manifest is not None and manifest.get("package_hash") == package_hash:
            return snapshot

    next_no = max((_snapshot_number(p) or 0 for p in snapshots), default=0) + 1
    snapshot_dir = history_dir / f"package-{next_no:03d}"
    tmp_dir = history_dir / f".package-{next_no:03d}.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        for artifact in artifacts:
            src = feature_active / artifact["path"]
            dst = tmp_dir / artifact["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        atomic_write_json(tmp_dir / "manifest.json", {
            "kind": "design-package-snapshot",
            "schema_version": 1,
            "written": _utc_now(),
            "package_hash": package_hash,
            "artifacts": artifacts,
        })
        tmp_dir.rename(snapshot_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return snapshot_dir
