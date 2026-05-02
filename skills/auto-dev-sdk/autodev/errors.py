"""Typed errors for the harness."""
from __future__ import annotations


class AutodevError(Exception):
    """Base class."""


class LockConflict(AutodevError):
    """Another process holds the feature lock."""


class StaleInput(AutodevError):
    """Upstream artifact hash no longer matches what was recorded."""


class ConfigError(AutodevError):
    """vendors.yml or similar config is malformed."""


class SchemaError(AutodevError):
    """Artifact failed schema validation."""


class PreflightError(AutodevError):
    """Preflight check failed (e.g. non-git directory)."""


class GatePending(AutodevError):
    """A mandatory gate requires user action."""

    def __init__(self, gate: str, detail: str = ""):
        super().__init__(f"gate pending: {gate} — {detail}")
        self.gate = gate
        self.detail = detail


class GateFailed(AutodevError):
    """A gate was run but the verdict was not pass."""

    def __init__(self, gate: str, verdict: str, summary: str = ""):
        super().__init__(f"gate failed: {gate} verdict={verdict} — {summary}")
        self.gate = gate
        self.verdict = verdict


class DirtyWorkspace(AutodevError):
    """Workspace is dirty and no acknowledge-dirty override is active."""


class SubprocessFailure(AutodevError):
    """Vendor subprocess failed; inspect `failure_kind` and `failure_json_path`."""

    def __init__(self, failure_kind: str, detail: str, failure_json_path: str = ""):
        super().__init__(f"{failure_kind}: {detail}")
        self.failure_kind = failure_kind
        self.failure_json_path = failure_json_path


class VendorNotAllowed(AutodevError):
    """vendors.yml references a vendor / flag not in the allowlist."""
