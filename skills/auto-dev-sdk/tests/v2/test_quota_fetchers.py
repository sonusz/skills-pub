"""Per-vendor quota parsing (binding-window min + reset), with HTTP/creds stubbed.

Fixtures are shaped like the real provider usage payloads (cf. Mana's
ManaTests/Fixtures/{claude,cursor,gemini,codex}). We stub credential discovery
and the HTTP getter so no network/creds are needed.
"""
from __future__ import annotations

import base64
import json

from autodev.vendors.quota import claude, codex, cursor, gemini


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


def test_gemini_family_filter(monkeypatch):
    monkeypatch.setattr(gemini, "read_json_file", lambda p: {"access_token": "t"})
    monkeypatch.setattr(gemini, "_project", lambda token: None)
    quota = {
        "models": [
            {"name": "gemini-pro", "windows": [{"utilization": 30, "resets_at": "2026-05-02T09:00:00+00:00"}]},
            {"name": "gemini-flash", "windows": [{"utilization": 90, "resets_at": "2026-05-02T09:00:00+00:00"}]},
        ]
    }
    monkeypatch.setattr(gemini, "http_json", lambda url, **k: (200, quota))
    r = gemini.fetch(model="gemini-3.1-pro")
    assert r.ok and r.remaining_pct == 70.0  # pro family only (flash excluded)


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
