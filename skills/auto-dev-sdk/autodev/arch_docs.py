"""Architecture-doc discovery for architecture-review gate (R4c, v2-12).

Collect-all (not first-hit), prioritized, total cap 200 KB.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autodev.state.hashing import hash_file

TOTAL_CAP_BYTES = 200_000
MAX_SHIPPED_SPECS = 5


@dataclass
class DocRef:
    path: Path
    size: int
    hash: str
    priority: int  # 1 = highest


def discover(repo_root: Path, feature: str) -> list[DocRef]:
    """Return the full set of docs to feed the arch panel, respecting priority + cap."""
    docs: list[DocRef] = []
    repo_root = Path(repo_root)

    # Priority 1: feature-local architecture.md
    p1 = repo_root / "docs" / "features" / feature / "planned" / "architecture.md"
    if not p1.exists():
        p1 = repo_root / "docs" / "features" / feature / "active" / "architecture.md"
    if p1.exists() and p1.is_file():
        docs.append(_ref(p1, priority=1))

    # Priority 2: repo-global architecture docs
    for name in ("architecture.md", "design.md", "ARCHITECTURE.md"):
        p = repo_root / "docs" / name
        if p.exists() and p.is_file():
            docs.append(_ref(p, priority=2))

    # Priority 3: CLAUDE.md / AGENTS.md at repo root
    for name in ("CLAUDE.md", "AGENTS.md"):
        p = repo_root / name
        if p.exists() and p.is_file():
            docs.append(_ref(p, priority=3))

    # Priority 4: shipped feature specs (complete/spec.md), by feature-dir mtime desc, ≤5
    features_dir = repo_root / "docs" / "features"
    if features_dir.is_dir():
        candidates: list[tuple[float, Path]] = []
        for child in features_dir.iterdir():
            if not child.is_dir() or child.name == feature:
                continue
            complete = child / "complete"
            spec = complete / "spec.md"
            if spec.exists():
                try:
                    candidates.append((child.stat().st_mtime, spec))
                except OSError:
                    continue
        candidates.sort(key=lambda t: t[0], reverse=True)
        for _, p in candidates[:MAX_SHIPPED_SPECS]:
            docs.append(_ref(p, priority=4))

    # Apply 200 KB cap, preferring lower priority numbers first.
    docs.sort(key=lambda d: (d.priority, -d.size))  # priority first; larger first within tier
    total = 0
    kept: list[DocRef] = []
    for d in docs:
        if total + d.size <= TOTAL_CAP_BYTES:
            kept.append(d)
            total += d.size
    return kept


def _ref(path: Path, *, priority: int) -> DocRef:
    stat = path.stat()
    return DocRef(path=path, size=stat.st_size, hash=hash_file(path), priority=priority)
