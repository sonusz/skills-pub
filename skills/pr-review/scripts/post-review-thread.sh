#!/usr/bin/env bash
# post-review-thread.sh -- post one inline review thread on a GitHub PR
#
# Posts a single review comment using the PR review-comments REST API,
# attached to a specific file + line (or line range). Uses the git
# credential helper for auth (same pattern as shared/github-ops scripts).
#
# Stale-SHA defense: by default the script re-fetches the PR head SHA
# from the API immediately before posting. The PR may have moved since
# the agent finished its review, and a stale `commit_id` causes GitHub
# to reject the comment as out-of-diff. Pass --commit <sha> to override
# (useful for testing with a fixed commit, or when the caller has just
# fetched the SHA and wants determinism).
#
# Usage:
#   post-review-thread.sh \
#     --pr <number> \
#     --path <file-path> \
#     --body-file <path-to-body-md> \
#     [--line <n> | --start-line <start> --end-line <end>] \
#     [--commit <sha>]
#
# Required GitHub PAT scope: Pull requests: Write.
#
# Output: JSON line {id, html_url, path, line, start_line, commit_id}
# on success. The commit_id is included so the caller can confirm which
# SHA the comment was anchored to (especially when re-fetched).
#
# Exit codes:
#   0 success
#   1 usage error
#   2 auth / network failure
#   3 GitHub API rejected the comment (out-of-diff line, invalid SHA, etc.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<EOF >&2
Usage:
  $0 --pr <number> --path <file> --body-file <path> \\
       [--line <n> | --start-line <start> --end-line <end>] \\
       [--commit <sha>]

  --commit is optional; when omitted, the head SHA is re-fetched from the
  PR API immediately before posting (recommended — defends against the
  PR moving during review).
EOF
  exit 1
}

PR=""
COMMIT=""
PATH_ARG=""
BODY_FILE=""
LINE=""
START_LINE=""
END_LINE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)         PR="$2"; shift 2 ;;
    --commit)     COMMIT="$2"; shift 2 ;;
    --path)       PATH_ARG="$2"; shift 2 ;;
    --body-file)  BODY_FILE="$2"; shift 2 ;;
    --line)       LINE="$2"; shift 2 ;;
    --start-line) START_LINE="$2"; shift 2 ;;
    --end-line)   END_LINE="$2"; shift 2 ;;
    -h|--help)    usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

[[ -z "$PR" || -z "$PATH_ARG" || -z "$BODY_FILE" ]] && usage
[[ ! -f "$BODY_FILE" ]] && { echo "Error: body file not found: $BODY_FILE" >&2; exit 1; }

# Either --line OR (--start-line AND --end-line); not both styles, not neither.
if [[ -n "$LINE" && ( -n "$START_LINE" || -n "$END_LINE" ) ]]; then
  echo "Error: cannot mix --line with --start-line/--end-line" >&2
  usage
fi
if [[ -z "$LINE" && ( -z "$START_LINE" || -z "$END_LINE" ) ]]; then
  echo "Error: provide --line or both --start-line and --end-line" >&2
  usage
fi
if [[ -n "$START_LINE" && -n "$END_LINE" && "$START_LINE" -gt "$END_LINE" ]]; then
  echo "Error: --start-line ($START_LINE) > --end-line ($END_LINE)" >&2
  usage
fi

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
source "$SKILL_DIR/shared/github-ops/github-remote.sh"
if ! _init_github_auth >/dev/null; then
  echo "Error: GitHub credential helper returned no token." >&2
  exit 2
fi
_source_github_auth
PAYLOAD=$(mktemp)
trap 'rm -f "$PAYLOAD"; _cleanup_github_auth' EXIT

# ---------------------------------------------------------------------------
# Resolve commit SHA (re-fetch by default; --commit overrides for determinism)
# ---------------------------------------------------------------------------
if [[ -z "$COMMIT" ]]; then
  PR_JSON=$(_auth_curl "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/pulls/$PR")
  if echo "$PR_JSON" | jq -e '.message' >/dev/null 2>&1; then
    echo "Error: could not fetch PR head SHA: $(echo "$PR_JSON" | jq -r .message)" >&2
    exit 2
  fi
  COMMIT=$(echo "$PR_JSON" | jq -r '.head.sha')
  if [[ -z "$COMMIT" || "$COMMIT" == "null" ]]; then
    echo "Error: PR API returned no head SHA." >&2
    exit 2
  fi
fi

URL="https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/pulls/$PR/comments"

# ---------------------------------------------------------------------------
# Build payload
# ---------------------------------------------------------------------------
if [[ -n "$LINE" ]]; then
  jq -n \
    --rawfile body "$BODY_FILE" \
    --arg commit_id "$COMMIT" \
    --arg path "$PATH_ARG" \
    --argjson line "$LINE" \
    '{body: $body, commit_id: $commit_id, path: $path, side: "RIGHT", line: $line}' \
    > "$PAYLOAD"
else
  jq -n \
    --rawfile body "$BODY_FILE" \
    --arg commit_id "$COMMIT" \
    --arg path "$PATH_ARG" \
    --argjson start_line "$START_LINE" \
    --argjson line "$END_LINE" \
    '{body: $body, commit_id: $commit_id, path: $path, side: "RIGHT", start_side: "RIGHT", start_line: $start_line, line: $line}' \
    > "$PAYLOAD"
fi

# ---------------------------------------------------------------------------
# Post
# ---------------------------------------------------------------------------
RESPONSE=$(_auth_curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  --data "@$PAYLOAD" "$URL")

if echo "$RESPONSE" | jq -e '.message' >/dev/null 2>&1; then
  ERR=$(echo "$RESPONSE" | jq -r '.message')
  echo "Error: GitHub rejected comment (commit_id=$COMMIT): $ERR" >&2
  echo "$RESPONSE" | jq '{message, errors}' >&2
  exit 3
fi

echo "$RESPONSE" | jq --arg commit_id "$COMMIT" '{id, html_url, path, line, start_line, commit_id: $commit_id}'
