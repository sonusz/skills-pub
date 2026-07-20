#!/usr/bin/env bash
# Real nested vendor integration test.
#
# Each selected outer vendor is asked to execute call.sh and fan out to the
# selected inner vendors. This intentionally spends real model calls and grants
# the outer agent command-execution permissions.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CALL_SCRIPT="$SCRIPT_DIR/call.sh"

usage() {
  cat <<'USAGE'
Usage:
  scripts/nested-test.sh --run-real-nested [options]

Options:
  --run-real-nested        Required guard; this test makes real nested calls
  --outer-vendor NAME      openai, claude, agy, or cursor; repeatable. Default: all
  --inner-vendor NAME      openai, claude, agy, or cursor; repeatable. Default: all
  --timeout SECONDS        Timeout for each outer vendor call (default: 420)
  --inner-timeout SECONDS  Timeout each outer call passes to inner call.sh (default: 120)
  --output-dir DIR         Keep outputs in DIR. Default: temp dir kept and printed
  -h, --help               Show this help
USAGE
}

die() {
  printf "nested-test.sh: %s\n" "$*" >&2
  exit 2
}

require_value() {
  local opt="$1"
  local value="${2-}"

  if [ -z "$value" ]; then
    die "$opt requires a value"
  fi
}

RUN_GUARD=0
OUTER_VENDORS=()
INNER_VENDORS=()
TIMEOUT_SECONDS=420
INNER_TIMEOUT_SECONDS=120
OUTPUT_DIR=""

lower() {
  printf "%s" "$1" | tr '[:upper:]' '[:lower:]'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --run-real-nested)
      RUN_GUARD=1
      shift
      ;;
    --outer-vendor)
      require_value "$1" "${2-}"
      OUTER_VENDORS+=("$2")
      shift 2
      ;;
    --outer-vendor=*)
      OUTER_VENDORS+=("${1#*=}")
      shift
      ;;
    --inner-vendor)
      require_value "$1" "${2-}"
      INNER_VENDORS+=("$2")
      shift 2
      ;;
    --inner-vendor=*)
      INNER_VENDORS+=("${1#*=}")
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
    --inner-timeout)
      require_value "$1" "${2-}"
      INNER_TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --inner-timeout=*)
      INNER_TIMEOUT_SECONDS="${1#*=}"
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
    *)
      die "unknown option: $1"
      ;;
  esac
done

if [ "$RUN_GUARD" -ne 1 ]; then
  die "refusing to run nested real model/tool test without --run-real-nested"
fi

case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*) die "--timeout must be a positive integer" ;;
  0) die "--timeout must be greater than zero" ;;
esac

case "$INNER_TIMEOUT_SECONDS" in
  ''|*[!0-9]*) die "--inner-timeout must be a positive integer" ;;
  0) die "--inner-timeout must be greater than zero" ;;
esac

if [ "${#OUTER_VENDORS[@]}" -eq 0 ]; then
  OUTER_VENDORS=(openai claude agy cursor)
fi
if [ "${#INNER_VENDORS[@]}" -eq 0 ]; then
  INNER_VENDORS=(openai claude agy cursor)
fi

if [ -z "$OUTPUT_DIR" ]; then
  OUTPUT_DIR=$(mktemp -d /tmp/vendors-nested.XXXXXX)
else
  mkdir -p "$OUTPUT_DIR"
fi

normalize_id() {
  case "$(lower "$1")" in
    openai|codex|gpt) printf "openai\n" ;;
    claude|anthropic) printf "claude\n" ;;
    agy|antigravity) printf "agy\n" ;;
    cursor|cursor-agent|anysphere) printf "cursor\n" ;;
    *) return 1 ;;
  esac
}

build_inner_vendor_args() {
  local vendor=""

  for vendor in "${INNER_VENDORS[@]}"; do
    printf ' --vendor %q' "$vendor"
  done
}

validate_inner_outputs() {
  local outer_id="$1"
  local inner_dir="$2"
  local failed=0
  local vendor=""
  local id=""
  local status_file=""
  local output_file=""
  local code=""

  for vendor in "${INNER_VENDORS[@]}"; do
    id=$(normalize_id "$vendor") || die "unknown inner vendor: $vendor"
    status_file="$inner_dir/$id/status"
    output_file="$inner_dir/$id/out"

    if [ ! -s "$status_file" ]; then
      printf "FAIL: outer %s did not create non-empty inner status %s\n" "$outer_id" "$status_file" >&2
      failed=$((failed + 1))
      continue
    fi
    code=$(awk -F= '$1 == "exit_code" { print $2; exit }' "$status_file")
    if [ "$code" != "0" ]; then
      printf "FAIL: outer %s inner %s exited %s\n" "$outer_id" "$id" "$code" >&2
      failed=$((failed + 1))
      continue
    fi
    if [ ! -s "$output_file" ]; then
      printf "FAIL: outer %s inner %s produced empty output %s\n" "$outer_id" "$id" "$output_file" >&2
      failed=$((failed + 1))
    fi
  done

  return "$failed"
}

INNER_VENDOR_ARGS=$(build_inner_vendor_args)
FAILED=0

printf "Nested real vendor test output_dir=%s\n" "$OUTPUT_DIR"
printf "Outer vendors: %s\n" "${OUTER_VENDORS[*]}"
printf "Inner vendors: %s\n" "${INNER_VENDORS[*]}"
printf "\n"

for outer in "${OUTER_VENDORS[@]}"; do
  outer_id=$(normalize_id "$outer") || die "unknown outer vendor: $outer"
  outer_dir="$OUTPUT_DIR/$outer_id"
  inner_dir="$outer_dir/inner"
  outer_call_dir="$outer_dir/outer"
  prompt_file="$outer_dir/prompt.txt"
  mkdir -p "$inner_dir" "$outer_call_dir"

  cat > "$prompt_file" <<PROMPT
You are running a nested vendor integration test.

Run this exact shell command once:

"$CALL_SCRIPT"$INNER_VENDOR_ARGS --effort low --prompt "Nested test invoked by outer vendor: $outer_id. Who are you?" --output-dir "$inner_dir" --min-success ${#INNER_VENDORS[@]} --timeout "$INNER_TIMEOUT_SECONDS"

After the command finishes, reply with one short sentence saying whether it completed.
Do not ask follow-up questions.
PROMPT

  "$SCRIPT_DIR/call.sh" \
    --vendor "$outer" \
    --id "outer-$outer_id" \
    --yolo \
    --effort low \
    --prompt-file "$prompt_file" \
    --output-dir "$outer_call_dir" \
    --min-success 1 \
    --timeout "$TIMEOUT_SECONDS" >/dev/null || FAILED=$((FAILED + 1))

  if validate_inner_outputs "$outer_id" "$inner_dir"; then
    printf "[OK] outer=%s nested call produced %d inner output(s)\n" "$outer_id" "${#INNER_VENDORS[@]}"
  else
    printf "[--] outer=%s nested call failed validation\n" "$outer_id" >&2
    FAILED=$((FAILED + 1))
  fi
done

if [ "$FAILED" -gt 0 ]; then
  printf "\nFAIL: nested vendor test had %d failure(s). Outputs kept in %s\n" "$FAILED" "$OUTPUT_DIR" >&2
  exit 1
fi

printf "\nOK: nested vendor test passed. Outputs in %s\n" "$OUTPUT_DIR"
