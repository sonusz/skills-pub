"""Per-vendor quota parsing (binding-window min + reset), with HTTP/creds stubbed.

Fixtures are shaped like the real provider usage payloads (cf. Mana's
ManaTests fixtures for Claude, Cursor, and Codex). We stub credential discovery
and the HTTP getter so no network/creds are needed.
"""
from __future__ import annotations

import base64
import json

from autodev.vendors.quota import (
    agy,
    claude,
    codex,
    cursor,
    get_remaining,
    normalize_quota_vendor,
)


def test_claude_binding_window(monkeypatch):
    monkeypatch.setattr(
        claude, "_load_raw", lambda: ({"claudeAiOauth": {"accessToken": "t"}}, "file")
    )
    usage = {
        "five_hour": {"utilization": 86, "resets_at": "2026-05-02T08:10:00+00:00"},
        "seven_day": {"utilization": 17, "resets_at": "2026-05-04T20:00:00+00:00"},
    }
    monkeypatch.setattr(claude, "http_json", lambda url, **k: (200, usage))
    r = claude.fetch()
    assert r.ok and r.remaining_pct == 14.0  # 100-86 (five_hour is most-constrained)
    assert r.resets_at is not None and r.resets_at.year == 2026


def test_claude_missing_creds_is_unknown(monkeypatch):
    monkeypatch.setattr(claude, "_load_raw", lambda: (None, ""))
    r = claude.fetch()
    assert r.remaining_pct is None and not r.ok


def test_cursor_plan_and_ondemand(monkeypatch):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "user1"}).encode()).rstrip(b"=").decode()
    monkeypatch.setattr(cursor, "_token", lambda: f"h.{payload}.s")
    usage = {
        "billingCycleEnd": "2026-05-10T00:00:00+00:00",
        "individualUsage": {
            "plan": {"enabled": True, "totalPercentUsed": 40},
            "onDemand": {"enabled": True, "limit": 100, "remaining": 90},
        },
    }
    monkeypatch.setattr(cursor, "http_json", lambda url, **k: (200, usage))
    r = cursor.fetch()
    assert r.ok and r.remaining_pct == 60.0  # plan 100-40=60 < ondemand 90
    assert r.resets_at is not None


def test_agy_quota_is_unknown():
    r = agy.fetch(model=None)
    assert r.vendor == "agy"
    assert r.remaining_pct is None and not r.ok
    assert r.error is not None and "machine-readable" in r.error
    assert normalize_quota_vendor("Antigravity") == "agy"
    assert get_remaining("agy", force=True).vendor == "agy"


def test_codex_rate_limit_windows(monkeypatch):
    monkeypatch.setattr(codex, "read_json_file", lambda p: {"tokens": {"access_token": "t"}})
    usage = {
        "rate_limit": {
            "primary_window": {"used_percent": 40, "reset_at": 1780354508},
            "secondary_window": {"used_percent": 20, "reset_at": 1780654508},
        }
    }
    monkeypatch.setattr(codex, "http_json", lambda url, **k: (200, usage))
    r = codex.fetch()
    assert r.ok and r.remaining_pct == 60.0  # primary 100-40=60 < secondary 80
    assert r.resets_at is not None


def test_codex_missing_auth_is_unknown(monkeypatch):
    monkeypatch.setattr(codex, "read_json_file", lambda p: None)
    r = codex.fetch()
    assert r.remaining_pct is None and not r.ok
