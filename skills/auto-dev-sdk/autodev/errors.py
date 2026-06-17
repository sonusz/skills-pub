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


class StageOutputInvalid(PreflightError):
    """A stage subprocess exited 0 but produced a deficient artifact
    (missing expected artifact, malformed provenance header, incomplete
    classification coverage, unparseable JSON, …).

    Distinct from a hard subprocess failure or a containment violation:
    the agent did real work, the deliverable is just incomplete or
    malformed, so re-dispatching the SAME agent with the specific
    deficiency described — and the prior artifact handed back for
    in-place amendment — is a sound recovery. The orchestrator retries
    on this up to a bounded cap before letting it propagate.

    Subclasses ``PreflightError`` so callers that already catch
    ``PreflightError`` (e.g. the CLI dispatcher) keep treating an
    *exhausted* retry as a normal hard error.
    """

    def __init__(self, stage: str, kind: str, detail: str = ""):
        super().__init__(f"stage {stage} output invalid ({kind}): {detail}")
        self.stage = stage
        self.kind = kind
        self.detail = detail


class GatePending(AutodevError):
    """A mandatory gate requires user action."""

    def __init__(self, gate: str, detail: str = ""):
        super().__init__(f"gate pending: {gate} — {detail}")
        self.gate = gate
        self.detail = detail


class QuotaHalt(AutodevError):
    """Every candidate LLM for a role is below its minimum remaining-quota
    requirement (fail-closed: unfetchable quota counts as insufficient).

    Raised by the fallback resolver *before* a vendor subprocess launches, so
    the run can pause cleanly. ``resume_at`` is the earliest time any skipped
    candidate is expected to recover (``None`` if no candidate exposed a reset);
    ``diagnostics`` is a per-candidate record of what was seen (vendor, model,
    remaining_pct, min required, resets_at, error) for the pause record + logs.
    """

    def __init__(
        self,
        role: str,
        diagnostics: list[dict],
        resume_at=None,  # datetime | None
    ):
        super().__init__(
            f"quota halt for {role}: no candidate meets its min remaining quota"
        )
        self.role = role
        self.diagnostics = diagnostics
        self.resume_at = resume_at


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
