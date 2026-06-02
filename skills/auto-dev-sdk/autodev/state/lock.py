"""Single-writer feature lock via atomic `mkdir`, with holder-liveness
reclaim and per-acquire self-validation.

Two failure modes this guards against:

1. **Stale lock from a crashed holder.** If an orchestrator is hard-killed it
   cannot run its release; the `.lock/` dir lingers. A new run must be able to
   tell whether the recorded holder is still alive (reclaim if dead) rather
   than block forever.

2. **Orphaned orchestrator after its lock is released/stolen.** A holder must
   not keep advancing the pipeline after something else removed or reclaimed
   the lock (e.g. an abort that deleted `.lock`, or a force-reclaim). Each
   acquire mints a unique `token`; the holder re-checks `validate()` every
   step and self-terminates the moment the on-disk token stops matching.

The lock is per-feature (`<feature-root>/.lock/`); liveness and validation key
on the recorded `pid`/`token`, never on "any autodev process" — so multiple
features can run concurrently on one machine without interfering.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

from autodev.errors import LockConflict
from autodev.state.atomic import atomic_write_json


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists (signal 0 probe)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def _owner_is_live(owner: dict | None) -> bool:
    """Best-effort: is the lock's recorded holder still running?

    Only decidable on the SAME host (we can only signal local PIDs). A lock
    recorded from a different host is treated as live — we never auto-steal a
    remote holder's lock.
    """
    if not isinstance(owner, dict) or not owner:
        return False
    host = owner.get("host")
    if host and host != socket.gethostname():
        return True  # different host — cannot probe; assume live
    return _pid_alive(owner.get("pid"))


class Lock:
    """Feature-root lock at ``<feature-root>/.lock/``.

    Usage:
        with Lock(feature_root, session_id="...", verb="run") as lk:
            while ...:
                if not lk.validate():   # heartbeat: stop if we lost the lock
                    raise LockConflict("lock lost")
                ...
    """

    def __init__(
        self,
        feature_root: Path,
        *,
        session_id: str,
        verb: str,
        feature: str | None = None,
    ) -> None:
        self.feature_root = Path(feature_root)
        self.lock_dir = self.feature_root / ".lock"
        self.owner_path = self.lock_dir / "owner.json"
        self.session_id = session_id
        self.verb = verb
        self.feature = feature or self.feature_root.parent.name
        # Unique per acquire — the identity this holder validates against.
        self.token = uuid.uuid4().hex

    def _write_owner(self) -> None:
        atomic_write_json(self.owner_path, {
            "session_id": self.session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "verb": self.verb,
            "feature": self.feature,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "token": self.token,
        })

    def acquire(self, *, force: bool = False) -> None:
        try:
            self.lock_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            owner = read_owner(self.feature_root)
            if not force and _owner_is_live(owner):
                raise LockConflict(
                    f"lock held at {self.lock_dir} by "
                    f"pid={(owner or {}).get('pid')} "
                    f"session={(owner or {}).get('session_id')!r}; holder is alive. "
                    f"Stop it (`autodev abort`) or pass force=True after attesting staleness."
                )
            # Holder is dead (crashed without releasing) or force-reclaim:
            # steal the stale lock, then re-create it as ours.
            self._raw_remove()
            try:
                self.lock_dir.mkdir(parents=False, exist_ok=False)
            except FileExistsError as e:
                raise LockConflict(
                    f"lock at {self.lock_dir} was re-acquired concurrently during reclaim"
                ) from e
            except FileNotFoundError as e:
                raise LockConflict(f"parent {self.feature_root} does not exist") from e
        except FileNotFoundError as e:
            raise LockConflict(f"parent {self.feature_root} does not exist") from e
        self._write_owner()

    def validate(self) -> bool:
        """True iff THIS process still holds the lock — i.e. ``owner.json``
        exists and its ``token`` matches the one we wrote on acquire.

        Returns False if the lock was released, deleted, or reclaimed by
        another acquire. A holder that sees False MUST stop immediately to
        avoid running concurrently with whoever now owns the lock.
        """
        owner = read_owner(self.feature_root)
        return bool(owner) and owner.get("token") == self.token

    def _raw_remove(self) -> None:
        try:
            self.owner_path.unlink(missing_ok=True)
            self.lock_dir.rmdir()
        except (FileNotFoundError, OSError):
            pass

    def release(self, *, force: bool = False) -> None:
        # Only remove the lock if it is still OURS. If another acquire reclaimed
        # it (token changed), a late/orphaned release must NOT clobber the new
        # holder's lock. `force=True` overrides this — used by `abort`, which
        # intentionally frees another process's lock after stopping it.
        if not force:
            owner = read_owner(self.feature_root)
            if owner and owner.get("token") not in (None, self.token):
                return
        self._raw_remove()

    def __enter__(self) -> "Lock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def read_owner(feature_root: Path) -> dict | None:
    owner = Path(feature_root) / ".lock" / "owner.json"
    if not owner.exists():
        return None
    try:
        return json.loads(owner.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
