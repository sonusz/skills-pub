"""Quota-aware candidate resolution: primary / fallback / halt / fail-closed."""
from __future__ import annotations

from datetime import timedelta

import pytest

from autodev.errors import QuotaHalt
from autodev.vendors import fallback
from autodev.vendors.config import FallbackSpec, StageSpec
from autodev.vendors.quota.base import QuotaResult, now_utc


def _qr(vendor, rem, reset=None):
    return QuotaResult(vendor=vendor, remaining_pct=rem, resets_at=reset, fetched_at=now_utc())


def _spec(min_q, fbs):
    return StageSpec(stage="design", vendor="claude", model="m", min_quota_pct=min_q, fallbacks=tuple(fbs))


def _candidates(min_q, fbs):
    return fallback.build_candidates(_spec(min_q, fbs))


def test_primary_sufficient(monkeypatch):
    monkeypatch.setattr(fallback, "get_remaining", lambda v, m=None, force=False: _qr(v, 50))
    c = fallback.resolve_candidate(
        _candidates(20, [FallbackSpec("cursor", "m2", min_quota_pct=10)]), role="design"
    )
    assert c.vendor == "claude"


def test_first_adequate_fallback(monkeypatch):
    table = {"claude": _qr("claude", 5), "cursor": _qr("cursor", 80)}
    monkeypatch.setattr(fallback, "get_remaining", lambda v, m=None, force=False: table[v])
    c = fallback.resolve_candidate(
        _candidates(20, [FallbackSpec("cursor", "m2", min_quota_pct=10)]), role="design"
    )
    assert c.vendor == "cursor"


def test_agy_to_grok_quota_fallback(monkeypatch):
    table = {"agy": _qr("agy", 4), "grok": _qr("grok", 55)}
    monkeypatch.setattr(fallback, "get_remaining", lambda v, m=None, force=False: table[v])
    c = fallback.resolve_candidate(
        [
            fallback.Candidate("agy", "gemini-3.1-pro-high", min_quota_pct=20),
            fallback.Candidate("grok", "grok-4.5", min_quota_pct=10),
        ],
        role="reviewer",
    )
    assert c.vendor == "grok"


def test_halt_uses_earliest_reset(monkeypatch):
    r_late = now_utc() + timedelta(hours=2)
    r_early = now_utc() + timedelta(hours=1)
    table = {"claude": _qr("claude", 5, r_late), "cursor": _qr("cursor", 3, r_early)}
    monkeypatch.setattr(fallback, "get_remaining", lambda v, m=None, force=False: table[v])
    with pytest.raises(QuotaHalt) as ei:
        fallback.resolve_candidate(
            _candidates(20, [FallbackSpec("cursor", "m2", min_quota_pct=10)]), role="design"
        )
    assert abs((ei.value.resume_at - r_early).total_seconds()) < 1
    assert len(ei.value.diagnostics) == 2


def test_fail_closed_on_unknown(monkeypatch):
    monkeypatch.setattr(fallback, "get_remaining", lambda v, m=None, force=False: _qr(v, None))
    with pytest.raises(QuotaHalt):
        fallback.resolve_candidate(
            _candidates(20, [FallbackSpec("cursor", "m2", min_quota_pct=10)]), role="design"
        )


def test_ungated_primary_skips_quota_fetch(monkeypatch):
    calls = []

    def _spy(v, m=None, force=False):
        calls.append(v)
        return _qr(v, 0)

    monkeypatch.setattr(fallback, "get_remaining", _spy)
    c = fallback.resolve_candidate(_candidates(None, []), role="design")
    assert c.vendor == "claude"
    assert calls == []  # ungated → no quota lookup at all
