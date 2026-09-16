"""Durable registry for in-flight shared-vendor process groups.

Every real vendor call starts a new session, so killing only the orchestrator
does not necessarily terminate its reviewer/synthesizer children. This module
records each session leader while it is alive. ``autodev abort`` can then stop
all registered groups before releasing the feature lock.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from autodev.state.atomic import atomic_write_json
from autodev.state.hostos import _host_os

REGISTRY_FILENAME = ".running-pids.json"

# Linux procfs root. A module constant so tests can point it at a fake tree.
_PROC_ROOT = Path("/proc")
# ``ps -o lstart=`` format on macOS, e.g. ``Wed Sep 16 11:00:33 2026``.
_PS_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"

_REGISTRY_LOCK = threading.RLock()


def registry_path(feature_active: Path) -> Path:
    return Path(feature_active) / REGISTRY_FILENAME


def _linux_process_start_id(pid: int) -> str | None:
    """Linux's immutable per-PID start tick from ``/proc/<pid>/stat``."""
    try:
        raw = (_PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8")
        # comm is parenthesized and may contain spaces. Fields after the final
        # ')' begin with field 3 (state); starttime is field 22 => index 19.
        rest = raw[raw.rfind(")") + 2 :].split()
        return rest[19]
    except (OSError, IndexError):
        return None


def _darwin_process_start_id(pid: int) -> str | None:
    """macOS process start time as epoch seconds, from ``ps -o lstart=``.

    ``lstart`` is the kernel's process start time (not the ``ps`` sampling
    time). Resolution is one second, so a PID recycled within the same second
    as its predecessor is indistinguishable by this value alone; the caller
    also checks the owner pid and the process group before signalling.
    """
    ps = shutil.which("ps")
    if not ps:
        return None
    try:
        result = subprocess.run(
            [ps, "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            # ``lstart`` is locale-formatted; pin C so strptime's English
            # month/day names always match (same as shared session-state.py).
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        # strptime tolerates the space-padded day ("Sep  6"); strip() drops
        # the trailing padding ps appends to the column.
        started = time.strptime(result.stdout.strip(), _PS_LSTART_FORMAT)
        return str(int(time.mktime(started)))
    except (ValueError, OverflowError):
        return None


def _process_start_id(pid: int) -> str | None:
    """Return an immutable per-PID start identity, or None when unknown.

    The registry compares it before signalling, so a recycled PID is never
    mistaken for the process that was registered.
    """
    host = _host_os()
    if host == "linux":
        return _linux_process_start_id(pid)
    if host == "darwin":
        return _darwin_process_start_id(pid)
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


def _process_group_alive_via_ps(pgid: int) -> bool | None:
    """Return group liveness from POSIX ``ps``, or None if unavailable.

    On macOS, ``killpg(pgid, 0)`` returns EPERM for a group containing only
    zombies.  A successful process-table scan lets us distinguish that state
    from an inaccessible group that may still contain executable work.
    """
    ps = shutil.which("ps")
    if not ps:
        return None
    try:
        result = subprocess.run(
            [ps, "-axo", "pgid=,stat="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            process_group = int(fields[0])
        except ValueError:
            continue
        if process_group == pgid and not fields[1].startswith("Z"):
            return True
    return False


def _process_group_alive_via_proc(pgid: int) -> bool | None:
    """Linux: scan ``/proc`` for a non-zombie member of ``pgid``.

    Returns None when ``/proc`` is not mounted or nothing could be scanned,
    so the caller can fall back to ``ps``.
    """
    if not _PROC_ROOT.is_dir():
        return None
    scanned = False
    try:
        for stat_path in _PROC_ROOT.glob("[0-9]*/stat"):
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
    return None


def process_group_alive(pgid: int) -> bool:
    host = _host_os()
    if host == "linux":
        alive = _process_group_alive_via_proc(pgid)
        if alive is None:
            # /proc not mounted (minimal containers, chroots): use ps.
            alive = _process_group_alive_via_ps(pgid)
    elif host == "darwin":
        # macOS killpg(pgid, 0) reports EPERM for a zombie-only group, which
        # the last-resort probe below would misread as "alive". A successful
        # ps scan is therefore authoritative here; killpg is consulted only
        # when ps itself is unavailable.
        alive = _process_group_alive_via_ps(pgid)
    else:
        alive = _process_group_alive_via_ps(pgid)
    if alive is not None:
        return alive
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
    except PermissionError:
        return not process_group_alive(pgid)
    deadline = time.monotonic() + max(grace_sec, 0.0)
    while process_group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)
    if process_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError:
            return not process_group_alive(pgid)
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
        except (ProcessLookupError, PermissionError):
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
        except (ProcessLookupError, PermissionError):
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
