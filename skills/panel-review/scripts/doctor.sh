#!/usr/bin/env bash
# Usage: doctor.sh [options] [vendors_yaml]
#
# Probes the configured panel calls and synthesis call through the shared
# vendors module. Exits 0 when >=2 panel calls and the synthesis call are ready.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=panel-config.sh
. "$SCRIPT_DIR/panel-config.sh"

usage() {
  cat <<'USAGE'
Usage:
  scripts/doctor.sh [options] [vendors_yaml]

Options:
  --output-dir DIR   Write and keep probe diagnostics in DIR
  --keep-output      Keep temp probe diagnostics even on success
  -h, --help         Show this help
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

VENDORS_YAML="$SCRIPT_DIR/../vendors.yaml"
PROBE_TIMEOUT="${PANEL_DOCTOR_TIMEOUT:-60}"
PROBE_PROMPT="reply with the single word READY"
TROUBLESHOOTING_FILE="$PANEL_VENDORS_DIR/TROUBLESHOOTING.md"
OUTPUT_DIR=""
KEEP_OUTPUT=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
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
    --*)
      die "unknown option: $1"
      ;;
    *)
      VENDORS_YAML="$1"
      shift
      ;;
  esac
done

panel_require_config "$VENDORS_YAML"

if [ -n "$OUTPUT_DIR" ]; then
  mkdir -p "$OUTPUT_DIR"
  WORK="$OUTPUT_DIR"
else
  WORK=$(mktemp -d /tmp/panel-review-doctor.XXXXXX)
fi

cleanup() {
  local code=$?

  if [ "$code" -ne 0 ] || [ "$KEEP_OUTPUT" -eq 1 ]; then
    printf "Diagnostic output kept in %s\n" "$WORK" >&2
    printf "Vendor troubleshooting notes: %s\n" "$TROUBLESHOOTING_FILE" >&2
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

PANEL_IDS=()
while IFS= read -r id; do
  [ -z "$id" ] || PANEL_IDS+=("$id")
done < <(panel_yaml_panel_ids "$VENDORS_YAML")
SYNTHESIS_ID=$(panel_yaml_synthesis_id "$VENDORS_YAML")

if [ "${#PANEL_IDS[@]}" -lt 2 ]; then
  printf "FAIL: need >=2 configured panel calls for divergence signal, have %d.\n" "${#PANEL_IDS[@]}" >&2
  exit 1
fi
if [ -z "$SYNTHESIS_ID" ]; then
  printf "FAIL: vendors.yaml has no synthesis.id entry.\n" >&2
  exit 1
fi

probe_call() {
  local kind="$1"
  local id="$2"
  local status_file="$WORK/$kind-$id.status"
  local call_dir="$WORK/$kind-$id-call"
  local out_file="$call_dir/$id/out"
  local log_file="$WORK/$kind-$id.log"
  local snippet=""
  local -a call_args=()

  while IFS= read -r -d '' arg; do
    call_args+=("$arg")
  done < <(panel_call_args "$VENDORS_YAML" "$kind" "$id")
  mkdir -p "$call_dir"

  if "$PANEL_VENDOR_CALL" \
    "${call_args[@]}" \
    --id "$id" \
    --timeout "$PROBE_TIMEOUT" \
    --prompt "$PROBE_PROMPT" \
    --output-dir "$call_dir" \
    --min-success 1 > "$log_file" 2>&1; then
    if grep -qi '\bREADY\b' "$out_file" 2>/dev/null; then
      printf "ok\n" > "$status_file"
      return
    fi
  fi

  snippet=$( (head -c 160 "$out_file" "$log_file" 2>/dev/null || true) | tr '\n' ' ' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
  printf "%s\n" "${snippet:-no READY output before failure or timeout}" > "$status_file"
}

printf "Panel-review readiness probe (timeout: %ss per configured call)\n" "$PROBE_TIMEOUT"
printf "  Config: %s\n" "$(cd "$(dirname "$VENDORS_YAML")" && pwd)/$(basename "$VENDORS_YAML")"
printf "  Vendor module: %s\n" "$PANEL_VENDOR_CALL"
printf "  Probing configured calls in parallel...\n"
if [ "$KEEP_OUTPUT" -eq 1 ]; then
  printf "  Diagnostics: %s\n" "$WORK"
fi

PIDS=()
for id in "${PANEL_IDS[@]}"; do
  probe_call panel "$id" &
  PIDS+=("$!")
done
probe_call synthesis "$SYNTHESIS_ID" &
PIDS+=("$!")

for pid in "${PIDS[@]}"; do
  wait "$pid" 2>/dev/null || true
done

READY_PANEL=0
printf "\n"
for id in "${PANEL_IDS[@]}"; do
  vendor=$(panel_yaml_value "$VENDORS_YAML" panel "$id" vendor)
  model=$(panel_yaml_value "$VENDORS_YAML" panel "$id" model)
  status=$(cat "$WORK/panel-$id.status" 2>/dev/null || printf "probe did not complete")
  if [ "$status" = "ok" ]; then
    printf "  [OK] panel %-9s %-8s %s\n" "$id" "$vendor" "${model:-<default>}"
    READY_PANEL=$((READY_PANEL + 1))
  else
    printf "  [--] panel %-9s %-8s %s (%s)\n" "$id" "$vendor" "${model:-<default>}" "$status"
  fi
done

SYNTHESIS_READY=0
s_vendor=$(panel_yaml_value "$VENDORS_YAML" synthesis "$SYNTHESIS_ID" vendor)
s_model=$(panel_yaml_value "$VENDORS_YAML" synthesis "$SYNTHESIS_ID" model)
s_status=$(cat "$WORK/synthesis-$SYNTHESIS_ID.status" 2>/dev/null || printf "probe did not complete")
if [ "$s_status" = "ok" ]; then
  printf "  [OK] synthesis %-5s %-8s %s\n" "$SYNTHESIS_ID" "$s_vendor" "${s_model:-<default>}"
  SYNTHESIS_READY=1
else
  printf "  [--] synthesis %-5s %-8s %s (%s)\n" "$SYNTHESIS_ID" "$s_vendor" "${s_model:-<default>}" "$s_status"
fi
printf "\n"

if [ "$READY_PANEL" -lt 2 ]; then
  printf "FAIL: need >=2 ready panel calls, have %d.\n" "$READY_PANEL" >&2
  printf "Inspect %s/*-call/<id>/ for vendor out, status, and log files.\n" "$WORK" >&2
  exit 1
fi
if [ "$SYNTHESIS_READY" -ne 1 ]; then
  printf "FAIL: synthesis call is not ready.\n" >&2
  printf "Inspect %s/synthesis-%s-call/%s/ for vendor out, status, and log files.\n" "$WORK" "$SYNTHESIS_ID" "$SYNTHESIS_ID" >&2
  exit 1
fi

printf "OK: %d panel call(s) and synthesis call ready.\n" "$READY_PANEL"
printf "\n"
printf "Local-machine setup such as SSL cert overrides, proxies, custom PATH,\n"
printf "auth refreshes, or vendor sandbox workarounds must stay outside this\n"
printf "skill source and outside vendors.yaml.\n"
printf "Reusable vendor debugging notes live at %s.\n" "$TROUBLESHOOTING_FILE"
