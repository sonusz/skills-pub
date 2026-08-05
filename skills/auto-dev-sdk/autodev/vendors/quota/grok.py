"""Grok remaining-quota fetcher via the official CLI's ACP billing method.

The Grok Build CLI exposes subscription usage over newline-delimited JSON-RPC.
Following Mana's current integration, this module starts
``grok agent --no-leader stdio``, initializes ACP v1, then calls the custom
``_x.ai/billing`` method.  No model request is issued.
"""
from __future__ import annotations

import json
import math
import os
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

from autodev.vendors.quota.base import QuotaResult, now_utc, parse_iso8601

MAX_PROTOCOL_LINE_BYTES = 1024 * 1024
INITIALIZE_TIMEOUT_SEC = 4
BILLING_TIMEOUT_SEC = 12


class _GrokRPCError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


def _binary() -> str | None:
    for key in (
        "AUTODEV_VENDOR_BIN_GROK",
        "AUTODEV_GROK_PATH",
        "MANA_GROK_PATH",
        "GROK_CLI_PATH",
    ):
        value = os.environ.get(key)
        if value and os.access(value, os.X_OK):
            return str(Path(value).resolve())
    found = shutil.which("grok")
    if found:
        return str(Path(found).resolve())
    for candidate in (
        "~/.grok/bin/grok",
        "~/.local/bin/grok",
        "/opt/homebrew/bin/grok",
        "/usr/local/bin/grok",
    ):
        path = Path(candidate).expanduser()
        if os.access(path, os.X_OK):
            return str(path.resolve())
    return None


def _auth_path() -> Path:
    configured_home = os.environ.get("GROK_HOME")
    root = Path(configured_home).expanduser() if configured_home else Path("~/.grok").expanduser()
    return root / "auth.json"


def _auth_available() -> bool:
    return os.access(_auth_path(), os.R_OK)


def _child_env() -> dict[str, str]:
    allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
    env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    env["NO_COLOR"] = "1"
    return env


def _write_request(proc: subprocess.Popen, request_id: int, method: str, params: dict) -> None:
    if proc.stdin is None:
        raise _GrokRPCError("grok ACP stdin unavailable")
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    try:
        proc.stdin.write((encoded + "\n").encode("utf-8"))
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise _GrokRPCError("grok ACP input closed") from exc


