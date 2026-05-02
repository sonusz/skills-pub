"""Filesystem state primitives: atomic write, locking, hashing, cascade, JSONL log."""

from auto_dev.state.atomic import atomic_write, atomic_write_json
from auto_dev.state.hashing import HASH_PREFIX, hash_file, hash_bytes, parse_markdown_source_hash
from auto_dev.state.lock import Lock, read_owner
from auto_dev.state.log import JsonlLog
from auto_dev.state.cascade import StalenessCascade

__all__ = [
    "atomic_write",
    "atomic_write_json",
    "HASH_PREFIX",
    "hash_file",
    "hash_bytes",
    "parse_markdown_source_hash",
    "Lock",
    "read_owner",
    "JsonlLog",
    "StalenessCascade",
]
