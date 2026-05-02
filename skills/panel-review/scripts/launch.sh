#!/usr/bin/env bash
# Usage: launch.sh <prompt_file> <vendors_yaml> <output_dir>
#
# Launches the configured panel calls in parallel through the shared vendors
# module. Outputs: <output_dir>/<panel-id>/out plus log/status/call.log files.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=panel-config.sh
. "$SCRIPT_DIR/panel-config.sh"

PROMPT_FILE="${1:-}"
VENDORS_YAML="${2:-$SCRIPT_DIR/../vendors.yaml}"
RUN_DIR="${3:-}"
PANEL_CALL_TIMEOUT="${PANEL_CALL_TIMEOUT:-300}"

if [ -z "$PROMPT_FILE" ] || [ -z "$RUN_DIR" ]; then
  printf "Usage: launch.sh <prompt_file> <vendors_yaml> <output_dir>\n" >&2
  exit 2
fi
if [ ! -r "$PROMPT_FILE" ]; then
  printf "FAIL: cannot read prompt file at %s\n" "$PROMPT_FILE" >&2
  exit 2
fi
panel_require_config "$VENDORS_YAML"
mkdir -p "$RUN_DIR"

PANEL_IDS=()
while IFS= read -r id; do
  [ -z "$id" ] || PANEL_IDS+=("$id")
done < <(panel_yaml_panel_ids "$VENDORS_YAML")
if [ "${#PANEL_IDS[@]}" -lt 2 ]; then
  printf "FAIL: need >=2 configured panel calls for divergence signal, have %d.\n" "${#PANEL_IDS[@]}" >&2
  exit 1
fi

launch_one() {
  local id="$1"
  local call_dir="$RUN_DIR/$id"
  local out_file="$call_dir/out"
  local log_file="$call_dir/log"
  local wrapper_log="$call_dir/call.log"
  local status_file="$call_dir/status"
  local vendor=""
  local label=""
  local reason=""
  local vendor_code=""
  local code=0
  local -a call_args=()

  mkdir -p "$call_dir"
  vendor=$(panel_yaml_value "$VENDORS_YAML" panel "$id" vendor)
  while IFS= read -r -d '' arg; do
    call_args+=("$arg")
  done < <(panel_call_args "$VENDORS_YAML" panel "$id")

  if "$PANEL_VENDOR_CALL" \
    "${call_args[@]}" \
    --id "$id" \
    --timeout "$PANEL_CALL_TIMEOUT" \
    --prompt-file "$PROMPT_FILE" \
    --output-dir "$RUN_DIR" \
    --min-success 1 > "$wrapper_log" 2>&1; then
    code=0
  else
    code=$?
  fi
  if [ -r "$status_file" ]; then
    vendor_code=$(awk -F= '$1 == "exit_code" { print $2; exit }' "$status_file")
    label=$(awk -F= '$1 == "label" { print $2; exit }' "$status_file")
    reason=$(awk -F= '$1 == "reason" { print $2; exit }' "$status_file")
  fi
  [ -n "$vendor_code" ] && code="$vendor_code"

  {
    printf "id=%s\n" "$id"
    printf "kind=panel\n"
    printf "vendor=%s\n" "$vendor"
    [ -z "$label" ] || printf "label=%s\n" "$label"
    printf "exit_code=%s\n" "$code"
    printf "output=%s\n" "$out_file"
    printf "log=%s\n" "$log_file"
    printf "call_log=%s\n" "$wrapper_log"
    [ -z "$reason" ] || printf "reason=%s\n" "$reason"
  } > "$status_file"

  return "$code"
}

PIDS=()
for id in "${PANEL_IDS[@]}"; do
  launch_one "$id" &
  PIDS+=("$!")
done

for pid in "${PIDS[@]}"; do
  wait "$pid" 2>/dev/null || true
done

SUCCESS=0
for id in "${PANEL_IDS[@]}"; do
  code=$(awk -F= '$1 == "exit_code" { print $2; exit }' "$RUN_DIR/$id/status" 2>/dev/null || printf "missing")
  if [ "$code" = "0" ] && [ -s "$RUN_DIR/$id/out" ]; then
    SUCCESS=$((SUCCESS + 1))
  fi
done

if [ "$SUCCESS" -lt 2 ]; then
  printf "FAIL: need >=2 successful panel outputs, have %d.\n" "$SUCCESS" >&2
  printf "Inspect logs in %s.\n" "$RUN_DIR" >&2
  exit 1
fi

printf "OK: %d of %d panel calls completed. Outputs in %s\n" "$SUCCESS" "${#PANEL_IDS[@]}" "$RUN_DIR"
