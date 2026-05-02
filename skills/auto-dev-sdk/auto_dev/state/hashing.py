"""SHA-256 hashing with the `sha256:` prefix format.

Hashes are computed over raw bytes on disk. No normalization: a trailing
newline is part of the content and affects the digest.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

HASH_PREFIX = "sha256:"

_MARKDOWN_COMMENT_RE = re.compile(
    r"<!--\s*source_hash:\s*(sha256:[0-9a-f]{64})\s*-->", re.IGNORECASE
)


def hash_bytes(data: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    with open(path, "rb") as f:
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return HASH_PREFIX + h.hexdigest()


def parse_markdown_source_hash(path: Path) -> str | None:
    """Return the embedded `sha256:...` from a markdown file's first ~5 lines, or None."""
    with open(path, "r", encoding="utf-8") as f:
        head = "".join(f.readline() for _ in range(5))
    m = _MARKDOWN_COMMENT_RE.search(head)
    return m.group(1) if m else None
