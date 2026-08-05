"""Agy / Antigravity remaining-quota fetcher.

Antigravity no longer exposes individual quota through the public Cloud Code
Assist endpoint.  The supported source is the loopback Connect server started
by the ``agy`` TUI.  We launch it under a PTY without a prompt (so no model call
or quota is consumed), discover its listening port, and read
``RetrieveUserQuotaSummary``.

The transport mirrors Mana and CodexBar.  It never scrapes TUI content; the PTY
is used only because ``agy`` requires a controlling terminal.
"""
from __future__ import annotations

import json
import os
import pty
import re
import shutil
import signal
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from autodev.vendors.quota.base import (
    QuotaResult,
    min_remaining,
    now_utc,
    parse_iso8601,
)

SERVICE_PATH = "/exa.language_server_pb.LanguageServerService"
QUOTA_METHOD = "RetrieveUserQuotaSummary"
_LOOPBACK_PORT_RE = re.compile(r"(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)")


class _AgyError(RuntimeError):
    pass


class _AgyPTYProcess:
    """Own one prompt-free ``agy`` process and drain its terminal output."""

    def __init__(self, binary: str):
        pid, master_fd = pty.fork()
        if pid == 0:  # child
            env = dict(os.environ)
            env["NO_COLOR"] = "1"
            try:
                os.execve(binary, [binary], env)
            except OSError:
                os._exit(127)

        self.pid = pid
        self.master_fd = master_fd
        self._closed = False
        self._login_prompt = threading.Event()
        self._drainer = threading.Thread(target=self._drain, daemon=True)
        self._drainer.start()

    @property
    def saw_login_prompt(self) -> bool:
        return self._login_prompt.is_set()

    def _drain(self) -> None:
        tail = bytearray()
        while True:
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            tail.extend(chunk)
            if len(tail) > 64 * 1024:
                del tail[: len(tail) - 64 * 1024]
            ascii_tail = bytes(b if b < 0x80 else 0x20 for b in tail).lower()
            # Agy can transiently print "not signed in" while refreshing an
            # existing session.  Only the login-method menu is authoritative.
            if b"select login method" in ascii_tail:
                self._login_prompt.set()

    def alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.pid, sig)
            except (OSError, ProcessLookupError):
                break
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                try:
                    waited, _ = os.waitpid(self.pid, os.WNOHANG)
                except ChildProcessError:
                    waited = self.pid
                if waited == self.pid:
                    break
                time.sleep(0.02)
            else:
                continue
            break
        try:
            os.waitpid(self.pid, 0)
        except (ChildProcessError, OSError):
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass


def _binary() -> str | None:
    for key in (
        "AUTODEV_VENDOR_BIN_AGY",
        "AUTODEV_AGY_PATH",
        "MANA_AGY_PATH",
        "ANTIGRAVITY_CLI_PATH",
    ):
        value = os.environ.get(key)
        if value and os.access(value, os.X_OK):
            return str(Path(value).resolve())
    found = shutil.which("agy")
    if found:
        return str(Path(found).resolve())
    for candidate in (
        "~/.local/bin/agy",
        "~/.antigravity/antigravity/bin/agy",
        "/opt/homebrew/bin/agy",
        "/usr/local/bin/agy",
    ):
        path = Path(candidate).expanduser()
        if os.access(path, os.X_OK):
            return str(path.resolve())
    return None


def _parse_listening_ports(output: str) -> list[int]:
    return sorted({int(match.group(1)) for match in _LOOPBACK_PORT_RE.finditer(output)})


def _lsof_ports(pid: int) -> list[int]:
    lsof = "/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else shutil.which("lsof")
    if not lsof:
        return []
    try:
        proc = subprocess.run(
            [lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return _parse_listening_ports(proc.stdout or "")


def _discover_ports(process: _AgyPTYProcess) -> list[int]:
    for _ in range(20):
        if process.saw_login_prompt:
            raise _AgyError("agy login required")
        ports = _lsof_ports(process.pid)
        if ports:
            return ports
        if not process.alive():
            raise _AgyError("agy exited before opening its quota server")
        time.sleep(0.3)
    raise _AgyError("timed out discovering agy quota server")


def _loopback_json(port: int, method: str) -> tuple[int | None, dict | None]:
    url = f"https://127.0.0.1:{port}{SERVICE_PATH}/{method}"
    request = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
    )
    # The certificate is generated by the local agy process.  Disabling CA
    # validation is restricted to the hard-coded 127.0.0.1 URL above.
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=5, context=context) as response:
            status = getattr(response, "status", response.getcode())
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except OSError:
            raw = b""
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None
    return status, parsed if isinstance(parsed, dict) else None


