"""Feature folder path helpers (status + lookup)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

STATUS_DIRS = ("planned", "active", "complete", "retiring", "deferred", "cancelled")


@dataclass
class FeaturePaths:
    repo_root: Path
    feature: str

    @property
    def base(self) -> Path:
        return self.repo_root / "docs" / "features" / self.feature

    def current_status(self) -> str | None:
        for s in STATUS_DIRS:
            if (self.base / s).is_dir():
                return s
        return None

    def status_dir(self, status: str) -> Path:
        if status not in STATUS_DIRS:
            raise ValueError(f"unknown status: {status!r}")
        return self.base / status

    def active(self) -> Path:
        return self.status_dir("active")

    def log_path(self) -> Path:
        return self.active() / "log.jsonl"


def find_repo_root(start: Path) -> Path:
    """Walk up until we find `docs/features/` or hit filesystem root."""
    start = Path(start).resolve()
    for candidate in [start, *start.parents]:
        if (candidate / "docs" / "features").is_dir():
            return candidate
    return start
