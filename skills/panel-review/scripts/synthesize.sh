#!/usr/bin/env bash
# Usage: synthesize.sh <prompt_file> <vendors_yaml> <run_dir>
#
# Invokes the configured synthesis call through the shared vendors module.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=panel-config.sh
. "$SCRIPT_DIR/panel-config.sh"

PROMPT_FILE="${1:-}"
VENDORS_YAML="${2:-$(panel_default_config)}"
RUN_DIR="${3:-}"
SYNTHESIS_CALL_TIMEOUT="${SYNTHESIS_CALL_TIMEOUT:-300}"
# At the timeout deadline, keep waiting in windows of this many seconds while
# the vendor is still producing output; kill only after a silent window.
SYNTHESIS_CALL_TIMEOUT_EXTEND="${SYNTHESIS_CALL_TIMEOUT_EXTEND:-300}"
# Optional cheap-model idle probe, same env contract as launch.sh.
PANEL_IDLE_PROBE_ARGS=()
if [ -n "${PANEL_IDLE_PROBE_VENDOR:-}" ]; then
  PANEL_IDLE_PROBE_ARGS+=(--idle-probe-vendor "$PANEL_IDLE_PROBE_VENDOR")
  if [ -n "${PANEL_IDLE_PROBE_MODEL:-}" ]; then
    PANEL_IDLE_PROBE_ARGS+=(--idle-probe-model "$PANEL_IDLE_PROBE_MODEL")
  fi
  if [ -n "${PANEL_IDLE_PROBE_EFFORT:-}" ]; then
    PANEL_IDLE_PROBE_ARGS+=(--idle-probe-effort "$PANEL_IDLE_PROBE_EFFORT")
  fi
fi

if [ -z "$PROMPT_FILE" ] || [ -z "$RUN_DIR" ]; then
  printf "Usage: synthesize.sh <prompt_file> <vendors_yaml> <run_dir>\n" >&2
  exit 2
fi
if [ ! -r "$PROMPT_FILE" ]; then
  printf "FAIL: cannot read prompt file at %s\n" "$PROMPT_FILE" >&2
  exit 2
fi
if [ ! -d "$RUN_DIR" ]; then
  printf "FAIL: cannot read run dir at %s\n" "$RUN_DIR" >&2
  exit 2
fi
panel_require_config "$VENDORS_YAML"

PANEL_IDS=()
while IFS= read -r id; do
  [ -z "$id" ] || PANEL_IDS+=("$id")
done < <(panel_yaml_panel_ids "$VENDORS_YAML")
SYNTHESIS_ID=$(panel_yaml_synthesis_id "$VENDORS_YAML")
if [ -z "$SYNTHESIS_ID" ]; then
  printf "FAIL: vendors.yaml has no synthesis.id entry.\n" >&2
  exit 1
fi
SYNTHESIS_DIR="$RUN_DIR/$SYNTHESIS_ID"
SUMMARY_FILE="$SYNTHESIS_DIR/out"
SYNTHESIS_LOG="$SYNTHESIS_DIR/log"
SYNTHESIS_CALL_LOG="$SYNTHESIS_DIR/call.log"
SYNTHESIS_STATUS="$SYNTHESIS_DIR/status"
mkdir -p "$SYNTHESIS_DIR"

WORK=$(mktemp -d /tmp/panel-review-synthesis.XXXXXX)
trap 'rm -rf "$WORK"' EXIT
SYNTHESIS_PROMPT="$WORK/synthesis-prompt.txt"

