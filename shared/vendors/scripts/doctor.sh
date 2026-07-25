#!/usr/bin/env bash
# Usage: doctor.sh [--vendor openai|claude|agy|cursor|grok]...
#
# Verifies that selected vendor CLIs are not only installed, but can complete a
# trivial call through scripts/call.sh. This keeps readiness probing aligned
# with the real launch path.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=vendor-launch.sh
. "$SCRIPT_DIR/vendor-launch.sh"

usage() {
  cat <<'USAGE'
Usage:
  scripts/doctor.sh [options]

Options:
  --vendor NAME             openai, claude, agy, cursor, or grok/xai; repeatable.
                            Default: all
  --config FILE             Model mapping config (default: ../vendors.conf)
  --effort min|low|medium|high|xhigh|max
                          Probe effort hint (default: min)
  --timeout SECONDS         Per-vendor probe timeout (default: 60)
  --output-dir DIR          Write and keep probe diagnostics in DIR
  --keep-output             Keep temp probe diagnostics even on success
  -h, --help                Show this help
USAGE
}

die() {
  printf "doctor.sh: %s\n" "$*" >&2
  exit 2
}

require_value() {
  local opt="$1"
  local value="${2-}"

  if [ -z "$value" ]; then
    die "$opt requires a value"
  fi
}

print_local_setup_hint() {
  printf "\n"
  printf "Local-machine setup such as SSL certificate overrides, corporate\n"
  printf "proxy variables, custom PATH entries, CLI auth refreshes, or vendor\n"
  printf "sandbox workarounds must not be committed into this skill source.\n"
  printf "Keep those in user-level shell/vendor/Codex configuration outside\n"
  printf "%s so skill syncs do not erase them and shared source stays portable.\n" \
    "$(cd "$SCRIPT_DIR/.." && pwd)"
}

CONFIG_FILE="$SCRIPT_DIR/../vendors.conf"
EFFORT="min"
PROBE_TIMEOUT=60
OUTPUT_DIR=""
KEEP_OUTPUT=0
REQUESTED_VENDORS=()

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
      PROBE_TIMEOUT="$2"
      shift 2
      ;;
    --timeout=*)
      PROBE_TIMEOUT="${1#*=}"
      shift
      ;;
    --output-dir)
      require_value "$1" "${2-}"
      OUTPUT_DIR="$2"
      KEEP_OUTPUT=1
      shift 2
      ;;
    --output-dir=*)
      OUTPUT_DIR="${1#*=}"
      KEEP_OUTPUT=1
      shift
      ;;
    --keep-output)
      KEEP_OUTPUT=1
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

case "$PROBE_TIMEOUT" in
  ''|*[!0-9]*) die "--timeout must be a positive integer" ;;
  0) die "--timeout must be greater than zero" ;;
esac

if [ ! -r "$CONFIG_FILE" ]; then
  die "cannot read config: $CONFIG_FILE"
fi

CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"
vendors_read_models "$CONFIG_FILE"

if [ "${#REQUESTED_VENDORS[@]}" -eq 0 ]; then
  REQUESTED_VENDORS=(openai claude agy cursor grok)
fi

if [ -n "$OUTPUT_DIR" ]; then
  mkdir -p "$OUTPUT_DIR"
  WORK_DIR="$OUTPUT_DIR"
else
  WORK_DIR=$(mktemp -d /tmp/vendors-doctor.XXXXXX)
fi

