"""Durable registry for in-flight shared-vendor process groups.

Every real vendor call starts a new session, so killing only the orchestrator
does not necessarily terminate its reviewer/synthesizer children. This module
records each session leader while it is alive. ``autodev abort`` can then stop
all registered groups before releasing the feature lock.
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from autodev.state.atomic import atomic_write_json

REGISTRY_FILENAME = ".running-pids.json"

_REGISTRY_LOCK = threading.RLock()


def registry_path(feature_active: Path) -> Path:
    return Path(feature_active) / REGISTRY_FILENAME


def _process_start_id(pid: int) -> str | None:
    """Return Linux's immutable per-PID start tick, when available."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # comm is parenthesized and may contain spaces. Fields after the final
        # ')' begin with field 3 (state); starttime is field 22 => index 19.
        rest = raw[raw.rfind(")") + 2 :].split()
        return rest[19]
    except (OSError, IndexError):
        return None


def _load_unlocked(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    processes = raw.get("processes", []) if isinstance(raw, dict) else []
    return [entry for entry in processes if isinstance(entry, dict)]


def _write_unlocked(path: Path, entries: list[dict]) -> None:
    if not entries:
        path.unlink(missing_ok=True)
        return
    atomic_write_json(path, {
        "version": 1,
        "processes": entries,
    })


def register_process(path: Path, *, pid: int, label: str) -> None:
    """Register a new-session subprocess immediately after ``Popen``."""
    path = Path(path)
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return
    entry = {
        "pid": pid,
        "pgid": pgid,
        "owner_pid": os.getpid(),
        "label": label,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "process_start_id": _process_start_id(pid),
    }
    with _REGISTRY_LOCK:
        entries = [
            prior for prior in _load_unlocked(path)
            if prior.get("pid") != pid
        ]
        entries.append(entry)
        _write_unlocked(path, entries)


def unregister_process(path: Path, *, pid: int) -> None:
    path = Path(path)
    with _REGISTRY_LOCK:
        entries = [
            entry for entry in _load_unlocked(path)
            if entry.get("pid") != pid
        ]
        _write_unlocked(path, entries)


def read_processes(path: Path) -> list[dict]:
    with _REGISTRY_LOCK:
        return list(_load_unlocked(Path(path)))


def _entry_is_same_live_process(entry: dict) -> bool:
    pid = entry.get("pid")
    pgid = entry.get("pgid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    try:
        os.kill(pid, 0)
        if os.getpgid(pid) != pgid:
            return False
    except (ProcessLookupError, PermissionError, OSError):
        return False
    expected_start = entry.get("process_start_id")
    actual_start = _process_start_id(pid)
    if expected_start and actual_start and expected_start != actual_start:
        return False
    return True


def process_group_alive(pgid: int) -> bool:
    proc_root = Path("/proc")
    if proc_root.is_dir():
        scanned = False
        try:
            for stat_path in proc_root.glob("[0-9]*/stat"):
                try:
                    raw = stat_path.read_text(encoding="utf-8")
                    rest = raw[raw.rfind(")") + 2 :].split()
                    state = rest[0]
                    process_group = int(rest[2])
                except (OSError, IndexError, ValueError):
                    continue
                scanned = True
                if process_group == pgid and state != "Z":
                    return True
            if scanned:
                # A group containing only zombies has no executable work left;
                # its parent will reap it while abort unwinds.
                return False
        except OSError:
            pass
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_process_group(pgid: int, *, grace_sec: float = 0.5) -> bool:
    """Stop one process group, returning True once no live member remains."""
    if not isinstance(pgid, int) or pgid <= 0 or pgid == os.getpgrp():
        return False
    if not process_group_alive(pgid):
        return True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + max(grace_sec, 0.0)
    while process_group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)
    if process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        kill_deadline = time.monotonic() + 1.0
        while process_group_alive(pgid) and time.monotonic() < kill_deadline:
            time.sleep(0.02)
    return not process_group_alive(pgid)


@dataclass(frozen=True)
class TerminationSummary:
    pids: tuple[int, ...] = ()
    pgids: tuple[int, ...] = ()
    labels: tuple[str, ...] = ()
    forced_pgids: tuple[int, ...] = ()
    remaining_pgids: tuple[int, ...] = ()

    @property
    def all_stopped(self) -> bool:
        return not self.remaining_pgids


def terminate_registered_processes(
    path: Path,
    *,
    owner_pid: int,
    grace_sec: float = 5.0,
    remove_stopped: bool = True,
) -> TerminationSummary:
    """Terminate live groups registered by the current lock owner.

    PID start identities and the owner PID prevent a stale registry from
    targeting a recycled, unrelated process.
    """
    path = Path(path)
    entries = [
        entry for entry in read_processes(path)
        if entry.get("owner_pid") == owner_pid
        and _entry_is_same_live_process(entry)
    ]
    current_pgid = os.getpgrp()
    pgids = sorted({
        int(entry["pgid"]) for entry in entries
        if int(entry["pgid"]) != current_pgid
    })
    pids = sorted({int(entry["pid"]) for entry in entries})
    labels = sorted({
        str(entry.get("label", "unknown")) for entry in entries
    })

    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + max(grace_sec, 0.0)
    remaining = [pgid for pgid in pgids if process_group_alive(pgid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = [pgid for pgid in remaining if process_group_alive(pgid)]

    forced = list(remaining)
    for pgid in forced:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    kill_deadline = time.monotonic() + 1.0
    while remaining and time.monotonic() < kill_deadline:
        time.sleep(0.05)
        remaining = [
            pgid for pgid in remaining if process_group_alive(pgid)
        ]

    if remove_stopped:
        with _REGISTRY_LOCK:
            survivors = [
                entry for entry in _load_unlocked(path)
                if (
                    entry.get("owner_pid") != owner_pid
                    or entry.get("pgid") in remaining
                )
            ]
            _write_unlocked(path, survivors)

    return TerminationSummary(
        pids=tuple(pids),
        pgids=tuple(pgids),
        labels=tuple(labels),
        forced_pgids=tuple(forced),
        remaining_pgids=tuple(remaining),
    )