def _groups(parsed: dict) -> list[dict]:
    for candidate in (parsed.get("response"), parsed.get("summary"), parsed):
        if not isinstance(candidate, dict):
            continue
        groups = candidate.get("groups")
        if isinstance(groups, list):
            return [group for group in groups if isinstance(group, dict)]
    return []


def _model_family(model: str | None) -> str | None:
    raw = (model or "").lower()
    if "gemini" in raw:
        return "gemini"
    if any(token in raw for token in ("claude", "gpt", "openai")):
        return "third-party"
    return None


def _group_family(group: dict) -> str:
    name = str(group.get("displayName") or "").lower()
    bucket_ids = [
        str(bucket.get("bucketId") or "").lower()
        for bucket in group.get("buckets") or []
        if isinstance(bucket, dict)
    ]
    is_gemini = any("gemini" in bucket_id for bucket_id in bucket_ids) or (
        "gemini" in name and "claude" not in name
    )
    return "gemini" if is_gemini else "third-party"


def _remaining_fraction(bucket: dict) -> float | None:
    if "remainingFraction" in bucket:
        value = bucket.get("remainingFraction")
    else:
        remaining = bucket.get("remaining")
        if isinstance(remaining, dict):
            value = remaining.get("remainingFraction")
            if value is None and remaining.get("case") == "remainingFraction":
                value = remaining.get("value")
        else:
            # Proto3 JSON omits a scalar zero, so an otherwise valid bucket
            # without the field means fully consumed.
            value = 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _reset_time(value):
    reset = parse_iso8601(value)
    if reset is not None and reset.timestamp() <= 24 * 60 * 60:
        return None
    return reset


def _windows(parsed: dict, model: str | None):
    wanted = _model_family(model)
    windows = []
    for group in _groups(parsed):
        if wanted is not None and _group_family(group) != wanted:
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("disabled") is True:
                continue
            fraction = _remaining_fraction(bucket)
            if fraction is None:
                continue
            windows.append(
                (fraction * 100.0, _reset_time(bucket.get("resetTime")))
            )
    return windows


def _not_logged_in(parsed: dict | None) -> bool:
    if parsed is None:
        return False
    text = json.dumps(parsed, ensure_ascii=True).lower()
    return "not logged into antigravity" in text or "select login method" in text


def _fetch_local_summary(binary: str) -> dict:
    process = _AgyPTYProcess(binary)
    try:
        ports = _discover_ports(process)
        for _ in range(15):
            if process.saw_login_prompt:
                raise _AgyError("agy login required")
            for port in ports:
                status, parsed = _loopback_json(port, QUOTA_METHOD)
                if _not_logged_in(parsed):
                    raise _AgyError("agy login required")
                if status == 200 and parsed is not None and _windows(parsed, None):
                    return parsed
            if not process.alive():
                raise _AgyError("agy exited before quota became available")
            time.sleep(1)
        raise _AgyError("agy quota server did not become ready")
    finally:
        process.close()


def fetch(model: str | None = None) -> QuotaResult:
    binary = _binary()
    if binary is None:
        return QuotaResult.unknown("agy", "agy executable not found")
    try:
        parsed = _fetch_local_summary(binary)
    except _AgyError as exc:
        return QuotaResult.unknown("agy", str(exc))
    except Exception as exc:
        return QuotaResult.unknown("agy", f"quota probe failed: {exc}")

    rem, reset = min_remaining(_windows(parsed, model))
    if rem is None:
        family = _model_family(model) or "configured model"
        return QuotaResult.unknown("agy", f"no usable {family} quota bucket")
    return QuotaResult(
        vendor="agy",
        remaining_pct=rem,
        resets_at=reset,
        fetched_at=now_utc(),
        detail=f"{rem:.0f}% remaining",
    )