cleanup() {
  local code=$?

  if [ "$code" -ne 0 ] || [ "$KEEP_OUTPUT" -eq 1 ]; then
    printf "Diagnostic output kept in %s\n" "$WORK_DIR" >&2
    printf "Troubleshooting notes: %s\n" "$(cd "$SCRIPT_DIR/.." && pwd)/TROUBLESHOOTING.md" >&2
  else
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

PROBE_PROMPT="reply with the single word READY"
PIDS=()
VENDOR_IDS=()
VENDOR_LABELS=()
VENDOR_CLIS=()
VENDOR_MODELS=()

normalize_for_report() {
  local label="$1"
  local model=""

  vendors_normalize_vendor "$label"
  model=$(vendors_select_model "$VENDORS_VENDOR_ID" "")

  VENDOR_IDS+=("$VENDORS_VENDOR_ID")
  VENDOR_LABELS+=("$label")
  VENDOR_CLIS+=("$VENDORS_VENDOR_CLI")
  VENDOR_MODELS+=("$model")
}

probe_vendor() {
  local label="$1"
  local id="$2"
  local cli="$3"
  local status_file="$WORK_DIR/$id.status"
  local call_dir="$WORK_DIR/$id-call"
  local out_file="$call_dir/$id/out"
  local snippet=""

  if ! command -v "$cli" >/dev/null 2>&1; then
    printf "CLI not on PATH\n" > "$status_file"
    return
  fi

  mkdir -p "$call_dir"

  if "$SCRIPT_DIR/call.sh" \
    --vendor "$label" \
    --config "$CONFIG_FILE" \
    --effort "$EFFORT" \
    --timeout "$PROBE_TIMEOUT" \
    --prompt "$PROBE_PROMPT" \
    --output-dir "$call_dir" \
    --min-success 1 >/dev/null 2>&1; then
    if grep -qi '\bREADY\b' "$out_file" 2>/dev/null; then
      printf "ok\n" > "$status_file"
      return
    fi
  fi

  snippet=$( (head -c 160 "$out_file" 2>/dev/null || true) | tr '\n' ' ' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  printf "%s\n" "${snippet:-no READY output before failure or timeout}" > "$status_file"
}

for requested in "${REQUESTED_VENDORS[@]}"; do
  normalize_for_report "$requested"
done

printf "Vendors readiness probe (timeout: %ss per vendor)\n" "$PROBE_TIMEOUT"
printf "  Config: %s\n" "$CONFIG_FILE"
printf "  Effort hint: %s\n" "$EFFORT"
printf "  Probing through scripts/call.sh so doctor and runtime paths stay aligned.\n"
if [ "$KEEP_OUTPUT" -eq 1 ]; then
  printf "  Diagnostics: %s\n" "$WORK_DIR"
fi
printf "\n"

for i in "${!VENDOR_IDS[@]}"; do
  probe_vendor "${VENDOR_LABELS[$i]}" "${VENDOR_IDS[$i]}" "${VENDOR_CLIS[$i]}" &
  PIDS+=("$!")
done

for pid in "${PIDS[@]}"; do
  wait "$pid" 2>/dev/null || true
done

READY=0
FAILED=0

for i in "${!VENDOR_IDS[@]}"; do
  id="${VENDOR_IDS[$i]}"
  cli="${VENDOR_CLIS[$i]}"
  model="${VENDOR_MODELS[$i]}"
  status_file="$WORK_DIR/$id.status"
  status=$(cat "$status_file" 2>/dev/null || printf "probe did not complete")

  if [ "$status" = "ok" ]; then
    printf "  [OK] %-6s cli=%-6s model=%s\n" "$id" "$cli" "${model:-<vendor default>}"
    READY=$((READY + 1))
  else
    printf "  [--] %-6s cli=%-6s model=%-28s (%s)\n" "$id" "$cli" "${model:-<vendor default>}" "$status"
    FAILED=$((FAILED + 1))
  fi
done

printf "\n"
if [ "$FAILED" -gt 0 ]; then
  printf "FAIL: %d of %d selected vendor(s) failed readiness.\n" "$FAILED" "${#VENDOR_IDS[@]}" >&2
  printf "Fix the failing CLI/auth/model/config issue and re-run this doctor.\n" >&2
  printf "Inspect %s/<vendor>.status and any %s/<vendor>-call/<id>/ files.\n" "$WORK_DIR" "$WORK_DIR" >&2
  print_local_setup_hint >&2
  exit 1
fi

printf "OK: %d selected vendor(s) ready.\n" "$READY"
print_local_setup_hint
