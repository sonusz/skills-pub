"""Per-vendor flag allowlist (R3 / R2a / Codex round-4 blocker fix).

`vendors.yml.flags` entries are limited to a per-vendor-per-stage allowlist
of monotone-restrictive options. Anything else rejected at load time.

Write-scope flags (`--allowedTools`, `--add-dir`) are harness-owned and
NOT configurable via vendors.yml. vendors.yml flags can only FURTHER
restrict via `--disallowedTools`, or tweak other benign options.
"""
from __future__ import annotations

from autodev.errors import VendorNotAllowed


# Per-vendor allowlist of flags vendors.yml may add.
# Format: tuple of flag prefixes / exact flags. A flag matches if it starts
# with one of these strings (or equals it exactly for valueless flags).
VENDOR_ALLOWED_FLAGS: dict[str, tuple[str, ...]] = {
    "claude": (
        "--disallowedTools",       # further restrictions only
        "--disallowed-tools",
        "--effort",                # min/low/medium/high/xhigh/max — behavior tuning
        "--max-budget-usd",        # cost cap
        "--model",                 # allow explicit CLI-level model override
    ),
    "codex": (
        "--disable",               # feature flags off
        "-c",                      # config override (monotone if used for restrictions)
        "--oss",                   # OSS provider
        "--image",
    ),
    "cursor": (
        "--model",                 # explicit model SKU override (effort encoded in id)
        "--effort",                # accepted for uniformity (no cursor analog; ignored)
        "--max-budget-usd",        # cost cap
    ),
    "grok": (
        "--model",                 # explicit model override
        "--effort",                # shared effort scale maps to Grok reasoning effort
    ),
}
VENDOR_ALLOWED_FLAGS["openai"] = VENDOR_ALLOWED_FLAGS["codex"]


def validate_flags(*, vendor: str, stage: str, flags: tuple[str, ...]) -> None:
    """Raise VendorNotAllowed if any flag isn't in the vendor's allowlist.

    Walks `["--effort", "high"]` style pairs: flag names (entries starting
    with `-`) checked against allowlist; following non-flag entries are
    values and pass through. Widening attempts like `--allowedTools
    Bash,Network` fail because `--allowedTools` is harness-owned.
    """
    vendor = vendor.lower()
    allowed = VENDOR_ALLOWED_FLAGS.get(vendor)
    if allowed is None:
        raise VendorNotAllowed(f"vendor {vendor!r} has no flag allowlist defined")
    flags_list = list(flags)
    i = 0
    while i < len(flags_list):
        entry = flags_list[i]
        if entry.startswith("-"):
            name = entry.split("=", 1)[0]
            if not any(name == a or name.startswith(a + "=") for a in allowed):
                raise VendorNotAllowed(
                    f"vendors.yml stages.{stage}.flags: {entry!r} not in allowlist for "
                    f"vendor {vendor!r}. Allowed prefixes: {allowed}. "
                    f"Write-scope flags (--allowedTools, --add-dir) are harness-owned."
                )
            # Consume value if flag was `--foo` without `=` and next is not a flag.
            if "=" not in entry and i + 1 < len(flags_list) \
                    and not flags_list[i + 1].startswith("-"):
                i += 2
                continue
            i += 1
        else:
            # Orphan value — pass through (unusual).
            i += 1