{
  printf "You are synthesizing a panel review from multiple vendor outputs.\n"
  printf "Classify each panel output before extracting consensus/divergence.\n"
  printf "Successful means non-empty and substantively engaging the review question.\n"
  printf "Refusal, policy block, truncation, or off-topic output is failed, not divergence.\n"
  printf "Minimum two successful panel outputs are required. If fewer than two are successful, report failures and stop.\n"
  printf "Recommendations must be procedural only: clarify, verify, test, escalate, or document risk.\n"
  printf "Do not recommend product or architecture choices from panel output alone.\n"
  printf "\n"
  printf "Output exactly this structure:\n"
  printf "━━━ Panel Review ━━━\n"
  printf "Task: {description}\n"
  printf "Vendors: {vendor_a} ✅ | {vendor_b} ✅ | {vendor_c} ✅ | {vendor_d} ✅\n"
  printf "\n"
  printf "## Consensus\n"
  printf "{Shared findings, including shared concerns}\n"
  printf "\n"
  printf "## Divergence\n"
  printf "{Where they disagree, labeled by vendor — or \"None\"}\n"
  printf "\n"
  printf "## Recommendations\n"
  printf "{Process actions to resolve divergence or address unanimous concerns}\n"
  printf "━━━ End ━━━\n"
  printf "\n"
  printf "Original review prompt:\n"
  printf '%s\n' "----- BEGIN ORIGINAL PROMPT -----"
  cat "$PROMPT_FILE"
  printf "\n"
  printf '%s\n' "----- END ORIGINAL PROMPT -----"
  printf "\nPanel outputs:\n"

  for id in "${PANEL_IDS[@]}"; do
    vendor=$(panel_yaml_value "$VENDORS_YAML" panel "$id" vendor)
    status_file="$RUN_DIR/$id/status"
    out_file="$RUN_DIR/$id/out"
    printf "\n"
    printf '%s\n' "----- BEGIN PANEL OUTPUT: $id ($vendor) -----"
    if [ -r "$status_file" ]; then
      printf "Status:\n"
      cat "$status_file"
      printf "\n"
    else
      printf "Status: missing\n"
    fi
    printf "Output:\n"
    if [ -r "$out_file" ]; then
      cat "$out_file"
    else
      printf "MISSING OUTPUT FILE: %s\n" "$out_file"
    fi
    printf "\n"
    printf '%s\n' "----- END PANEL OUTPUT: $id ($vendor) -----"
  done
} > "$SYNTHESIS_PROMPT"

CALL_ARGS=()
while IFS= read -r -d '' arg; do
  CALL_ARGS+=("$arg")
done < <(panel_call_args "$VENDORS_YAML" synthesis "$SYNTHESIS_ID")
SYNTHESIS_VENDOR=$(panel_yaml_value "$VENDORS_YAML" synthesis "$SYNTHESIS_ID" vendor)
label=""
reason=""
vendor_code=""

if "$PANEL_VENDOR_CALL" \
  "${CALL_ARGS[@]}" \
  --id "$SYNTHESIS_ID" \
  --timeout "$SYNTHESIS_CALL_TIMEOUT" \
  --timeout-extend "$SYNTHESIS_CALL_TIMEOUT_EXTEND" \
  ${PANEL_IDLE_PROBE_ARGS[@]+"${PANEL_IDLE_PROBE_ARGS[@]}"} \
  --prompt-file "$SYNTHESIS_PROMPT" \
  --output-dir "$RUN_DIR" \
  --min-success 1 > "$SYNTHESIS_CALL_LOG" 2>&1; then
  code=0
else
  code=$?
fi

if [ -r "$SYNTHESIS_STATUS" ]; then
  vendor_code=$(awk -F= '$1 == "exit_code" { print $2; exit }' "$SYNTHESIS_STATUS")
  label=$(awk -F= '$1 == "label" { print $2; exit }' "$SYNTHESIS_STATUS")
  reason=$(awk -F= '$1 == "reason" { print $2; exit }' "$SYNTHESIS_STATUS")
fi
[ -n "$vendor_code" ] && code="$vendor_code"

{
  printf "id=%s\n" "$SYNTHESIS_ID"
  printf "kind=synthesis\n"
  printf "vendor=%s\n" "$SYNTHESIS_VENDOR"
  [ -z "$label" ] || printf "label=%s\n" "$label"
  printf "exit_code=%s\n" "$code"
  printf "output=%s\n" "$SUMMARY_FILE"
  printf "log=%s\n" "$SYNTHESIS_LOG"
  printf "call_log=%s\n" "$SYNTHESIS_CALL_LOG"
  [ -z "$reason" ] || printf "reason=%s\n" "$reason"
} > "$SYNTHESIS_STATUS"

if [ "$code" -ne 0 ]; then
  printf "FAIL: synthesis call failed. Inspect %s\n" "$SYNTHESIS_LOG" >&2
  exit "$code"
fi

printf "OK: synthesis output written to %s\n" "$SUMMARY_FILE"
