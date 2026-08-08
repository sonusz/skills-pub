"""Operator controls for persistent shared-vendor sessions."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from autodev.vendors.session_keys import feature_session_key


SDK_ROOT = Path(__file__).resolve().parents[2]
SESSION_HELPER = SDK_ROOT / "shared" / "vendors" / "scripts" / "session-state.py"
PERSISTENT_AGENT_ROLES = ("design", "build", "ralph-review")


def shared_session_state_dir() -> Path:
    """Mirror shared/vendors' documented runtime-state precedence."""

    configured = os.environ.get("VENDORS_SESSION_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state).expanduser() / "shared-vendors" / "sessions"
    user_home = os.environ.get("HOME")
    if user_home:
        return Path(user_home).expanduser() / ".local" / "state" / "shared-vendors" / "sessions"
    return Path(tempfile.gettempdir()) / f"shared-vendors-{os.getuid()}" / "sessions"


def reset_feature_session(feature_active: Path, role: str) -> int:
    """Reset all native mappings for one feature-scoped agent role."""

    if role not in PERSISTENT_AGENT_ROLES:
        raise ValueError(f"role does not have a persistent session: {role!r}")
    proc = subprocess.run(
        [
            sys.executable,
            str(SESSION_HELPER),
            "reset",
            "--state-dir",
            str(shared_session_state_dir()),
            "--key",
            feature_session_key(feature_active, role),
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown reset failure"
        raise RuntimeError(detail)
    try:
        return int(proc.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("session reset helper returned an invalid result") from exc
