#!/usr/bin/env bash
# Real LLM smoke for the auto-dev-sdk shared-vendors adapter.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="claude"
MODEL=""
TIMEOUT="120"

usage() {
  cat <<'USAGE'
Usage:
  scripts/llm-test.sh [--vendor claude|openai|codex|gemini] [--model MODEL] [--timeout SECONDS]

Runs one real model call through auto-dev-sdk's Python adapter and the packaged
shared/vendors module. Success means the call returned any non-empty output.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --vendor)
      VENDOR="${2:?--vendor requires a value}"
      shift 2
      ;;
    --vendor=*)
      VENDOR="${1#*=}"
      shift
      ;;
    --model)
      MODEL="${2:?--model requires a value}"
      shift 2
      ;;
    --model=*)
      MODEL="${1#*=}"
      shift
      ;;
    --timeout)
      TIMEOUT="${2:?--timeout requires a value}"
      shift 2
      ;;
    --timeout=*)
      TIMEOUT="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "llm-test.sh: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "Python not found: set PYTHON=/path/to/python" >&2
  exit 1
fi

PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" - "$VENDOR" "$MODEL" "$TIMEOUT" <<'PY'
from __future__ import annotations

import sys

from autodev.vendors.shared_call import call_shared_vendor


vendor, model, timeout = sys.argv[1], sys.argv[2], int(sys.argv[3])


def read_only_native_args(name: str) -> tuple[str, ...]:
    normalized = name.strip().lower()
    if normalized in {"openai", "codex", "gpt"}:
        return ("--sandbox", "read-only")
    if normalized in {"claude", "anthropic"}:
        return ("--allowedTools", "Read")
    if normalized in {"gemini", "google"}:
        return ("--approval-mode", "plan")
    return ()


result = call_shared_vendor(
    vendor=vendor,
    model=model or None,
    prompt=(
        "Who are you? Reply with one short sentence. "
        "This is an auto-dev-sdk shared-vendors integration smoke test."
    ),
    output_id="llm-test",
    timeout_sec=timeout,
    native_args=read_only_native_args(vendor),
)

if result.returncode != 0:
    detail = (result.log or result.summary_stderr or result.summary_stdout)[-1000:]
    print(f"llm-test failed: vendor={vendor} returncode={result.returncode}", file=sys.stderr)
    if detail:
        print(detail, file=sys.stderr)
    raise SystemExit(1)

output = result.output.strip()
if not output:
    print(f"llm-test failed: vendor={vendor} returned empty output", file=sys.stderr)
    raise SystemExit(1)

print(f"vendor={vendor}")
print(output)
PY
