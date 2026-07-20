"""Per-vendor remaining-quota lookup with a short TTL cache.

``get_remaining(vendor, model)`` returns a :class:`QuotaResult`. Results are
cached per (vendor, model) for a short TTL (default 45s, ``AUTODEV_QUOTA_TTL_SEC``)
so a Ralph build loop that resolves the gate on every iteration does not hammer
the usage endpoints. A fetcher that raises is coerced to an "unknown" result so
the caller's fail-closed policy applies uniformly.
"""
from __future__ import annotations

import os
import time

from autodev.vendors.quota import claude as _claude
from autodev.vendors.quota import codex as _codex
from autodev.vendors.quota import cursor as _cursor
from autodev.vendors.quota import agy as _agy
from autodev.vendors.quota.base import QuotaResult

_FETCHERS = {
    "claude": _claude.fetch,
    "cursor": _cursor.fetch,
    "agy": _agy.fetch,
    "codex": _codex.fetch,
}

_CACHE: dict[tuple[str, str | None], tuple[QuotaResult, float]] = {}


def normalize_quota_vendor(vendor: str) -> str:
    raw = (vendor or "").strip().lower()
    if raw in {"claude", "anthropic"}:
        return "claude"
    if raw in {"cursor", "cursor-agent", "anysphere"}:
        return "cursor"
    if raw in {"agy", "antigravity"}:
        return "agy"
    if raw in {"codex", "openai", "gpt"}:
        return "codex"
    return raw


def _ttl() -> float:
    try:
        return float(os.environ.get("AUTODEV_QUOTA_TTL_SEC", "45"))
    except ValueError:
        return 45.0


def get_remaining(vendor: str, model: str | None = None, *, force: bool = False) -> QuotaResult:
    norm = normalize_quota_vendor(vendor)
    key = (norm, model)
    ttl = _ttl()
    if not force and ttl > 0:
        cached = _CACHE.get(key)
        if cached is not None and (time.monotonic() - cached[1]) < ttl:
            return cached[0]
    fetcher = _FETCHERS.get(norm)
    if fetcher is None:
        return QuotaResult.unknown(norm, f"no quota fetcher for vendor {vendor!r}")
    try:
        result = fetcher(model)
    except Exception as exc:  # never let a fetcher bug break the run
        result = QuotaResult.unknown(norm, f"fetcher error: {exc}")
    _CACHE[key] = (result, time.monotonic())
    return result


def clear_cache() -> None:
    """Drop the TTL cache (used by tests and by quota-resume re-checks)."""
    _CACHE.clear()


__all__ = ["get_remaining", "clear_cache", "normalize_quota_vendor", "QuotaResult"]
