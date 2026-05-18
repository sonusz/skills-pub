#!/usr/bin/env bash
# pr-review-check.sh <pr-number-or-branch>
#
# Fetches PR-level signals that aren't tied to a specific line of code:
#   - Top-level reviews (the body of /pulls/{N}/reviews entries, including
#     CHANGES_REQUESTED reviews like "rename the branch")
#   - Issue-level comments (/issues/{N}/comments — general PR comments)
#
# `comment-check.sh` covers inline review THREADS. This script covers the
# rest: review summaries, change-request bodies, and free-form PR comments.
# Run both to get the full picture of unaddressed reviewer feedback.
#
# Output schema:
#   {
#     "pr_number": N,
#     "active_change_requests": [
#       { "review_id", "author", "body", "submitted_at" }
#     ],
#     "review_bodies": [
#       { "review_id", "author", "state", "body", "submitted_at" }
#     ],
#     "issue_comments": [
#       { "id", "author", "body", "created_at", "updated_at" }
#     ],
#     "total_active_change_requests": N,
#     "total_review_bodies": N,
#     "total_issue_comments": N
#   }
#
# `active_change_requests` mirrors GitHub's merge-block semantics: for each
# author, look at their reviews in chronological order and take the LATEST one
# whose state is APPROVED, CHANGES_REQUESTED, or DISMISSED (COMMENTED and
# PENDING never change merge-block state — a reviewer can drop follow-up
# COMMENTED notes after a CHANGES_REQUESTED without clearing it). If the
# resulting state is CHANGES_REQUESTED, the author still blocks merge.
#
# `review_bodies` contains every review with a non-empty body, regardless of
# state. Useful for surfacing bot summaries (e.g. Copilot "Pull request
# overview") that aren't tied to a line.
#
# Uses the git credential helper for auth (same mechanism as ci-check.sh and
# comment-check.sh).
#
# Required fine-grained PAT permissions (Repository):
#   - Pull requests: Read
#   - Issues: Read   (issue comments share the issues API surface)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=github-remote.sh
source "$SCRIPT_DIR/github-remote.sh"

OWNER="$GITHUB_OWNER"
REPO="$GITHUB_REPO"
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
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
# Resolve TARGET -> PR number (same logic as comment-check.sh)
# ---------------------------------------------------------------------------
PR_NUMBER=""
if [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  PR_NUMBER="$TARGET"
else
  TARGET_ENC=$(jq -rn --arg s "$TARGET" '$s|@uri')
  OWNER_ENC=$(jq -rn --arg s "$OWNER" '$s|@uri')
  PR_LIST=$(_auth_curl \
    "https://api.github.com/repos/$OWNER/$REPO/pulls?head=${OWNER_ENC}:${TARGET_ENC}&state=open&per_page=1" 2>/dev/null || echo "[]")

  PR_NUMBER=$(echo "$PR_LIST" | jq -r '.[0].number // empty')
  if [[ -z "$PR_NUMBER" ]]; then
    jq -n --arg target "$TARGET" '{error: ("No open PR found for branch " + $target)}'
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Paginated GET via Link header. Returns concatenated JSON array on stdout.
# ---------------------------------------------------------------------------
fetch_paginated() {
  local url="$1"
  local all='[]'
  local next="$url"
  local tmp_headers tmp_body http page

  while [[ -n "$next" ]]; do
    tmp_headers="$(mktemp)"
    tmp_body="$(mktemp)"
    http=$(_auth_curl -D "$tmp_headers" -o "$tmp_body" -w "%{http_code}" "$next" || echo "000")
    if [[ "$http" != "200" ]]; then
      rm -f "$tmp_headers" "$tmp_body"
      jq -n --arg url "$next" --arg code "$http" \
        '{error: ("GET " + $url + " returned HTTP " + $code)}' >&2
      return 1
    fi
    page=$(cat "$tmp_body")
    all=$(jq -n --argjson a "$all" --argjson p "$page" '$a + $p')
    # Parse Link header for rel="next". Header is case-insensitive; grep -i.
    next=$(grep -i '^link:' "$tmp_headers" 2>/dev/null \
      | tr ',' '\n' \
      | awk -F';' '/rel="next"/ {gsub(/[ <>]/, "", $1); print $1; exit}')
    rm -f "$tmp_headers" "$tmp_body"
  done
  echo "$all"
}

REVIEWS_JSON=$(fetch_paginated "https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews?per_page=100") \
  || { echo '{"error":"failed to fetch reviews"}'; exit 1; }

ISSUE_COMMENTS_JSON=$(fetch_paginated "https://api.github.com/repos/$OWNER/$REPO/issues/$PR_NUMBER/comments?per_page=100") \
  || { echo '{"error":"failed to fetch issue comments"}'; exit 1; }

# ---------------------------------------------------------------------------
# Shape output.
#
# active_change_requests: For each author, take their latest review whose
# state is in {APPROVED, CHANGES_REQUESTED, DISMISSED} — these are the only
# states that change merge-block status. COMMENTED and PENDING are skipped so
# follow-up notes don't clear an outstanding CHANGES_REQUESTED. If that
# effective state is CHANGES_REQUESTED, include it. Each entry exposes both
# the effective review (whose state is CHANGES_REQUESTED) AND the body of
# that review — that body is what the reviewer wants the author to address.
#
# review_bodies: Every review with a non-empty body, regardless of state.
# Bots often leave the only meaningful summary in a COMMENTED review body.
# ---------------------------------------------------------------------------
OUTPUT=$(jq -n \
  --argjson pr "$PR_NUMBER" \
  --argjson reviews "$REVIEWS_JSON" \
  --argjson issue_comments "$ISSUE_COMMENTS_JSON" '
  def normalize_review:
    {
      review_id:    .id,
      author:       (.user.login // "unknown"),
      state:        .state,
      body:         (.body // ""),
      submitted_at: (.submitted_at // .created_at // "")
    };

  ($reviews | map(normalize_review)) as $all
  | (
      $all
      | group_by(.author)
      | map(
          map(select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED"))
          | sort_by(.submitted_at)
          | last
        )
      | map(select(. != null and .state == "CHANGES_REQUESTED"))
      | map({review_id, author, body, submitted_at})
    ) as $acrs
  | (
      $all
      | map(select(.body != ""))
      | sort_by(.submitted_at)
    ) as $bodies
  | (
      $issue_comments
      | map({
          id:         .id,
          author:     (.user.login // "unknown"),
          body:       (.body // ""),
          created_at: .created_at,
          updated_at: .updated_at
        })
    ) as $ics
  | {
      pr_number:                    $pr,
      active_change_requests:       $acrs,
      review_bodies:                $bodies,
      issue_comments:               $ics,
      total_active_change_requests: ($acrs | length),
      total_review_bodies:          ($bodies | length),
      total_issue_comments:         ($ics | length)
    }
')

echo "$OUTPUT"
