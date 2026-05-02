#!/usr/bin/env bash
# ci-check.sh [<branch-or-pr-number>]
#
# Polls GitHub CI status using:
#   - git remote -> auto-detect GitHub owner/repo
#   - git credential helper for auth (fine-grained PAT)
#   - Pull Requests API  -> resolve branch/PR to head SHA
#   - Actions Workflow Runs API -> query CI state for that SHA
#   - Actions Jobs API -> fetch failed job/step details
#
# Outputs JSON:
#   { "status": "passed|failed|pending|error", "failed_jobs": [...], "error_log": "..." }
#
# Required fine-grained PAT permissions (Repository):
#   - Pull requests: Read
#   - Actions: Read
#   - Contents: Read

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=github-remote.sh
source "$SCRIPT_DIR/github-remote.sh"

OWNER="$GITHUB_OWNER"
REPO="$GITHUB_REPO"
TARGET="${1:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")}"

if [[ -z "$TARGET" ]]; then
  echo '{"status":"error","failed_jobs":[],"error_log":"Could not determine branch -- pass branch or PR number as argument"}'
  exit 1
fi

BASE_URL="https://api.github.com/repos/$OWNER/$REPO"

if ! _init_github_auth; then
  echo '{"status":"error","failed_jobs":[],"error_log":"No credential found for https://github.com -- check git credential helper"}'
  exit 1
fi
_source_github_auth
trap _cleanup_github_auth EXIT

api_get() {
  local url="$1"
  local tmp_headers tmp_body http
  tmp_headers="$(mktemp)"
  tmp_body="$(mktemp)"

  http=$(_auth_curl -D "$tmp_headers" -o "$tmp_body" -w "%{http_code}" \
    "$url" || true)

  if [[ "$http" != "200" ]]; then
    echo "API error: HTTP $http for $url" >&2
    tr -d '\r' < "$tmp_headers" | grep -i 'x-accepted-github-permissions' >&2 || true
    rm -f "$tmp_headers" "$tmp_body"
    return 1
  fi

  cat "$tmp_body"
  rm -f "$tmp_headers" "$tmp_body"
}

emit() {
  jq -n \
    --arg status "$1" \
    --argjson failed_jobs "$2" \
    --arg error_log "$3" \
    '{status: $status, failed_jobs: $failed_jobs, error_log: $error_log}'
}

# ---------------------------------------------------------------------------
# Resolve TARGET -> commit SHA
# ---------------------------------------------------------------------------
COMMIT_SHA=""

if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  # Target is a PR number
  PR_BODY=$(api_get "$BASE_URL/pulls/$TARGET") || {
    emit "error" "[]" "Could not fetch PR #$TARGET -- check PAT permissions"
    exit 0
  }
  COMMIT_SHA=$(echo "$PR_BODY" | jq -r '.head.sha')
else
  # Target is a branch name -- find open PR for it, or fall back to ref.
  # URL-encode the branch and owner before interpolating into URL paths or
  # query strings so a name containing `&`, `?`, `%`, or `/` cannot inject
  # extra parameters or otherwise distort the GitHub API request.
  TARGET_ENC=$(jq -rn --arg s "$TARGET" '$s|@uri')
  OWNER_ENC=$(jq -rn --arg s "$OWNER" '$s|@uri')
  PR_LIST=$(api_get "$BASE_URL/pulls?head=${OWNER_ENC}:${TARGET_ENC}&state=open&per_page=1") || true

  if [[ -n "$PR_LIST" ]] && [[ "$(echo "$PR_LIST" | jq 'length')" -gt 0 ]]; then
    COMMIT_SHA=$(echo "$PR_LIST" | jq -r '.[0].head.sha')
  else
    REF_BODY=$(api_get "$BASE_URL/git/ref/heads/${TARGET_ENC}") || {
      emit "error" "[]" "Branch '$TARGET' not found on $OWNER/$REPO"
      exit 0
    }
    COMMIT_SHA=$(echo "$REF_BODY" | jq -r '.object.sha')
  fi
