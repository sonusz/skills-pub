"""Codex / OpenAI (ChatGPT backend) remaining-quota fetcher.

Ported from Mana's CodexClient.swift: read ~/.codex/auth.json tokens (refresh if
stale), GET the wham usage endpoint, and take the most-constrained rate-limit
window. NOTE: the `credits` balance is binary (100 if balance>0 else 0) — coarse;
we rely on the percentage-based rate-limit windows for the remaining%.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from autodev.state.atomic import atomic_write
from autodev.vendors.quota.base import (
    QuotaResult,
    epoch_to_dt,
    http_json,
    min_remaining,
    now_utc,
    read_json_file,
)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_AFTER_SEC = 8 * 24 * 60 * 60  # Mana refreshes when last_refresh > 8 days


def _auth_path() -> Path:
    return Path(os.path.expanduser("~/.codex/auth.json"))


def _tokens(raw: dict) -> dict:
    tk = raw.get("tokens")
    return tk if isinstance(tk, dict) else {}


def _access_token(raw: dict) -> str | None:
    tk = _tokens(raw)
    return tk.get("access_token") or tk.get("accessToken") or raw.get("OPENAI_API_KEY")


def _stale(raw: dict) -> bool:
    last = raw.get("last_refresh")
    dt = None
    if isinstance(last, (int, float)):
        dt = epoch_to_dt(last)
    elif isinstance(last, str):
        from autodev.vendors.quota.base import parse_iso8601

        dt = parse_iso8601(last)
    if dt is None:
        return False
    return (time.time() - dt.timestamp()) > REFRESH_AFTER_SEC


def _refresh(raw: dict) -> dict | None:
    tk = _tokens(raw)
    refresh_token = tk.get("refresh_token") or tk.get("refreshToken")
    if not refresh_token:
        return None
    body = json.dumps(
        {
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        }
    )
    status, parsed = http_json(
        TOKEN_URL,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=body,
    )
    if status != 200 or not isinstance(parsed, dict):
        return None
    new_access = parsed.get("access_token")
    if not new_access:
        return None
    merged = dict(raw)
    new_tokens = dict(tk)
    new_tokens["access_token"] = new_access
    if parsed.get("refresh_token"):
        new_tokens["refresh_token"] = parsed["refresh_token"]
    if parsed.get("id_token"):
        new_tokens["id_token"] = parsed["id_token"]
    merged["tokens"] = new_tokens
    merged["last_refresh"] = now_utc().isoformat()
    try:
        atomic_write(_auth_path(), json.dumps(merged))
    except OSError:
        pass
    return merged


def _window(win: dict):
    if not isinstance(win, dict):
        return None
    used = win.get("used_percent")
    if used is None:
        return None
    try:
        rem = max(0.0, 100.0 - float(used))
    except (TypeError, ValueError):
        return None
    return rem, epoch_to_dt(win.get("reset_at"))


def _windows(parsed: dict):
    out = []
    rate = parsed.get("rate_limit")
    if isinstance(rate, dict):
        for key in ("primary_window", "secondary_window"):
            w = _window(rate.get(key))
            if w is not None:
                out.append(w)
    extra = parsed.get("additional_rate_limits")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, dict):
                for key in ("primary_window", "secondary_window", "window"):
                    w = _window(item.get(key))
                    if w is not None:
                        out.append(w)
    return out


def fetch(model: str | None = None) -> QuotaResult:
    raw = read_json_file(_auth_path())
    if raw is None:
        return QuotaResult.unknown("codex", "no ~/.codex/auth.json")
    if _stale(raw):
        refreshed = _refresh(raw)
        if refreshed is not None:
            raw = refreshed
    token = _access_token(raw)
    if not token:
        return QuotaResult.unknown("codex", "no access token in auth.json")

    status, parsed = http_json(
        USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if status == 401:
        refreshed = _refresh(raw)
        if refreshed is not None:
            token = _access_token(refreshed)
            status, parsed = http_json(
                USAGE_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    if status != 200 or not isinstance(parsed, dict):
        return QuotaResult.unknown("codex", f"wham usage status={status}")

    rem, reset = min_remaining(_windows(parsed))
    if rem is None:
        return QuotaResult.unknown("codex", "no rate-limit window in response")
    return QuotaResult(
        vendor="codex",
        remaining_pct=rem,
        resets_at=reset,
        fetched_at=now_utc(),
        detail=f"{rem:.0f}% remaining",
    )
