#!/usr/bin/env bash
# Real vendor response test.
#
# This test intentionally calls the configured vendor CLIs through call.sh.
# It is separate from smoke-test.sh so fake-CLI regression tests stay free.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=vendor-launch.sh
. "$SCRIPT_DIR/vendor-launch.sh"

usage() {
  cat <<'USAGE'
Usage:
  scripts/hello-test.sh [options]

Options:
  --vendor NAME             openai, claude, or gemini; repeatable. Default: all
  --config FILE             Model mapping config passed to call.sh
  --effort min|low|medium|high|xhigh|max
                          Best-effort reasoning hint (default: min)
  --timeout SECONDS         Per-vendor timeout (default: 60)
  --output-dir DIR          Keep outputs in DIR. Default: temp dir kept and printed
  --prompt TEXT             Override probe prompt
  -h, --help                Show this help
USAGE
}

die() {
  printf "hello-test.sh: %s\n" "$*" >&2
  exit 2
}

require_value() {
  local opt="$1"
  local value="${2-}"

  if [ -z "$value" ]; then
    die "$opt requires a value"
  fi
}

REQUESTED_VENDORS=()
CONFIG_FILE=""
EFFORT="min"
TIMEOUT_SECONDS=60
OUTPUT_DIR=""
PROMPT="Who are you? Reply in one short sentence and include your model or vendor name if you know it."

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --vendor)
      require_value "$1" "${2-}"
      REQUESTED_VENDORS+=("$2")
      shift 2
      ;;
    --vendor=*)
      REQUESTED_VENDORS+=("${1#*=}")
      shift
      ;;
    --config)
      require_value "$1" "${2-}"
      CONFIG_FILE="$2"
      shift 2
      ;;
    --config=*)
      CONFIG_FILE="${1#*=}"
      shift
      ;;
    --effort)
      require_value "$1" "${2-}"
      EFFORT=$(vendors_lower "$2")
      shift 2
      ;;
    --effort=*)
      EFFORT="${1#*=}"
      EFFORT=$(vendors_lower "$EFFORT")
      shift
      ;;
    --timeout)
      require_value "$1" "${2-}"
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --timeout=*)
      TIMEOUT_SECONDS="${1#*=}"
      shift
      ;;
    --output-dir)
      require_value "$1" "${2-}"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --output-dir=*)
      OUTPUT_DIR="${1#*=}"
      shift
      ;;
    --prompt)
      require_value "$1" "${2-}"
      PROMPT="$2"
      shift 2
      ;;
    --prompt=*)
      PROMPT="${1#*=}"
      shift
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

case "$EFFORT" in
  *)
    EFFORT=$(vendors_normalize_effort "$EFFORT") \
      || die "--effort must be min, low, medium, high, xhigh, or max"
    ;;
esac

case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*) die "--timeout must be a positive integer" ;;
  0) die "--timeout must be greater than zero" ;;
esac

if [ "${#REQUESTED_VENDORS[@]}" -eq 0 ]; then
  REQUESTED_VENDORS=(openai claude gemini)
fi

if [ -z "$OUTPUT_DIR" ]; then
  OUTPUT_DIR=$(mktemp -d /tmp/vendors-hello.XXXXXX)
else
  mkdir -p "$OUTPUT_DIR"
fi

CALL_ARGS=()
for vendor in "${REQUESTED_VENDORS[@]}"; do
  CALL_ARGS+=(--vendor "$vendor")
done
if [ -n "$CONFIG_FILE" ]; then
  CALL_ARGS+=(--config "$CONFIG_FILE")
fi

"$SCRIPT_DIR/call.sh" \
  "${CALL_ARGS[@]}" \
  --effort "$EFFORT" \
  --timeout "$TIMEOUT_SECONDS" \
  --prompt "$PROMPT" \
  --output-dir "$OUTPUT_DIR" \
  --min-success "${#REQUESTED_VENDORS[@]}"

FAILED=0
for status_file in "$OUTPUT_DIR"/*/status; do
  [ -e "$status_file" ] || continue
  id=$(awk -F= '$1 == "id" { print $2; exit }' "$status_file")
  output_file=$(awk -F= '$1 == "output" { print $2; exit }' "$status_file")
  code=$(awk -F= '$1 == "exit_code" { print $2; exit }' "$status_file")

  if [ "$code" != "0" ]; then
    printf "FAIL: %s exited with %s\n" "$id" "$code" >&2
    FAILED=$((FAILED + 1))
    continue
  fi

  if [ ! -s "$output_file" ]; then
    printf "FAIL: %s exited successfully but produced empty output at %s\n" "$id" "$output_file" >&2
    FAILED=$((FAILED + 1))
  fi
done

if [ "$FAILED" -gt 0 ]; then
  printf "FAIL: %d non-error response check(s) failed. Outputs kept in %s\n" "$FAILED" "$OUTPUT_DIR" >&2
  exit 1
fi

printf "OK: all selected vendors returned non-error output. Outputs in %s\n" "$OUTPUT_DIR"
