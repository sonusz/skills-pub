#!/usr/bin/env bash
# comment-check.sh <pr-number-or-branch> [--include-resolved]
#
# Fetches all PR review-thread pages and returns them as JSON.
#
# Default mode (no flag):
#   Returns only UNRESOLVED threads (outdated unresolved threads are kept,
#   since outdated means the diff hunk moved, not that the thread was
#   addressed). Output schema:
#     {
#       "pr_number": N,
#       "unresolved_threads": [
#         { "thread_id", "is_outdated", "path", "line",
#           "comments": [{ "id", "database_id", "author", "body", "created_at" }] }
#       ],
#       "total_unresolved": N,
#       "total_review_threads": N
#     }
#   This is the contract pr-watch / pr-watch-auto consume; do not change
#   field names without updating those callers.
#
# --include-resolved:
#   Returns ALL threads (resolved AND unresolved), with extra context the
#   pr-review skill needs to judge resolution quality. Output schema:
#     {
#       "pr_number": N,
#       "threads": [
#         { "thread_id", "is_resolved", "is_outdated", "path", "line",
#           "start_line", "resolved_by",
#           "comments": [{...same as above...}],
#           "last_comment": { "author", "body", "created_at" } | null
#         }
#       ],
#       "total_threads": N,
#       "total_review_threads": N
#     }
#   Threads are sorted by (path, line) for stable browsing.
#
# Uses the git credential helper for auth (same mechanism as ci-check.sh).
#
# Required fine-grained PAT permissions (Repository):
#   - Pull requests: Read

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=github-remote.sh
source "$SCRIPT_DIR/github-remote.sh"

OWNER="$GITHUB_OWNER"
REPO="$GITHUB_REPO"
TARGET=""
INCLUDE_RESOLVED=0

# Parse args. Positional is the target (PR number or branch); --include-resolved
# is the only flag.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --include-resolved) INCLUDE_RESOLVED=1; shift ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --*)
      echo "{\"error\":\"unknown flag: $1\"}"
      exit 1
      ;;
    *)
      if [[ -n "$TARGET" ]]; then
        echo "{\"error\":\"multiple targets given; expected one PR number or branch\"}"
        exit 1
      fi
      TARGET="$1"
      shift
      ;;
  esac
done

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
#
# Always request the full set of fields. Default-mode callers ignore the
# extra fields when shaping output; the cost of fetching them is negligible
# and avoids maintaining two queries.
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
          startLine
          resolvedBy { login }
          comments(first: 50) {
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

TOTAL_REVIEW_THREADS=$(echo "$ALL_THREADS" | jq 'length')

if [[ "$INCLUDE_RESOLVED" -eq 1 ]]; then
  # Full set with resolution context. Sort by (path, line) for stable
  # browsing; null line (outdated threads) sorts to the top of each path.
  THREADS=$(echo "$ALL_THREADS" | jq '[
    .[]
    | {
        thread_id:   .id,
        is_resolved: .isResolved,
        is_outdated: .isOutdated,
        path:        .path,
        line:        .line,
        start_line:  .startLine,
        resolved_by: (.resolvedBy.login // null),
        comments: [.comments.nodes[] | {
          id:          .id,
          database_id: .databaseId,
          author:      .author.login,
          body:        .body,
          created_at:  .createdAt
        }],
        last_comment: (
          if (.comments.nodes | length) > 0
          then (.comments.nodes | last | {
            author: .author.login,
            body: .body,
            created_at: .createdAt
          })
          else null end
        )
      }
    ]
    | sort_by([.path // "", (.line // 0)])
  ')
  TOTAL=$(echo "$THREADS" | jq 'length')

  jq -n \
    --argjson pr "$PR_NUMBER" \
    --argjson threads "$THREADS" \
    --argjson total "$TOTAL" \
    --argjson total_review_threads "$TOTAL_REVIEW_THREADS" \
    '{pr_number: $pr, threads: $threads, total_threads: $total, total_review_threads: $total_review_threads}'
else
  # Default: unresolved only, original schema. Outdated unresolved threads
  # are kept because they still need explicit resolution.
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

  jq -n \
    --argjson pr "$PR_NUMBER" \
    --argjson threads "$UNRESOLVED" \
    --argjson total "$TOTAL" \
    --argjson total_review_threads "$TOTAL_REVIEW_THREADS" \
    '{pr_number: $pr, unresolved_threads: $threads, total_unresolved: $total, total_review_threads: $total_review_threads}'
fi
