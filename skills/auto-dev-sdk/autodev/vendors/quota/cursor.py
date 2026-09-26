"""Cursor remaining-quota fetcher.

Ported from Mana's CursorClient.swift: a session JWT becomes a
``WorkosCursorSessionToken`` cookie; GET the usage-summary endpoint and take the
most-constrained of the plan / on-demand buckets. Cursor has no token refresh.

Token source: the env overrides win on every OS; otherwise the caller branches
on the host OS (``_host_os``, the one shared/os definition) and reads that OS's
store — darwin: the Keychain item the Cursor apps write; linux: the file
``cursor-agent login`` writes, ``$XDG_CONFIG_HOME/cursor/auth.json`` (default
``~/.config/cursor/auth.json``), key ``accessToken``. Any other OS: env only.
"""
from __future__ import annotations

import base64
import json
import os

from pathlib import Path

from autodev.vendors.quota.base import (
    QuotaResult,
    http_json,
    keychain_password,
    min_remaining,
    now_utc,
    parse_iso8601,
    read_json_file,
)
from autodev.state.hostos import _host_os

USAGE_URL = "https://cursor.com/api/usage-summary"
KEYCHAIN_SERVICE = "cursor-access-token"
KEYCHAIN_ACCOUNT = "cursor-user"


def _linux_auth_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "cursor" / "auth.json"


def _darwin_token() -> str | None:
    return keychain_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)


def _linux_token() -> str | None:
    obj = read_json_file(_linux_auth_file())
    val = obj.get("accessToken") if obj else None
    return val.strip() if isinstance(val, str) and val.strip() else None


def _token() -> str | None:
    for env in ("AUTODEV_CURSOR_ACCESS_TOKEN", "MANA_CURSOR_ACCESS_TOKEN"):
        val = os.environ.get(env)
        if val and val.strip():
            return val.strip()
    host = _host_os()
    if host == "darwin":
        return _darwin_token()
    if host == "linux":
        return _linux_token()
    return None


def _jwt_sub(jwt: str) -> str | None:
    parts = jwt.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # pad base64url
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        obj = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    sub = obj.get("sub") if isinstance(obj, dict) else None
    return sub if isinstance(sub, str) and sub else None


def _plan_remaining(plan: dict):
    if not isinstance(plan, dict) or not plan.get("enabled"):
        return None, None
    used_pct = plan.get("totalPercentUsed")
    if used_pct is None:
        used_pct = plan.get("percentUsed")
    try:
        if used_pct is not None:
            return max(0.0, 100.0 - float(used_pct)), None
        limit = float(plan.get("limit") or 0)
        used = float(plan.get("used") or 0)
        if limit > 0:
            return max(0.0, 100.0 - used / limit * 100.0), None
    except (TypeError, ValueError):
        return None, None
    return None, None


def _ondemand_remaining(od: dict):
    if not isinstance(od, dict) or not od.get("enabled"):
        return None, None
    try:
        limit = float(od.get("limit") or 0)
        remaining = float(od.get("remaining") or 0)
        if limit > 0:
            return max(0.0, remaining / limit * 100.0), None
    except (TypeError, ValueError):
        return None, None
    return None, None


def fetch(model: str | None = None) -> QuotaResult:
    jwt = _token()
    if not jwt:
        return QuotaResult.unknown("cursor", "no Cursor access token found")
    sub = _jwt_sub(jwt)
    if not sub:
        return QuotaResult.unknown("cursor", "could not parse user id from JWT")

    cookie = f"WorkosCursorSessionToken={sub}%3A%3A{jwt}"
    status, parsed = http_json(
        USAGE_URL,
        headers={"Accept": "application/json", "Cookie": cookie},
    )
    if status != 200 or not isinstance(parsed, dict):
        return QuotaResult.unknown("cursor", f"usage-summary status={status}")

    reset = parse_iso8601(parsed.get("billingCycleEnd"))
    individual = parsed.get("individualUsage")
    individual = individual if isinstance(individual, dict) else {}
    windows = []
    p_rem, _ = _plan_remaining(individual.get("plan"))
    if p_rem is not None:
        windows.append((p_rem, reset))
    o_rem, _ = _ondemand_remaining(individual.get("onDemand"))
    if o_rem is not None:
        windows.append((o_rem, reset))

    rem, used_reset = min_remaining(windows)
    if rem is None:
        return QuotaResult.unknown("cursor", "no usable quota bucket in response")
    return QuotaResult(
        vendor="cursor",
        remaining_pct=rem,
        resets_at=used_reset,
        fetched_at=now_utc(),
        detail=f"{rem:.0f}% remaining",
    )
