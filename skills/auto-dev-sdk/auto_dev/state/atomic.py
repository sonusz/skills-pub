"""Atomic write: write to `<name>.tmp`, fsync, rename.

Rename is atomic on POSIX filesystems. If the process dies between tmp write
and rename, the target file is untouched; the orphan .tmp is reported (not
auto-deleted) by preflight.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write(path: Path, data: bytes | str) -> None:
    """Write bytes/str to `path` atomically.

    Raises whatever the underlying filesystem raises. On failure, the `.tmp`
    scratch file is cleaned up; the pre-existing target (if any) is untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any, *, indent: int = 2) -> None:
    """JSON variant — pretty-printed, trailing newline, deterministic separators."""
    text = json.dumps(obj, indent=indent, ensure_ascii=False, sort_keys=False) + "\n"
    atomic_write(path, text)
