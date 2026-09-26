"""Per-vendor quota parsing (binding-window min + reset), with HTTP/creds stubbed.

Fixtures are shaped like the real provider usage payloads (cf. Mana's
ManaTests fixtures for Claude, Cursor, and Codex). We stub credential discovery
and the HTTP getter so no network/creds are needed.
"""
from __future__ import annotations

import pytest

import base64
import json

from autodev.vendors.quota import (
    agy,
    claude,
    codex,
    cursor,
    grok,
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


def test_agy_quota_summary_filters_model_family(monkeypatch):
    summary = {
        "response": {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-5h",
                            "remaining": {"remainingFraction": 0.72},
                            "resetTime": "2026-08-05T22:00:00Z",
                        },
                        {
                            "bucketId": "gemini-weekly",
                            "remainingFraction": 0.61,
                            "resetTime": "2026-08-10T22:00:00Z",
                        },
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {
                            "bucketId": "3p-5h",
                            "remaining": {"case": "remainingFraction", "value": 0.44},
                            "resetTime": "2026-08-05T23:00:00Z",
                        },
                        {
                            "bucketId": "3p-weekly",
                            "remainingFraction": 0.37,
                            "resetTime": "2026-08-11T23:00:00Z",
                        },
                    ],
                },
            ]
        }
    }
    monkeypatch.setattr(agy, "_binary", lambda: "/fake/agy")
    monkeypatch.setattr(agy, "_fetch_local_summary", lambda binary: summary)

    gemini_result = agy.fetch(model="gemini-3.1-pro-high")
    third_party_result = agy.fetch(model="claude-sonnet-5")
    conservative_result = agy.fetch(model=None)

    assert gemini_result.ok and gemini_result.remaining_pct == 61.0
    assert third_party_result.ok and third_party_result.remaining_pct == 37.0
    assert conservative_result.ok and conservative_result.remaining_pct == 37.0
    assert third_party_result.resets_at is not None


def test_agy_proto_zero_and_loopback_port_parsing():
    summary = {
        "groups": [
            {
                "displayName": "Claude and GPT models",
                "buckets": [{"bucketId": "3p-weekly"}],
            }
        ]
    }
    assert agy._windows(summary, "gpt-5.6") == [(0.0, None)]
    ports = agy._parse_listening_ports(
        "agy 1 user 10u IPv4 TCP 127.0.0.1:64440 (LISTEN)\n"
        "agy 1 user 11u IPv6 TCP [::1]:64441 (LISTEN)\n"
    )
    assert ports == [64440, 64441]


def test_agy_transient_login_state_does_not_override_quota_api(monkeypatch):
    summary = {
        "groups": [
            {
                "displayName": "Claude and GPT models",
                "buckets": [{"bucketId": "3p-weekly", "remainingFraction": 0.5}],
            }
        ]
    }

    class FakeProcess:
        saw_login_prompt = True
        pid = 123

        def alive(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(agy, "_AgyPTYProcess", lambda binary: FakeProcess())
    monkeypatch.setattr(agy, "_lsof_ports", lambda pid: [64440])
    responses = iter([
        (200, {"error": {"message": "Not logged into Antigravity"}}),
        (200, summary),
    ])
    monkeypatch.setattr(agy, "_loopback_json", lambda port, method: next(responses))
    monkeypatch.setattr(agy.time, "sleep", lambda seconds: None)

    assert agy._fetch_local_summary("/fake/agy") == summary


def test_agy_missing_binary_is_unknown(monkeypatch):
    monkeypatch.setattr(agy, "_binary", lambda: None)
    r = agy.fetch(model=None)
    assert r.vendor == "agy" and not r.ok
    assert normalize_quota_vendor("Antigravity") == "agy"


def test_grok_modern_billing(monkeypatch):
    monkeypatch.setattr(grok, "_binary", lambda: "/fake/grok")
    monkeypatch.setattr(grok, "_auth_available", lambda: True)
    monkeypatch.setattr(
        grok,
        "_fetch_billing_document",
        lambda binary: {
            "subscription_tier": "supergrok",
            "config": {
                "creditUsagePercent": 37.4,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "end": "2026-08-12T00:00:00Z",
                },
            },
        },
    )
    r = grok.fetch()
    assert r.ok and r.remaining_pct == 62.6
    assert r.resets_at is not None
    assert "supergrok" in r.detail


