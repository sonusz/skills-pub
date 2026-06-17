"""Quota-pause record + state fingerprint + recovery recheck.

When the fallback resolver raises :class:`QuotaHalt`, the run pauses cleanly and
records (a) the earliest time any skipped candidate is expected to recover and
(b) a *fingerprint* of the repo + feature state at pause time. A later
``autodev quota-resume`` auto-continues ONLY if the run is still quota-paused,
the time has been reached, quota has actually recovered, and the fingerprint is
unchanged — so any commit, manual edit, abort, or other run cancels auto-resume.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from autodev.state.atomic import atomic_write_json
from autodev.state.hashing import hash_bytes, hash_file

PAUSE_FILE = ".pause"
QUOTA_PAUSE_FILE = ".quota-pause.json"
PAUSE_QUOTA_MARKER = "paused-by-quota"

# Active-dir entries that change on every run / are the pause artifacts
# themselves — excluded from the fingerprint so it reflects "did the work change",
# not "did we just write the pause record".
_FP_EXCLUDE_NAMES = {PAUSE_FILE, QUOTA_PAUSE_FILE, "log.jsonl"}
_FP_EXCLUDE_SUFFIXES = (".tmp", ".stdout.log", ".stderr.log")


def _git(cwd: Path | str, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def compute_fingerprint(active: Path) -> str:
    """Hash of: git HEAD + working-tree status OUTSIDE the active subtree + a
    tree-hash of the active dir (excluding volatile/pause files).

    Detects commits, manual edits anywhere in the repo, and any feature-state
    change since the pause. The active subtree is excluded from `git status`
    (and covered authoritatively by the tree-hash) so that writing the pause
    files themselves does not change the fingerprint."""
    parts: list[str] = []
    toplevel = _git(active, "rev-parse", "--show-toplevel").strip()
    parts.append("HEAD:" + _git(active, "rev-parse", "HEAD").strip())
    # Repo-wide working-tree changes, excluding the active subtree.
    status = ""
    if toplevel:
        try:
            rel = active.resolve().relative_to(Path(toplevel).resolve()).as_posix()
        except ValueError:
            rel = ""
        if rel:
            status = _git(
                toplevel, "status", "--porcelain", "--untracked-files=all",
                "--", ".", f":(exclude){rel}", f":(exclude){rel}/**",
            )
        else:
            status = _git(toplevel, "status", "--porcelain", "--untracked-files=all")
    parts.append("STATUS:\n" + status)
    if active.is_dir():
        for p in sorted(active.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(active).as_posix()
            if (
                p.name in _FP_EXCLUDE_NAMES
                or p.name.endswith(_FP_EXCLUDE_SUFFIXES)
                or rel.startswith(".lock")
            ):
                continue
            try:
                parts.append(f"{rel}:{hash_file(p)}")
            except OSError:
                continue
    return hash_bytes("\n".join(parts).encode("utf-8"))


def write_quota_pause(
    active: Path,
    *,
    role: str,
    resume_at: datetime | None,
    diagnostics: list[dict],
    now: datetime | None = None,
) -> dict:
    """Write `.pause` (quota marker) + `.quota-pause.json` and return the record."""
    now = now or datetime.now(timezone.utc)
    record = {
        "kind": "quota",
        "role": role,
        "resume_at": resume_at.isoformat() if resume_at else None,
        "created_at": now.isoformat(),
        "diagnostics": diagnostics,
        "fingerprint": compute_fingerprint(active),
    }
    (active / PAUSE_FILE).write_text(PAUSE_QUOTA_MARKER + "\n", encoding="utf-8")
    atomic_write_json(active / QUOTA_PAUSE_FILE, record)
    return record


def read_quota_pause(active: Path) -> dict | None:
    path = active / QUOTA_PAUSE_FILE
    if not path.exists():
        return None
    import json

    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def is_quota_paused(active: Path) -> bool:
    """True iff the run is still in the quota-pause state THIS module wrote:
    the `.pause` sentinel exists AND still carries the quota marker (not a manual
    pause / abort), AND the `.quota-pause.json` record is present."""
    pause = active / PAUSE_FILE
    if not pause.exists() or not (active / QUOTA_PAUSE_FILE).exists():
        return False
    try:
        return pause.read_text(encoding="utf-8").strip() == PAUSE_QUOTA_MARKER
    except OSError:
        return False


def clear_quota_pause(active: Path) -> None:
    """Remove the quota-pause record AND the `.pause` sentinel (resume)."""
    for name in (QUOTA_PAUSE_FILE, PAUSE_FILE):
        try:
            (active / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def recheck_recovery(record: dict) -> tuple[bool, datetime | None]:
    """Re-fetch quota for the recorded (skipped) candidates, bypassing the cache.

    Returns ``(recovered, earliest_reset)`` — ``recovered`` is True if any
    candidate now meets its min; otherwise ``earliest_reset`` is the soonest
    expected recovery among the still-insufficient candidates (or None)."""
    from autodev.vendors.quota import clear_cache, get_remaining

    clear_cache()
    earliest: datetime | None = None
    for diag in record.get("diagnostics", []):
        vendor = diag.get("vendor")
        model = diag.get("model")
        min_q = diag.get("min_quota_pct")
        if not vendor or min_q is None:
            continue
        q = get_remaining(vendor, model, force=True)
        if q.remaining_pct is not None and q.remaining_pct >= float(min_q):
            return True, None
        if q.resets_at is not None and (earliest is None or q.resets_at < earliest):
            earliest = q.resets_at
    return False, earliest