def _read_response(
    proc: subprocess.Popen,
    request_id: int,
    timeout: float,
    pending: bytearray,
) -> dict:
    if proc.stdout is None:
        raise _GrokRPCError("grok ACP stdout unavailable")
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    while True:
        while b"\n" in pending:
            raw, _, rest = pending.partition(b"\n")
            pending[:] = rest
            if not raw:
                continue
            if len(raw) > MAX_PROTOCOL_LINE_BYTES:
                raise _GrokRPCError("grok ACP response line too large")
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise _GrokRPCError("malformed grok ACP response") from exc
            if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0":
                raise _GrokRPCError("malformed grok ACP envelope")
            if "id" not in envelope:  # notification
                continue
            try:
                response_id = int(envelope["id"])
            except (TypeError, ValueError):
                raise _GrokRPCError("invalid grok ACP response id")
            if response_id != request_id:
                continue
            error = envelope.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "grok ACP request failed")
                code = error.get("code")
                try:
                    code = int(code) if code is not None else None
                except (TypeError, ValueError):
                    code = None
                if "authentication required" in message.lower():
                    message = "grok login required"
                raise _GrokRPCError(message, code=code)
            result = envelope.get("result")
            if not isinstance(result, dict):
                raise _GrokRPCError("grok ACP response missing result")
            return result

        if len(pending) > MAX_PROTOCOL_LINE_BYTES:
            raise _GrokRPCError("grok ACP response line too large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _GrokRPCError("grok ACP request timed out")
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            raise _GrokRPCError("grok ACP request timed out")
        try:
            chunk = os.read(fd, 64 * 1024)
        except OSError as exc:
            raise _GrokRPCError("grok ACP output read failed") from exc
        if not chunk:
            raise _GrokRPCError(
                f"grok ACP exited before response (status={proc.poll()})"
            )
        pending.extend(chunk)


def _close_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _valid_initialize(result: dict) -> bool:
    version = result.get("protocolVersion")
    valid_version = version == "1" or (
        isinstance(version, (int, float)) and not isinstance(version, bool) and version == 1
    )
    return (
        valid_version
        and isinstance(result.get("agentCapabilities"), dict)
        and isinstance(result.get("authMethods"), list)
    )


def _fetch_billing_document(binary: str) -> dict:
    try:
        proc = subprocess.Popen(
            [binary, "agent", "--no-leader", "stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_child_env(),
            start_new_session=True,
        )
    except OSError as exc:
        raise _GrokRPCError("could not start grok ACP") from exc

    pending = bytearray()
    try:
        _write_request(
            proc,
            1,
            "initialize",
            {
                "protocolVersion": "1",
                "clientCapabilities": {},
                "clientInfo": {"name": "auto-dev-sdk", "version": "2"},
            },
        )
        initialized = _read_response(proc, 1, INITIALIZE_TIMEOUT_SEC, pending)
        if not _valid_initialize(initialized):
            raise _GrokRPCError("unsupported grok ACP initialize response")

        _write_request(proc, 2, "_x.ai/billing", {})
        try:
            return _read_response(proc, 2, BILLING_TIMEOUT_SEC, pending)
        except _GrokRPCError as exc:
            # Older Grok builds exposed the unprefixed custom method.  The
            # current CLI requires the wire-level underscore used above.
            if exc.code != -32601:
                raise
            _write_request(proc, 3, "x.ai/billing", {})
            return _read_response(proc, 3, BILLING_TIMEOUT_SEC, pending)
    finally:
        _close_process(proc)


def _number(value) -> float | None:
    if isinstance(value, dict):
        value = value.get("val")
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _remaining_from_document(document: dict) -> tuple[float | None, object, str]:
    """Return (remaining percent, reset value, plan detail).

    Supports Mana's current subscription/config shape and the earlier
    CodexBar billingCycle/monthlyLimit shape for CLI-version compatibility.
    """
    tier = document.get("subscription_tier")
    detail = str(tier) if isinstance(tier, str) else ""
    config = document.get("config")
    if isinstance(config, dict):
        period = config.get("currentPeriod")
        reset = period.get("end") if isinstance(period, dict) else None
        used = _number(config.get("creditUsagePercent"))
        if used is not None:
            return max(0.0, min(100.0, 100.0 - used)), reset, detail

        period_type = period.get("type") if isinstance(period, dict) else None
        limit = _number(config.get("monthlyLimit"))
        spent = _number(config.get("used"))
        if period_type == "USAGE_PERIOD_TYPE_MONTHLY" and limit and limit > 0 and spent is not None:
            rem = (limit - spent) / limit * 100.0
            return max(0.0, min(100.0, rem)), reset, detail
        if (
            isinstance(period, dict)
            and "creditUsagePercent" not in config
            and "monthlyLimit" not in config
            and "used" not in config
        ):
            return 100.0, reset, detail
        return None, reset, detail

    cycle = document.get("billingCycle")
    reset = cycle.get("billingPeriodEnd") if isinstance(cycle, dict) else None
    limit = _number(document.get("monthlyLimit"))
    usage = document.get("usage")
    spent = _number(usage.get("totalUsed")) if isinstance(usage, dict) else None
    if limit and limit > 0 and spent is not None:
        rem = (limit - spent) / limit * 100.0
        return max(0.0, min(100.0, rem)), reset, detail
    return None, reset, detail


def fetch(model: str | None = None) -> QuotaResult:
    del model  # Grok exposes one included-credit pool for the account.
    binary = _binary()
    if binary is None:
        return QuotaResult.unknown("grok", "grok executable not found")
    if not _auth_available():
        return QuotaResult.unknown("grok", "no readable ~/.grok/auth.json")
    try:
        document = _fetch_billing_document(binary)
    except _GrokRPCError as exc:
        return QuotaResult.unknown("grok", str(exc))
    except Exception as exc:
        return QuotaResult.unknown("grok", f"billing probe failed: {exc}")

    remaining, reset_value, plan = _remaining_from_document(document)
    if remaining is None:
        return QuotaResult.unknown("grok", "no usable quota in billing response")
    reset = parse_iso8601(reset_value)
    detail = f"{remaining:.0f}% remaining"
    if plan:
        detail += f" ({plan})"
    return QuotaResult(
        vendor="grok",
        remaining_pct=remaining,
        resets_at=reset,
        fetched_at=now_utc(),
        detail=detail,
    )
