"""Gemini (Cloud Code Assist) remaining-quota fetcher.

Ported from Mana's GeminiClient.swift: read ~/.gemini OAuth creds (refresh if
expired, using the client id/secret embedded in the gemini-cli install), resolve
the project, POST retrieveUserQuota, and take the most-constrained window for the
model's family (Pro / Flash / Flash-lite).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from autodev.state.atomic import atomic_write
from autodev.vendors.quota.base import (
    QuotaResult,
    epoch_to_dt,
    http_json,
    min_remaining,
    now_utc,
    parse_iso8601,
    read_json_file,
)

QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
LOADCODE_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
TOKEN_URL = "https://oauth2.googleapis.com/token"

_CLI_ROOTS = (
    "/opt/homebrew/lib/node_modules/@google/gemini-cli",
    "/usr/local/lib/node_modules/@google/gemini-cli",
    "~/.npm-global/lib/node_modules/@google/gemini-cli",
)
_CLIENT_ID_RE = re.compile(r"OAUTH_CLIENT_ID\s*[:=]\s*[\"']([^\"']+)[\"']")
_CLIENT_SECRET_RE = re.compile(r"OAUTH_CLIENT_SECRET\s*[:=]\s*[\"']([^\"']+)[\"']")


def _creds_path() -> Path:
    return Path(os.path.expanduser("~/.gemini/oauth_creds.json"))


def _settings_path() -> Path:
    return Path(os.path.expanduser("~/.gemini/settings.json"))


def _is_expired(creds: dict) -> bool:
    exp = creds.get("expiry_date")
    if not exp:
        return False
    try:
        exp_f = float(exp)
    except (TypeError, ValueError):
        return False
    exp_s = exp_f / 1000.0 if exp_f > 1e11 else exp_f
    return time.time() >= exp_s


def _scan_client_creds() -> tuple[str | None, str | None]:
    for root in _CLI_ROOTS:
        base = Path(os.path.expanduser(root))
        if not base.exists():
            continue
        for sub in ("dist", "bundle", "."):
            d = base / sub
            if not d.exists():
                continue
            for js in d.rglob("*.js"):
                try:
                    text = js.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                cid = _CLIENT_ID_RE.search(text)
                csec = _CLIENT_SECRET_RE.search(text)
                if cid and csec:
                    return cid.group(1), csec.group(1)
    return None, None


def _refresh(creds: dict) -> dict | None:
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None
    client_id, client_secret = _scan_client_creds()
    if not client_id or not client_secret:
        return None
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )
    status, parsed = http_json(
        TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    if status != 200 or not isinstance(parsed, dict):
        return None
    new_access = parsed.get("access_token")
    if not new_access:
        return None
    merged = dict(creds)
    merged["access_token"] = new_access
    if parsed.get("refresh_token"):
        merged["refresh_token"] = parsed["refresh_token"]
    if parsed.get("expires_in"):
        merged["expiry_date"] = int((time.time() + float(parsed["expires_in"])) * 1000)
    try:
        atomic_write(_creds_path(), json.dumps(merged))
    except OSError:
        pass
    return merged


def _project(token: str) -> str | None:
    settings = read_json_file(_settings_path())
    if settings:
        proj = settings.get("cloudProject") or settings.get("project")
        if isinstance(proj, str) and proj:
            return proj
    status, parsed = http_json(
        LOADCODE_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        body="{}",
    )
    if status == 200 and isinstance(parsed, dict):
        proj = parsed.get("cloudaicompanionProject")
        if isinstance(proj, str) and proj:
            return proj
    return None


def _family(name: str | None) -> str | None:
    if not name:
        return None
    low = name.lower()
    if "flash-lite" in low:
        return "flash-lite"
    if "flash" in low:
        return "flash"
    if "pro" in low:
        return "pro"
    return None


def _reset_from(value):
    dt = parse_iso8601(value)
    if dt is None:
        dt = epoch_to_dt(value)
    # Mana discards near-epoch sentinels (<= 1 day, e.g. 1970-01-01) that Gemini
    # returns for an exhausted bucket with no real reset time.
    if dt is not None and dt.timestamp() <= 24 * 60 * 60:
        return None
    return dt


def _windows(parsed: dict, want_family: str | None):
    out = []
    models = parsed.get("models")
    if isinstance(models, list):
        for mdl in models:
            if not isinstance(mdl, dict):
                continue
            fam = _family(mdl.get("name"))
            if want_family and fam and fam != want_family:
                continue
            for win in mdl.get("windows") or []:
                if not isinstance(win, dict):
                    continue
                util = win.get("utilization")
                if util is None:
                    continue
                try:
                    out.append((max(0.0, 100.0 - float(util)), _reset_from(win.get("resets_at"))))
                except (TypeError, ValueError):
                    continue
    buckets = parsed.get("buckets")
    if isinstance(buckets, list):
        for bkt in buckets:
            if not isinstance(bkt, dict):
                continue
            fam = _family(bkt.get("modelId"))
            if want_family and fam and fam != want_family:
                continue
            frac = bkt.get("remainingFraction")
            if frac is None:
                continue
            try:
                rem = max(0.0, min(1.0, float(frac))) * 100.0
            except (TypeError, ValueError):
                continue
            out.append((rem, _reset_from(bkt.get("resetTime"))))
    return out


def fetch(model: str | None = None) -> QuotaResult:
    creds = read_json_file(_creds_path())
    if creds is None:
        return QuotaResult.unknown("gemini", "no ~/.gemini/oauth_creds.json")
    if _is_expired(creds):
        refreshed = _refresh(creds)
        if refreshed is not None:
            creds = refreshed
    token = creds.get("access_token")
    if not token:
        return QuotaResult.unknown("gemini", "no access_token in creds")

    project = _project(token)
    body = json.dumps({"project": project}) if project else "{}"
    status, parsed = http_json(
        QUOTA_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        body=body,
    )
    if status != 200 or not isinstance(parsed, dict):
        return QuotaResult.unknown("gemini", f"retrieveUserQuota status={status}")

    want = _family(model)
    windows = _windows(parsed, want)
    if not windows and want is not None:
        windows = _windows(parsed, None)  # fall back to all families
    rem, reset = min_remaining(windows)
    if rem is None:
        return QuotaResult.unknown("gemini", "no usable quota window in response")
    return QuotaResult(
        vendor="gemini",
        remaining_pct=rem,
        resets_at=reset,
        fetched_at=now_utc(),
        detail=f"{rem:.0f}% remaining",
    )
