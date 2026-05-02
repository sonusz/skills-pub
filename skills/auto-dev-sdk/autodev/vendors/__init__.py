"""v2 vendor dispatch through the packaged shared/vendors adapter."""

from autodev.vendors.config import (
    VendorsConfig, StageSpec, ProbeConfig, load_vendors_config, STAGES,
    DEFAULT_TIMEOUT_SEC,
)
from autodev.vendors.allowlist import VENDOR_ALLOWED_FLAGS, validate_flags
from autodev.vendors.subprocess_runner import run_stage_subprocess

__all__ = [
    "VendorsConfig", "StageSpec", "ProbeConfig", "load_vendors_config", "STAGES",
    "DEFAULT_TIMEOUT_SEC",
    "VENDOR_ALLOWED_FLAGS", "validate_flags",
    "run_stage_subprocess",
]
