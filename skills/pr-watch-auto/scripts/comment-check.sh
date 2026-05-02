#!/usr/bin/env bash
# comment-check.sh <pr-number-or-branch>
#
# Fetches all PR review-thread pages, then returns unresolved threads.
# Outdated unresolved threads are included because they still need explicit
# resolution. Resolved threads are terminal for this workflow and omitted.
# Uses the git credential helper for auth (same mechanism as ci-check.sh).
#
# Outputs JSON:
#   {
#     "pr_number": N,
#     "unresolved_threads": [
#       { "thread_id": "...", "is_outdated": false, "path": "...", "line": N,
#         "comments": [{ "id": "...", "database_id": N, "author": "...", "body": "...", "created_at": "..." }] }
#     ],
#     "total_unresolved": N,
#     "total_review_threads": N
#   }
#
# Required fine-grained PAT permissions (Repository):
#   - Pull requests: Read

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=github-remote.sh
source "$SCRIPT_DIR/github-remote.sh"

OWNER="$GITHUB_OWNER"
REPO="$GITHUB_REPO"
TARGET="${1:-}"

if [[ -z "$TARGET" ]]; then
  echo '{"error":"No PR number or branch provided"}'
  exit 1
fi

if ! _init_github_auth; then
  echo '{"error":"No credential found for https://github.com -- check git credential helper"}'
  exit 1
fi
_source_github_auth
trap _cleanup_github_auth EXIT

# ---------------------------------------------------------------------------
# Resolve TARGET -> PR number
# ---------------------------------------------------------------------------
PR_NUMBER=""
if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  PR_NUMBER="$TARGET"
else
  # URL-encode the branch and owner before interpolating into the query
  # string so a name containing `&`, `?`, `%`, or `/` cannot inject extra
  # query parameters or otherwise distort the GitHub API request.
  TARGET_ENC=$(jq -rn --arg s "$TARGET" '$s|@uri')
  OWNER_ENC=$(jq -rn --arg s "$OWNER" '$s|@uri')
  PR_LIST=$(_auth_curl \
    "https://api.github.com/repos/$OWNER/$REPO/pulls?head=${OWNER_ENC}:${TARGET_ENC}&state=open&per_page=1" 2>/dev/null || echo "[]")

  PR_NUMBER=$(echo "$PR_LIST" | jq -r '.[0].number // empty')
  if [[ -z "$PR_NUMBER" ]]; then
    # Construct the JSON error via jq so a branch name with `"` cannot
    # break the output payload.
    jq -n --arg target "$TARGET" '{error: ("No open PR found for branch " + $target)}'
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# GraphQL: fetch all review-thread pages
# ---------------------------------------------------------------------------
QUERY='query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 20) {
            nodes {
              id
              databaseId
              body
              author { login }
              createdAt
            }
          }
        }
      }
    }
  }
}'

ALL_THREADS='[]'
CURSOR=""

while :; do
  VARIABLES=$(jq -n \
    --arg owner "$OWNER" \
    --arg repo "$REPO" \
    --argjson pr "$PR_NUMBER" \
    --arg cursor "$CURSOR" \
    '{owner: $owner, repo: $repo, pr: $pr, cursor: (if $cursor == "" then null else $cursor end)}')

  RESPONSE=$(_auth_curl \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg q "$QUERY" --argjson v "$VARIABLES" '{query: $q, variables: $v}')" \
    "https://api.github.com/graphql" 2>/dev/null)

  if echo "$RESPONSE" | jq -e '.errors' &>/dev/null; then
    echo "$RESPONSE" | jq '{error: .errors[0].message}'
    exit 1
  fi

  PAGE_THREADS=$(echo "$RESPONSE" | jq '.data.repository.pullRequest.reviewThreads.nodes // []')
  ALL_THREADS=$(jq -n --argjson existing "$ALL_THREADS" --argjson page "$PAGE_THREADS" '$existing + $page')

  HAS_NEXT=$(echo "$RESPONSE" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage // false')
  if [[ "$HAS_NEXT" != "true" ]]; then
    break
  fi
  CURSOR=$(echo "$RESPONSE" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor // ""')
done

# Extract unresolved threads, including outdated ones. Outdated means the diff
# hunk moved or was addressed by a newer push; it does not mean resolved.
UNRESOLVED=$(echo "$ALL_THREADS" | jq '[
  .[]
  | select(.isResolved == false)
  | {
      thread_id: .id,
      is_outdated: .isOutdated,
      path: .path,
      line: .line,
      comments: [.comments.nodes[] | {
        id: .id,
        database_id: .databaseId,
        author: .author.login,
        body: .body,
        created_at: .createdAt
      }]
    }
]')

TOTAL=$(echo "$UNRESOLVED" | jq 'length')
TOTAL_REVIEW_THREADS=$(echo "$ALL_THREADS" | jq 'length')

jq -n \
  --argjson pr "$PR_NUMBER" \
  --argjson threads "$UNRESOLVED" \
  --argjson total "$TOTAL" \
  --argjson total_review_threads "$TOTAL_REVIEW_THREADS" \
  '{pr_number: $pr, unresolved_threads: $threads, total_unresolved: $total, total_review_threads: $total_review_threads}'