def test_grok_acp_transport(tmp_path):
    fake = tmp_path / "fake-grok"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if request['method'] == 'initialize':\n"
        "        result = {'protocolVersion': '1', 'agentCapabilities': {}, 'authMethods': []}\n"
        "    elif request['method'] == '_x.ai/billing':\n"
        "        result = {'subscription_tier': 'supergrok', 'config': "
        "{'creditUsagePercent': 20, 'currentPeriod': "
        "{'type': 'USAGE_PERIOD_TYPE_WEEKLY', 'end': '2026-08-12T00:00:00Z'}}}\n"
        "    else:\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], "
        "'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)\n"
        "        continue\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)\n"
    )
    fake.chmod(0o755)
    document = grok._fetch_billing_document(str(fake))
    assert document["subscription_tier"] == "supergrok"
    assert document["config"]["creditUsagePercent"] == 20


def test_grok_legacy_billing_shape_and_alias():
    rem, reset, plan = grok._remaining_from_document(
        {
            "billingCycle": {"billingPeriodEnd": "2026-09-01T00:00:00Z"},
            "monthlyLimit": {"val": 1000},
            "usage": {"totalUsed": {"val": 250}},
        }
    )
    assert rem == 75.0
    assert reset == "2026-09-01T00:00:00Z"
    assert plan == ""
    assert normalize_quota_vendor("xAI") == "grok"


def test_grok_missing_auth_is_unknown(monkeypatch):
    monkeypatch.setattr(grok, "_binary", lambda: "/fake/grok")
    monkeypatch.setattr(grok, "_auth_available", lambda: False)
    r = grok.fetch()
    assert r.vendor == "grok" and not r.ok
    assert r.error is not None and "auth.json" in r.error


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



def _clear_cursor_env(monkeypatch):
    for env in ("AUTODEV_CURSOR_ACCESS_TOKEN", "MANA_CURSOR_ACCESS_TOKEN"):
        monkeypatch.delenv(env, raising=False)


def _write_auth(tmp_path, body):
    (tmp_path / "cursor").mkdir(exist_ok=True)
    (tmp_path / "cursor" / "auth.json").write_text(body)


def test_cursor_token_linux_reads_auth_file(monkeypatch, tmp_path):
    """linux: cursor-agent stores the JWT in $XDG_CONFIG_HOME/cursor/auth.json; no Keychain call."""
    _clear_cursor_env(monkeypatch)
    monkeypatch.setattr(cursor, "_host_os", lambda: "linux")
    monkeypatch.setattr(cursor, "keychain_password", lambda *a, **k: pytest.fail("keychain on linux"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_auth(tmp_path, '{"accessToken": " a.b.c ", "refreshToken": "x.y.z"}')
    assert cursor._token() == "a.b.c"


def test_cursor_token_darwin_reads_keychain_not_file(monkeypatch, tmp_path):
    _clear_cursor_env(monkeypatch)
    monkeypatch.setattr(cursor, "_host_os", lambda: "darwin")
    monkeypatch.setattr(cursor, "keychain_password", lambda *a, **k: "kc.tok.en")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _write_auth(tmp_path, '{"accessToken": "file.tok.en"}')
    assert cursor._token() == "kc.tok.en"


def test_cursor_token_env_wins_on_every_os(monkeypatch):
    monkeypatch.setenv("AUTODEV_CURSOR_ACCESS_TOKEN", "env.tok.en")
    for host in ("linux", "darwin", "win32"):
        monkeypatch.setattr(cursor, "_host_os", lambda h=host: h)
        assert cursor._token() == "env.tok.en"


def test_cursor_token_other_os_is_env_only(monkeypatch):
    _clear_cursor_env(monkeypatch)
    monkeypatch.setattr(cursor, "_host_os", lambda: "win32")
    assert cursor._token() is None


def test_cursor_token_linux_missing_or_malformed_auth_file(monkeypatch, tmp_path):
    _clear_cursor_env(monkeypatch)
    monkeypatch.setattr(cursor, "_host_os", lambda: "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cursor._token() is None                      # no file
    _write_auth(tmp_path, "not json")
    assert cursor._token() is None                      # unreadable
    _write_auth(tmp_path, '{"accessToken": 5}')
    assert cursor._token() is None                      # wrong type
