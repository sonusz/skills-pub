"""Preflight checks before writes (R1 / §4 git constraint)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from autodev.errors import LockConflict
from autodev.state.cascade import StalenessCascade
from autodev.state.lock import read_owner
from autodev.workspace import ensure_git_repo, is_git_repo


@dataclass
class PreflightReport:
    lock_held_by: dict | None = None
    orphan_tmps: list[str] = field(default_factory=list)
    stale_artifacts: list[str] = field(default_factory=list)
    next_stage: str = "prd"
    is_git: bool = False


def scan_orphan_tmps(root: Path, *, older_than_sec: int = 3600) -> list[str]:
    now = time.time()
    orphans: list[str] = []
    for p in Path(root).rglob("*.tmp"):
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age >= older_than_sec:
            orphans.append(str(p))
    return orphans


def preflight_feature(feature_active: Path, *, reject_on_lock: bool = True) -> PreflightReport:
    feature_active = Path(feature_active)
    report = PreflightReport()

    owner = read_owner(feature_active)
    if owner is not None:
        report.lock_held_by = owner
        if reject_on_lock:
            raise LockConflict(f"lock held: {owner}")

    report.orphan_tmps = scan_orphan_tmps(feature_active)
    cascade = StalenessCascade(feature_active)
    report.stale_artifacts = cascade.stale()
    report.next_stage = cascade.next_stage()
    report.is_git = is_git_repo(feature_active)
    return report


def preflight_repo_root(repo_root: Path) -> None:
    """Run at `autodev prd` — refuse non-git dirs (§4)."""
    ensure_git_repo(Path(repo_root))
