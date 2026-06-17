"""Claude (Anthropic) remaining-quota fetcher.

Ported from Mana's ClaudeClient.swift: read the Claude Code OAuth credential
(file or Keychain), refresh if expired, then GET the OAuth usage endpoint and
take the most-constrained window's remaining percent.
"""
from __future__ import annotations

import os
import time
import urllib.parse
from pathlib import Path

from autodev.state.atomic import atomic_write
from autodev.vendors.quota.base import (
    QuotaResult,
    http_json,
    keychain_password,
    min_remaining,
    now_utc,
    parse_iso8601,
    read_json_file,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _creds_path() -> Path:
    return Path(os.path.expanduser("~/.claude/.credentials.json"))


def _load_raw() -> tuple[dict | None, str]:
    """Return (raw_json, source) where source is 'file'|'keychain'|''."""
    obj = read_json_file(_creds_path())
    if obj is not None:
        return obj, "file"
    secret = keychain_password(KEYCHAIN_SERVICE)
    if secret:
        import json

        try:
            obj = json.loads(secret)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            return obj, "keychain"
    return None, ""


def _oauth_block(raw: dict) -> dict:
    blk = raw.get("claudeAiOauth") or raw.get("oauth") or {}
    return blk if isinstance(blk, dict) else {}


def _access_token(raw: dict) -> str | None:
    blk = _oauth_block(raw)
    return blk.get("accessToken") or raw.get("access_token")


def _is_expired(raw: dict) -> bool:
    blk = _oauth_block(raw)
    exp = blk.get("expiresAt")
    if not exp:
        return False
    try:
        exp_f = float(exp)
    except (TypeError, ValueError):
        return False
    # expiresAt is epoch ms in Claude Code creds.
    exp_s = exp_f / 1000.0 if exp_f > 1e11 else exp_f
    return time.time() >= exp_s


def _refresh(raw: dict, source: str) -> dict | None:
    blk = _oauth_block(raw)
    refresh_token = blk.get("refreshToken") or raw.get("refresh_token")
    if not refresh_token:
        return None
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
    )
    status, parsed = http_json(
        REFRESH_URL,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        body=body,
    )
    if status != 200 or not isinstance(parsed, dict):
        return None
    new_access = parsed.get("access_token")
    new_refresh = parsed.get("refresh_token")
    if not new_access or not new_refresh:
        return None
    expires_in = parsed.get("expires_in") or 28800
    expires_at_ms = int((time.time() + float(expires_in)) * 1000)
    merged = dict(raw)
    target = "claudeAiOauth" if isinstance(raw.get("claudeAiOauth"), dict) else (
        "oauth" if isinstance(raw.get("oauth"), dict) else None
    )
    if target is not None:
        blk2 = dict(merged[target])
        blk2.update(
            accessToken=new_access, refreshToken=new_refresh, expiresAt=expires_at_ms
        )
        merged[target] = blk2
    else:
        merged["access_token"] = new_access
        merged["refresh_token"] = new_refresh
    if source == "file":
        import json

        try:
            atomic_write(_creds_path(), json.dumps(merged))
        except OSError:
            pass
    # Keychain write-back omitted: read access still works this run.
    return merged


def _windows(usage: dict):
    """Yield (remaining_pct, resets_at) for every quota window present."""
    out = []
    account = usage.get("account") if isinstance(usage.get("account"), dict) else {}

    def add_bucket(bucket):
        if not isinstance(bucket, dict):
            return
        util = bucket.get("utilization")
        if util is None:
            return
        try:
            rem = 100.0 - float(util)
        except (TypeError, ValueError):
            return
        out.append((max(0.0, rem), parse_iso8601(bucket.get("resets_at"))))

    add_bucket(account.get("five_hour") or usage.get("five_hour"))
    add_bucket(account.get("seven_day") or usage.get("seven_day"))
    models = usage.get("models") if isinstance(usage.get("models"), dict) else {}
    for mdl in models.values():
        if isinstance(mdl, dict):
            add_bucket(mdl.get("seven_day"))
    for key in ("seven_day_sonnet", "seven_day_opus", "seven_day_design"):
        add_bucket(usage.get(key))

    # extra_usage (paid overage budget): remaining = (limit-used)/limit*100.
    extra = usage.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        limit = extra.get("monthly_limit")
        used = extra.get("used_credits")
        try:
            if limit and float(limit) > 0 and used is not None:
                rem = max(0.0, float(limit) - float(used)) / float(limit) * 100.0
                out.append((rem, None))
        except (TypeError, ValueError):
            pass
    return out


def fetch(model: str | None = None) -> QuotaResult:
    raw, source = _load_raw()
    if raw is None:
        return QuotaResult.unknown("claude", "no Claude credentials found")
    if _is_expired(raw):
        refreshed = _refresh(raw, source)
        if refreshed is not None:
            raw = refreshed
    token = _access_token(raw)
    if not token:
        return QuotaResult.unknown("claude", "no access token in credentials")

    status, parsed = http_json(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    if status == 401:
        refreshed = _refresh(raw, source)
        if refreshed is not None:
            token = _access_token(refreshed)
            status, parsed = http_json(
                USAGE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                    "Accept": "application/json",
                },
            )
    if status != 200 or not isinstance(parsed, dict):
        return QuotaResult.unknown("claude", f"usage endpoint status={status}")

    rem, reset = min_remaining(_windows(parsed))
    if rem is None:
        return QuotaResult.unknown("claude", "no usable quota window in response")
    return QuotaResult(
        vendor="claude",
        remaining_pct=rem,
        resets_at=reset,
        fetched_at=now_utc(),
        detail=f"{rem:.0f}% remaining",
    )
