"""Shared primitives for the per-vendor quota fetchers.

The fetchers reimplement, in stdlib Python, how the Mana app reads each
vendor's usage API (credential discovery → optional token refresh → HTTP →
parse a *remaining percent* and a *reset time* for the binding window).

No third-party deps: HTTP via ``urllib``, macOS Keychain via the ``security``
CLI. Every fetcher returns a :class:`QuotaResult`; on ANY failure it returns
``remaining_pct=None`` (unknown) rather than raising, so the resolver can apply
its fail-closed policy uniformly.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class QuotaResult:
    """Remaining quota for one vendor's *binding* (most-constrained) window.

    ``remaining_pct is None`` means "unknown" (no creds, endpoint error, parse
    failure) — the resolver treats unknown as insufficient (fail-closed).
    """

    vendor: str
    remaining_pct: float | None
    resets_at: datetime | None
    fetched_at: datetime
    detail: str = ""
    ok: bool = True
    error: str | None = None

    @classmethod
    def unknown(cls, vendor: str, error: str) -> "QuotaResult":
        return cls(
            vendor=vendor,
            remaining_pct=None,
            resets_at=None,
            fetched_at=now_utc(),
            ok=False,
            error=error,
        )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso8601(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # Trim over-long fractional seconds (Python only accepts up to 6 digits).
        m = re.match(r"^(.*\.\d{6})\d*([+-]\d{2}:\d{2})?$", raw)
        if not m:
            return None
        try:
            dt = datetime.fromisoformat(m.group(1) + (m.group(2) or "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def epoch_to_dt(value) -> datetime | None:
    """Unix epoch (seconds or milliseconds) → aware datetime. Mana discards
    sentinel reset times <= 1 day; callers can apply that rule themselves."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n > 1e11:  # milliseconds
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def http_json(url, *, method="GET", headers=None, body=None, timeout=10):
    """Perform an HTTP request and parse JSON.

    Returns ``(status_code, parsed_json_or_none)``. Returns ``(None, None)`` on
    a transport-level failure (DNS, connection, TLS, timeout). HTTP error
    statuses (401/4xx/5xx) come back with their code and any JSON body.
    """
    req = urllib.request.Request(url, method=method)
    for key, val in (headers or {}).items():
        req.add_header(key, val)
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", resp.getcode())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        status = exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
    return status, parsed


def keychain_password(service: str, account: str | None = None) -> str | None:
    """macOS Keychain generic-password lookup (`security find-generic-password`).
    Returns the secret, or None if unavailable / not on macOS."""
    args = ["/usr/bin/security", "find-generic-password", "-s", service]
    if account:
        args += ["-a", account]
    args += ["-w"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    secret = (proc.stdout or "").strip()
    return secret or None


def read_json_file(path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def min_remaining(windows):
    """Given an iterable of ``(remaining_pct, resets_at)`` pairs (remaining may be
    None), return ``(remaining_pct, resets_at)`` for the most-constrained window —
    i.e. the lowest remaining percent — mirroring Mana's "lowest remaining wins".
    Returns ``(None, None)`` if no usable window."""
    best = None
    for rem, reset in windows:
        if rem is None:
            continue
        if best is None or rem < best[0]:
            best = (float(rem), reset)
    return best if best is not None else (None, None)
