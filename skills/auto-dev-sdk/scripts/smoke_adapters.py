#!/usr/bin/env python3
"""Live-vendor smoke test for every adapter referenced in vendors.yml.

Per scope item ad-13: iterate each `{vendor, model}` in `vendors.yml`,
instantiate the adapter, issue a minimal `run_subagent` call, and verify
`SubagentResponse.structured` is a dict.

Design goals:
  * Runs OUTSIDE the pytest suite — pytest must stay offline.
  * Vendors without credentials are skipped with a clear stderr warning;
    that alone is exit 0.
  * Vendors that fail a real call exit non-zero (1 per failure, summed).
  * Fast: one prompt per vendor, no retries, per-call timeout.

Usage:
    scripts/smoke_adapters.py                   # uses ./vendors.yml
    scripts/smoke_adapters.py --vendors-yml X   # explicit config
    scripts/smoke_adapters.py --schema          # force tool_use path
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

from auto_dev.errors import AutoDevError
from auto_dev.vendors import get_adapter, load_vendors_config
from auto_dev.vendors.config import STAGES, DELEGATED_STAGES


# Per-vendor env var that must exist for the adapter to have a chance.
_API_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def _has_credentials(vendor: str) -> bool:
    keys = _API_KEY_ENV.get(vendor, ())
    if isinstance(keys, str):
        keys = (keys,)
    return any(os.environ.get(k) for k in keys)


def _probe_payload() -> dict:
    return {
        "stage_tag": "smoke",
        "question": "Return exactly {\"ok\": true}.",
    }


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-vendor adapter smoke test.")
    parser.add_argument("--vendors-yml", default="vendors.yml")
    parser.add_argument(
        "--schema", action="store_true",
        help="Pass response_schema so adapters with native structured output (e.g., Anthropic) exercise the tool_use path.",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=256,
        help="Per-call token ceiling; keep small for a smoke test.",
    )
    args = parser.parse_args()

    cfg_path = Path(args.vendors_yml)
    if not cfg_path.exists():
        print(f"ERROR: {cfg_path} not found", file=sys.stderr)
        return 1

    cfg = load_vendors_config(cfg_path)

    # De-dup (vendor, model) across stages so we don't hammer the same
    # combo repeatedly. Keep first-seen stage as label.
    seen: dict[tuple[str, str], str] = {}
    for stage in STAGES:
        if stage in DELEGATED_STAGES:
            continue
        spec = cfg.stages[stage]
        seen.setdefault((spec.vendor, spec.model), stage)

    failures = 0
    skips = 0
    successes = 0

    for (vendor, model), label_stage in seen.items():
        if not _has_credentials(vendor):
            env_hint = _API_KEY_ENV.get(vendor, "<unknown>")
            print(
                f"SKIP  {vendor}:{model} (stage={label_stage}) — "
                f"no API key in env ({env_hint})",
                file=sys.stderr,
            )
            skips += 1
            continue

        try:
            adapter = get_adapter(vendor)
        except AutoDevError as e:
            print(f"FAIL  {vendor}:{model} — adapter init failed: {e}", file=sys.stderr)
            failures += 1
            continue

        start = time.monotonic()
        try:
            response = adapter.run_subagent(
                model=model,
                system="You respond with a single minimal JSON object as requested.",
                inputs=_probe_payload(),
                response_schema=_schema() if args.schema else None,
                max_tokens=args.max_tokens,
            )
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - start
            print(f"FAIL  {vendor}:{model} [{elapsed:.1f}s] — {type(e).__name__}: {e}",
                  file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            failures += 1
            continue

        elapsed = time.monotonic() - start
        if not isinstance(response.structured, dict):
            print(f"FAIL  {vendor}:{model} [{elapsed:.1f}s] — structured is "
                  f"{type(response.structured).__name__}, not dict",
                  file=sys.stderr)
            failures += 1
            continue

        if "ok" not in response.structured:
            print(f"WARN  {vendor}:{model} [{elapsed:.1f}s] — structured missing 'ok' key; "
                  f"got keys {sorted(response.structured.keys())}",
                  file=sys.stderr)
        print(f"OK    {vendor}:{model} [{elapsed:.1f}s] — {response.structured}")
        successes += 1

    print(
        f"\n--- smoke summary: {successes} ok, {skips} skipped, {failures} failed ---",
        file=sys.stderr,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