fi

if [[ -z "$COMMIT_SHA" || "$COMMIT_SHA" == "null" ]]; then
  emit "error" "[]" "Could not resolve commit SHA for: $TARGET"
  exit 0
fi

# ---------------------------------------------------------------------------
# Query Actions Workflow Runs API
# ---------------------------------------------------------------------------
ACTIONS_BODY=$(api_get "$BASE_URL/actions/runs?head_sha=$COMMIT_SHA&per_page=20") || {
  emit "error" "[]" "Could not fetch workflow runs for SHA $COMMIT_SHA -- PAT may lack Actions:Read"
  exit 0
}

TOTAL_RUNS=$(echo "$ACTIONS_BODY" | jq -r '.total_count')

if [[ "$TOTAL_RUNS" == "0" ]]; then
  emit "pending" "[]" "No workflow runs found yet for SHA ${COMMIT_SHA:0:7}"
  exit 0
fi

# Determine overall status
HAS_FAILURE=$(echo "$ACTIONS_BODY" | jq '[.workflow_runs[] | select(.conclusion == "failure" or .conclusion == "cancelled" or .conclusion == "timed_out")] | length')
HAS_PENDING=$(echo "$ACTIONS_BODY" | jq '[.workflow_runs[] | select(.status != "completed")] | length')

if [[ "$HAS_FAILURE" -gt 0 ]]; then
  OVERALL="failed"
elif [[ "$HAS_PENDING" -gt 0 ]]; then
  OVERALL="pending"
else
  OVERALL="passed"
fi

# ---------------------------------------------------------------------------
# Handle each status
# ---------------------------------------------------------------------------
case "$OVERALL" in
  passed)
    emit "passed" "[]" ""
    ;;
  pending)
    emit "pending" "[]" "CI is still running @ ${COMMIT_SHA:0:7}"
    ;;
  failed)
    # Collect failed run IDs
    FAILED_RUN_IDS=$(echo "$ACTIONS_BODY" | jq -r '.workflow_runs[] | select(.conclusion == "failure") | .id')

    FAILED_JOBS_JSON="[]"
    ERROR_LOG=""

    for RUN_ID in $FAILED_RUN_IDS; do
      RUN_NAME=$(echo "$ACTIONS_BODY" | jq -r --argjson id "$RUN_ID" '.workflow_runs[] | select(.id == $id) | .name')
      RUN_URL=$(echo "$ACTIONS_BODY" | jq -r --argjson id "$RUN_ID" '.workflow_runs[] | select(.id == $id) | .html_url')

      JOBS_BODY=$(api_get "$BASE_URL/actions/runs/$RUN_ID/jobs") || continue

      # Extract failed jobs with their failed steps
      RUN_FAILED_JOBS=$(echo "$JOBS_BODY" | jq '[
        .jobs[]
        | select(.conclusion == "failure")
        | {
            job: .name,
            url: .html_url,
            failed_steps: [.steps[] | select(.conclusion == "failure") | .name]
          }
      ]')

      # Merge into accumulated list
      FAILED_JOBS_JSON=$(echo "$FAILED_JOBS_JSON" "$RUN_FAILED_JOBS" | jq -s 'add')

      # Build human-readable error log
      RUN_LOG=$(echo "$JOBS_BODY" | jq -r '
        .jobs[]
        | select(.conclusion == "failure")
        | "[FAILED] \(.name)\n  URL: \(.html_url)"
        + (.steps
            | map(select(.conclusion == "failure"))
            | if length > 0 then
                "\n  Failed steps:" + (map("\n    - \(.name)") | join(""))
              else ""
              end
          )
      ')

      if [[ -n "$ERROR_LOG" ]]; then
        ERROR_LOG="${ERROR_LOG}\n\n${RUN_LOG}"
      else
        ERROR_LOG="$RUN_LOG"
      fi
    done

    emit "failed" "$FAILED_JOBS_JSON" "$ERROR_LOG"
    ;;
esac
