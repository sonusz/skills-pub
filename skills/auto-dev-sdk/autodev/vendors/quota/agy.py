"""Agy remaining-quota lookup.

Agy exposes quota in its interactive UI but currently has no stable,
machine-readable quota subcommand. Return unknown so quota-gated calls fail
closed instead of guessing from private cache files.
"""
from __future__ import annotations

from autodev.vendors.quota.base import QuotaResult


def fetch(model: str | None = None) -> QuotaResult:
    del model
    return QuotaResult.unknown(
        "agy", "agy CLI does not expose machine-readable remaining quota"
    )
