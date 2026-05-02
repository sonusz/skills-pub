"""Close stage: move active → {complete|retiring|deferred|cancelled} with cleanup."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from auto_dev.state.atomic import atomic_write


VALID_REASONS = ("complete", "retiring", "deferred", "cancelled")


# Per-reason artifact policy (PRD / SKILL.md close table).
# `keep` = files to retain relative to active/; everything else is stripped.
# `archive_remaining` = on cancelled, rest goes to history/.
_POLICY = {
    "complete": dict(
        keep=("prd.md", "spec.md", "README.md", "external-dependency", "review.md"),
        strip=("scope.json", "trace.md", "test-plan.md", "build.json", ".lock", ".gates"),
        archive_remaining=False,
        banner=None,
    ),
    "retiring": dict(
        keep=("prd.md", "spec.md", "README.md", "external-dependency", "review.md"),
        strip=("scope.json", "trace.md", "test-plan.md", "build.json", ".lock", ".gates"),
        archive_remaining=False,
        banner="**Status: Retiring**",
    ),
    "deferred": dict(
        keep="*",
        strip=(".lock",),
        archive_remaining=False,
        banner=None,
    ),
    "cancelled": dict(
        keep=(),
        strip=(".lock", ".gates"),
        archive_remaining=True,
        banner=None,
    ),
}


@dataclass
class CloseResult:
    new_path: Path
    archived_to: Path | None = None


def close_feature(
    feature_base: Path,
    reason: str,
    *,
    cancel_note: str = "",
    decided_by: str = "cli-user",
) -> CloseResult:
    if reason not in VALID_REASONS:
        raise ValueError(f"unknown close reason: {reason!r}")
    feature_base = Path(feature_base)
    active = feature_base / "active"
    if not active.is_dir():
        raise FileNotFoundError(f"no active/ to close under {feature_base}")
    dest_status = reason
    dest = feature_base / dest_status
    if dest.exists():
        raise FileExistsError(f"{dest} already exists; resolve manually")

    policy = _POLICY[reason]

    # Remove strip entries (lock, gates, etc.) before move.
    for s in policy["strip"]:
        target = active / s
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)

    if reason == "cancelled":
        # Archive remaining artifacts under history/.
        history = active / "history" / f"cancelled-{date.today().strftime('%Y%m%d')}"
        history.mkdir(parents=True, exist_ok=True)
        for item in list(active.iterdir()):
            if item.name in ("history",):
                continue
            shutil.move(str(item), str(history / item.name))
        atomic_write(
            active / "CANCELLED.md",
            (
                f"# CANCELLED\n\n"
                f"Reason: {cancel_note or reason}\n"
                f"Decided by: {decided_by}\n"
                f"Date: {date.today().isoformat()}\n"
            ),
        )

    elif policy["keep"] != "*":
        # Strip everything not in keep.
        keep = set(policy["keep"])
        for item in list(active.iterdir()):
            if item.name in keep:
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)

    if policy["banner"] and (active / "spec.md").exists():
        spec = active / "spec.md"
        text = spec.read_text(encoding="utf-8")
        banner = policy["banner"] + "\n\n"
        if banner not in text[:200]:
            text = banner + text
            atomic_write(spec, text)

    shutil.move(str(active), str(dest))
    archived = (dest / "history" / f"cancelled-{date.today().strftime('%Y%m%d')}") if reason == "cancelled" else None
    return CloseResult(new_path=dest, archived_to=archived)
