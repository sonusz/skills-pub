#!/usr/bin/env bash
# wait-for-state.sh <pr-or-branch> [--ci-status STATUS] [--unresolved N]
#                                  [--max-wait SECONDS] [--interval SECONDS]
#
# Blocks until one of three things happens:
#   1. ci-check.sh reports a CI `status` different from --ci-status (defaults
#      to "pending": exits as soon as CI resolves to passed / failed / error)
#   2. comment-check.sh reports a total_unresolved different from --unresolved
#      (if --unresolved is not passed, comment changes are ignored)
#   3. --max-wait seconds elapse (default 1200s = 20 min)
#
# Prints one JSON line describing the change, then exits 0:
#   {"change":"ci","status":"passed"}
#   {"change":"comments","unresolved":3}
#   {"change":"timeout","elapsed":1200}
#
# Agent-orchestrated usage pattern:
#   1. snapshot: run ci-check.sh + comment-check.sh, record status + count
#   2. decide: if action needed, do it (invoke auto-fix, push, reply...)
#   3. idle: call wait-for-state.sh with the current snapshot values; it
#      returns when something changed or the max-wait fires
#   4. loop

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET=""
EXPECTED_CI_STATUS="pending"
EXPECTED_UNRESOLVED=""
MAX_WAIT=1200
INTERVAL=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ci-status)    EXPECTED_CI_STATUS="$2"; shift 2 ;;
    --unresolved)   EXPECTED_UNRESOLVED="$2"; shift 2 ;;
    --max-wait)     MAX_WAIT="$2"; shift 2 ;;
    --interval)     INTERVAL="$2"; shift 2 ;;
    -*)             echo "unknown flag: $1" >&2; exit 1 ;;
    *)              TARGET="$1"; shift ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  TARGET=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
fi
if [[ -z "$TARGET" ]]; then
  echo "Usage: wait-for-state.sh <pr-or-branch> [--ci-status STATUS] [--unresolved N] [--max-wait SECONDS] [--interval SECONDS]" >&2
  exit 1
fi

# Validate numeric flags up front so a non-numeric value can't reach the
# arithmetic context or `sleep` later — both fail in unhelpful ways under
# `set -euo pipefail`.
case "$MAX_WAIT" in
  ''|*[!0-9]*) echo "wait-for-state.sh: --max-wait must be a non-negative integer: $MAX_WAIT" >&2; exit 1 ;;
esac
case "$INTERVAL" in
  ''|*[!0-9]*) echo "wait-for-state.sh: --interval must be a non-negative integer: $INTERVAL" >&2; exit 1 ;;
esac
if [[ -n "$EXPECTED_UNRESOLVED" ]]; then
  case "$EXPECTED_UNRESOLVED" in
    *[!0-9]*) echo "wait-for-state.sh: --unresolved must be a non-negative integer: $EXPECTED_UNRESOLVED" >&2; exit 1 ;;
  esac
fi

command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }

START=$(date +%s)

while true; do
  # --- CI check ---
  CI_JSON=$("$SCRIPT_DIR/ci-check.sh" "$TARGET" 2>/dev/null || echo '{"status":"error"}')
  CI_STATUS=$(echo "$CI_JSON" | jq -r '.status // "error"')
  if [[ "$CI_STATUS" != "$EXPECTED_CI_STATUS" ]]; then
    printf '{"change":"ci","status":%s}\n' "$(echo "$CI_STATUS" | jq -R .)"
    exit 0
  fi

  # --- Comment check (only if caller provided a baseline count) ---
  if [[ -n "$EXPECTED_UNRESOLVED" ]]; then
    COMMENT_JSON=$("$SCRIPT_DIR/comment-check.sh" "$TARGET" 2>/dev/null || echo '{"total_unresolved":0}')
    UNRESOLVED=$(echo "$COMMENT_JSON" | jq -r '.total_unresolved // 0')
    if [[ "$UNRESOLVED" != "$EXPECTED_UNRESOLVED" ]]; then
      printf '{"change":"comments","unresolved":%s}\n' "$UNRESOLVED"
      exit 0
    fi
  fi

  # --- Timeout ---
  ELAPSED=$(( $(date +%s) - START ))
  if (( ELAPSED >= MAX_WAIT )); then
    printf '{"change":"timeout","elapsed":%s}\n' "$ELAPSED"
    exit 0
  fi

  sleep "$INTERVAL"
done
