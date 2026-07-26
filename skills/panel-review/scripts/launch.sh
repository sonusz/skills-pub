#!/usr/bin/env bash
# Usage: launch.sh --cwd DIR <prompt_file> <vendors_yaml> <output_dir>
#
# Launches the configured panel calls in parallel through the shared vendors
# module. Outputs: <output_dir>/<panel-id>/out plus log/status/call.log files.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=panel-config.sh
. "$SCRIPT_DIR/panel-config.sh"

usage() {
  cat <<'USAGE'
Usage:
  scripts/launch.sh --cwd DIR <prompt_file> <vendors_yaml> <output_dir>

Panel calls always run in path-based discovery mode with --yolo and --cwd so
each vendor reads the requested repo/source itself. Reviewed artifact bodies
must not be embedded in the prompt.

Options:
  --cwd DIR   Required repo/source root for reviewer tool access
  -h, --help  Show this help
USAGE
}

die() {
  printf "launch.sh: %s\n" "$*" >&2
  exit 2
}

require_value() {
  local opt="$1"
  local value="${2-}"

  if [ -z "$value" ]; then
    die "$opt requires a value"
  fi
}

PANEL_REVIEW_CWD_VALUE=""
PANEL_REVIEW_GIT_GUARD=0
PANEL_REVIEW_GIT_ROOT=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --cwd)
      require_value "$1" "${2-}"
      PANEL_REVIEW_CWD_VALUE="$2"
      shift 2
      ;;
    --cwd=*)
      PANEL_REVIEW_CWD_VALUE="${1#*=}"
      shift
      ;;
    --)
      shift
      break
      ;;
    --*)
      die "unknown option: $1"
      ;;
    *)
      break
      ;;
  esac
done

PROMPT_FILE="${1:-}"
VENDORS_YAML="${2:-$SCRIPT_DIR/../vendors.yaml}"
RUN_DIR="${3:-}"
PANEL_CALL_TIMEOUT="${PANEL_CALL_TIMEOUT:-300}"

if [ -z "$PROMPT_FILE" ] || [ -z "$RUN_DIR" ]; then
  usage >&2
  exit 2
fi
if [ ! -r "$PROMPT_FILE" ]; then
  printf "FAIL: cannot read prompt file at %s\n" "$PROMPT_FILE" >&2
  exit 2
fi
panel_require_config "$VENDORS_YAML"
mkdir -p "$RUN_DIR"
RUN_DIR="$(cd "$RUN_DIR" && pwd -P)"

if [ -z "$PANEL_REVIEW_CWD_VALUE" ]; then
  die "--cwd is required; panel reviews are path-based"
fi
if [ ! -d "$PANEL_REVIEW_CWD_VALUE" ]; then
  printf "FAIL: --cwd is not a directory: %s\n" "$PANEL_REVIEW_CWD_VALUE" >&2
  exit 2
fi
PANEL_REVIEW_CWD_VALUE="$(cd "$PANEL_REVIEW_CWD_VALUE" && pwd -P)"
if command -v git >/dev/null 2>&1 \
    && git -C "$PANEL_REVIEW_CWD_VALUE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  PANEL_REVIEW_GIT_ROOT=$(git -C "$PANEL_REVIEW_CWD_VALUE" rev-parse --show-toplevel)
  PANEL_REVIEW_GIT_ROOT="$(cd "$PANEL_REVIEW_GIT_ROOT" && pwd -P)"
  case "$RUN_DIR/" in
    "$PANEL_REVIEW_GIT_ROOT"/*)
      printf "FAIL: output_dir must be outside reviewed git worktree: %s\n" "$RUN_DIR" >&2
      printf "Use a temp dir such as /tmp/panel-review.XXXXXX.\n" >&2
      exit 2
      ;;
  esac
fi

repo_state_path() {
  local label="$1"
  local kind="$2"

  printf "%s\n" "$RUN_DIR/.repo-state/$label.$kind"
}

snapshot_repo_state() {
  local label="$1"
  local state_dir="$RUN_DIR/.repo-state"

  if [ -z "$PANEL_REVIEW_GIT_ROOT" ]; then
    return
  fi

  PANEL_REVIEW_GIT_GUARD=1
  mkdir -p "$state_dir"
  git -C "$PANEL_REVIEW_CWD_VALUE" status --porcelain=v1 -z > "$(repo_state_path "$label" status)"
  git -C "$PANEL_REVIEW_CWD_VALUE" diff --no-ext-diff --binary > "$(repo_state_path "$label" diff)"
  git -C "$PANEL_REVIEW_CWD_VALUE" diff --cached --no-ext-diff --binary > "$(repo_state_path "$label" cached.diff)"
}

repo_state_changed() {
  local kind=""

  if [ "$PANEL_REVIEW_GIT_GUARD" != "1" ]; then
    return 1
  fi

  for kind in status diff cached.diff; do
    if ! cmp -s "$(repo_state_path before "$kind")" "$(repo_state_path after "$kind")"; then
      return 0
    fi
  done
  return 1
}

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
    --cwd "$PANEL_REVIEW_CWD_VALUE" \
    --yolo \
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
    printf "mode=repo\n"
    printf "cwd=%s\n" "$PANEL_REVIEW_CWD_VALUE"
    printf "yolo=1\n"
    [ -z "$label" ] || printf "label=%s\n" "$label"
    printf "exit_code=%s\n" "$code"
    printf "output=%s\n" "$out_file"
    printf "log=%s\n" "$log_file"
    printf "call_log=%s\n" "$wrapper_log"
    [ -z "$reason" ] || printf "reason=%s\n" "$reason"
  } > "$status_file"

  return "$code"
}

snapshot_repo_state before

PIDS=()
for id in "${PANEL_IDS[@]}"; do
  launch_one "$id" &
  PIDS+=("$!")
done

for pid in "${PIDS[@]}"; do
  wait "$pid" 2>/dev/null || true
done

snapshot_repo_state after
if repo_state_changed; then
  printf "FAIL: repo state changed during path-based panel review.\n" >&2
  printf "Reviewers run with --yolo for read-only inspection only; no changes were reverted.\n" >&2
  printf "Inspect current status and snapshots under %s/.repo-state.\n" "$RUN_DIR" >&2
  git -C "$PANEL_REVIEW_CWD_VALUE" status --short >&2 || true
  exit 1
fi

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
