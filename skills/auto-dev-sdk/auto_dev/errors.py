"""Typed errors used across the harness."""
from __future__ import annotations


class AutoDevError(Exception):
    """Base class."""


class LockConflict(AutoDevError):
    """Another process holds the feature lock."""


class StaleInputs(AutoDevError):
    """An input file's hash no longer matches what was recorded."""


class ConfigError(AutoDevError):
    """vendors.yml / config shape wrong."""


class SchemaError(AutoDevError):
    """Artifact failed schema validation."""


class PermissionDenied(AutoDevError):
    """Tool executor refused a command per allow/deny rules."""


class VendorProtocolError(AutoDevError):
    """Vendor returned a response that can't be coerced to the unified shape."""


class GatePending(AutoDevError):
    """A gate requires user action before the pipeline can continue."""

    def __init__(self, gate: str, detail: str = ""):
        super().__init__(f"gate pending: {gate} — {detail}")
        self.gate = gate
        self.detail = detail


class PipelineError(AutoDevError):
    """Subagent returned an error or deviation that halts the pipeline."""
