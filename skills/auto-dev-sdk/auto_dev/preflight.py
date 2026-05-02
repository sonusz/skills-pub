"""Preflight checks: lock / orphan .tmp / staleness / hash handoff."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from auto_dev.errors import LockConflict
from auto_dev.state.cascade import StalenessCascade
from auto_dev.state.lock import read_owner


@dataclass
class PreflightReport:
    lock_held_by: dict | None = None
    orphan_tmps: list[str] = field(default_factory=list)
    stale_artifacts: list[str] = field(default_factory=list)
    next_stage: str = "prd"


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


def preflight(feature_active: Path, *, reject_on_lock: bool = True) -> PreflightReport:
    feature_active = Path(feature_active)
    report = PreflightReport()

    # 1. Lock check.
    owner = read_owner(feature_active)
    if owner is not None:
        report.lock_held_by = owner
        if reject_on_lock:
            raise LockConflict(f"lock held: {owner}")

    # 2. Orphan .tmp scan (reported only).
    report.orphan_tmps = scan_orphan_tmps(feature_active)

    # 3. Staleness cascade + next stage.
    cascade = StalenessCascade(feature_active)
    report.stale_artifacts = cascade.stale()
    report.next_stage = cascade.next_stage()

    return report
