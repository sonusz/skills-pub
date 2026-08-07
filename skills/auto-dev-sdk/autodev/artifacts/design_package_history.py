"""Versioned snapshots of design-stage package artifacts."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
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
DESIGN_REF_ROOT = "refs/autodev/design"


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


def _git(
    repo_root: Path,
    *args: str,
    index_file: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if index_file is not None:
        env["GIT_INDEX_FILE"] = index_file
    # These commits are local harness metadata, not user-authored history.
    env.setdefault("GIT_AUTHOR_NAME", "auto-dev")
    env.setdefault("GIT_AUTHOR_EMAIL", "auto-dev@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "auto-dev")
    env.setdefault("GIT_COMMITTER_EMAIL", "auto-dev@localhost")
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _git_stdout(
    repo_root: Path,
    *args: str,
    index_file: str | None = None,
) -> str:
    result = _git(repo_root, *args, index_file=index_file)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SchemaError(
            f"design package git snapshot failed: git {' '.join(args)}: {detail}"
        )
    return result.stdout.strip()


def _repo_root(feature_active: Path) -> Path:
    raw = _git_stdout(feature_active, "rev-parse", "--show-toplevel")
    root = Path(raw).resolve()
    try:
        feature_active.resolve().relative_to(root)
    except ValueError as exc:
        raise SchemaError(
            f"feature workspace {feature_active} is outside git root {root}"
        ) from exc
    return root


def _package_ref(feature_active: Path, snapshot: Path) -> str:
    feature = feature_active.parent.name
    ref = f"{DESIGN_REF_ROOT}/{feature}/{snapshot.name}"
    result = _git(feature_active, "check-ref-format", ref)
    if result.returncode != 0:
        raise SchemaError(f"invalid design package git ref: {ref!r}")
    return ref


def _snapshot_artifact_names(snapshot: Path) -> list[str]:
    names = [name for name in DESIGN_PACKAGE_FILENAMES if (snapshot / name).is_file()]
    names.extend(
        name for name in DESIGN_PACKAGE_SIDECARS if (snapshot / name).is_file()
    )
    missing = sorted(set(DESIGN_PACKAGE_FILENAMES).difference(names))
    if missing:
        raise SchemaError(
            f"design package {snapshot.name} missing snapshot files: {missing}"
        )
    return names


def _snapshot_tree(
    *, repo_root: Path, feature_active: Path, snapshot: Path,
) -> str:
    """Write a package-only tree under the canonical active-workspace paths.

    A throwaway index prevents changes to the user's branch, real index,
    worktree, or stash. Unlike the panel integrity restore point, this snapshot
    intentionally does not run ``git add -A``: only the five design package
    artifacts become Git objects.
    """
    active_rel = feature_active.resolve().relative_to(repo_root).as_posix()
    fd, index_path = tempfile.mkstemp(prefix="autodev-design-idx-")
    os.close(fd)
    try:
        os.unlink(index_path)  # git creates a fresh index
        for name in _snapshot_artifact_names(snapshot):
            source = snapshot / name
            blob = _git_stdout(repo_root, "hash-object", "-w", "--", str(source))
            canonical_path = f"{active_rel}/{name}"
            _git_stdout(
                repo_root,
                "update-index",
                "--add",
                "--cacheinfo",
                "100644",
                blob,
                canonical_path,
                index_file=index_path,
            )
        return _git_stdout(repo_root, "write-tree", index_file=index_path)
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def _commit_for_tree(
    *,
    repo_root: Path,
    ref: str,
    tree: str,
    parent_commit: str | None,
    message: str,
) -> str:
    existing = _git(
        repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}",
    )
    if existing.returncode == 0 and existing.stdout.strip():
        commit = existing.stdout.strip()
        existing_tree = _git_stdout(repo_root, "rev-parse", f"{commit}^{{tree}}")
        parents = _git_stdout(repo_root, "show", "-s", "--format=%P", commit).split()
        expected_parents = [parent_commit] if parent_commit else []
        if existing_tree == tree and parents == expected_parents:
            return commit

    args = ["commit-tree", tree]
    if parent_commit:
        args.extend(["-p", parent_commit])
    args.extend(["-m", message])
    commit = _git_stdout(repo_root, *args)
    _git_stdout(repo_root, "update-ref", ref, commit)
    return commit


def ensure_design_package_refs(feature_active: Path) -> list[dict[str, str | None]]:
    """Create/backfill a local Git-ref chain for every archived package."""
    feature_active = Path(feature_active)
    snapshots = _existing_snapshots(_history_dir(feature_active))
    if not snapshots:
        return []
    repo_root = _repo_root(feature_active)
    records: list[dict[str, str | None]] = []
    parent_ref: str | None = None
    parent_commit: str | None = None
    for snapshot in snapshots:
        tree = _snapshot_tree(
            repo_root=repo_root,
            feature_active=feature_active,
            snapshot=snapshot,
        )
        ref = _package_ref(feature_active, snapshot)
        commit = _commit_for_tree(
            repo_root=repo_root,
            ref=ref,
            tree=tree,
            parent_commit=parent_commit,
            message=(
                f"autodev design package {feature_active.parent.name} "
                f"{snapshot.name}"
            ),
        )
        manifest_path = snapshot / "manifest.json"
        manifest = _load_manifest(manifest_path)
        if manifest is None:
            raise SchemaError(f"invalid design package manifest: {manifest_path}")
        git_snapshot = {
            "ref": ref,
            "commit": commit,
            "tree": tree,
            "parent_ref": parent_ref,
            "parent_commit": parent_commit,
        }
        if manifest.get("git_snapshot") != git_snapshot:
            manifest["git_snapshot"] = git_snapshot
            atomic_write_json(manifest_path, manifest)
        records.append(git_snapshot)
        parent_ref = ref
        parent_commit = commit
    return records


def latest_design_revision_refs(feature_active: Path) -> dict[str, str | None] | None:
    """Return the latest package ref and its predecessor for panel navigation."""
    snapshots = _existing_snapshots(_history_dir(Path(feature_active)))
    if not snapshots:
        return None
    latest = _load_manifest(snapshots[-1] / "manifest.json") or {}
    current = latest.get("git_snapshot")
    if not isinstance(current, dict) or not current.get("ref"):
        return None
    previous_ref = current.get("parent_ref")
    return {
        "current_package": snapshots[-1].name,
        "current_ref": str(current["ref"]),
        "current_commit": str(current.get("commit") or ""),
        "previous_ref": str(previous_ref) if previous_ref else None,
        "previous_commit": (
            str(current.get("parent_commit"))
            if current.get("parent_commit")
            else None
        ),
    }


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
            ensure_design_package_refs(feature_active)
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

    ensure_design_package_refs(feature_active)
    return snapshot_dir
