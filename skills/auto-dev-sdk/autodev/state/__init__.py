"""Filesystem-as-state primitives (forked from v0.1; independent copies)."""

from autodev.state.atomic import atomic_write, atomic_write_json
from autodev.state.hashing import HASH_PREFIX, hash_bytes, hash_file, parse_markdown_source_hash
from autodev.state.lock import Lock, read_owner
from autodev.state.log import JsonlLog
from autodev.state.cascade import StalenessCascade, ArtifactRef, ARTIFACTS

__all__ = [
    "atomic_write", "atomic_write_json",
    "HASH_PREFIX", "hash_bytes", "hash_file", "parse_markdown_source_hash",
    "Lock", "read_owner",
    "JsonlLog",
    "StalenessCascade", "ArtifactRef", "ARTIFACTS",
]
