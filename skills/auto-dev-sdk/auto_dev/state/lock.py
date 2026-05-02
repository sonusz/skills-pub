"""Single-writer lock via atomic `mkdir`.

`mkdir` is atomic on POSIX: whichever process wins the directory creation
owns the lock. Stale detection is **user-attested only** — we do not check
process liveness automatically.
"""
from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from auto_dev.errors import LockConflict
from auto_dev.state.atomic import atomic_write_json


class Lock:
    """Feature-root lock at `<feature-root>/.lock/`.

    Usage::

        with Lock(feature_root, session_id="...", verb="implement"):
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

    def acquire(self, *, force: bool = False) -> None:
        try:
            self.lock_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            if not force:
                raise LockConflict(
                    f"lock held by another owner at {self.lock_dir}; "
                    f"pass force=True after user attests staleness"
                )
        except FileNotFoundError:
            raise LockConflict(
                f"parent {self.feature_root} does not exist; cannot create lock"
            )

        payload = {
            "session_id": self.session_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "verb": self.verb,
            "feature": self.feature,
            "host": socket.gethostname(),
            "pid": os.getpid(),
        }
        atomic_write_json(self.owner_path, payload)

    def release(self) -> None:
        try:
            self.owner_path.unlink(missing_ok=True)
            self.lock_dir.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Extra file(s) inside .lock/ — don't destroy evidence.
            pass

    def __enter__(self) -> "Lock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def read_owner(feature_root: Path) -> dict | None:
    owner = Path(feature_root) / ".lock" / "owner.json"
    if not owner.exists():
        return None
    try:
        return json.loads(owner.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@contextmanager
def maybe_lock(feature_root: Path, session_id: str, verb: str) -> Iterator[Lock | None]:
    """Context manager that yields a held lock, or None if skipped (dry-run)."""
    lock = Lock(feature_root, session_id=session_id, verb=verb)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
