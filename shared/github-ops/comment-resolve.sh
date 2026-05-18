#!/usr/bin/env bash
# comment-resolve.sh <pr-number> <thread-id> <reply-message> <comment-database-id> \
#                    --prior-comment-ids "id1,id2,..." [--no-resolve]
#
# Posts a reply to a review comment, then (unless --no-resolve is set)
# resolves its thread. The reply is REQUIRED — this script will not
# resolve a thread without leaving a note. Silent resolves are exactly
# the failure mode this script exists to prevent.
#
# Stateful gate enforcement
# -------------------------
# `--prior-comment-ids` is the comma-separated list of comment `databaseId`s
# that existed in the thread when the user confirmed the gate (taken from
# the snapshot the orchestrator captured at triage time). The script:
#
#   1. Before posting the reply, re-fetches the thread's comment IDs and
#      refuses (exit 4) if they have drifted from the prior set. New
#      comments mean someone replied to the thread between gate
#      confirmation and now — the user's consent was bound to the prior
#      state and is invalid for the new state. Re-triage required.
#   2. After posting succeeds, re-fetches again and refuses to resolve
#      (exit 5) if the thread state is anything other than
#      `prior_set ∪ {our new reply id}`. The reply stays visible; the
#      thread is intentionally left open for the user to re-evaluate.
#
# This converts the "don't silently resolve" invariant from prose-trust
# (the agent must remember to re-check) into structural enforcement (the
# script refuses without an up-to-date snapshot).
#
# --no-resolve: post the reply but leave the thread open. Use when the
# reviewer needs to weigh in on an escalation (e.g. auto-fix's
# "doc-drift" category, where code and docs disagree and a human must
# pick a side). The pre-reply drift check still runs.
#
# Uses the git credential helper for auth.
#
# Arguments (first four required):
#   pr-number              PR number
#   thread-id              GraphQL node ID of the review thread (PRRT_...)
#   reply-message          Message body to post (Markdown allowed)
#   comment-database-id    REST API comment ID (number) that the reply threads to
# Required flag:
#   --prior-comment-ids    Comma-separated databaseIds present in the thread
#                          at gate-confirmation time. Empty allowed only if
#                          the snapshot truly contained zero comments
#                          (rare; the originating comment always exists).
# Optional flag:
#   --no-resolve           Post the reply only; do not resolve the thread.
#
# Exit codes:
#   0  reply posted (+ thread resolved, unless --no-resolve)
#   1  usage / setup error (missing arg, bad auth)
#   2  reply POST failed — thread NOT resolved; retry-able
#   3  reply posted but resolve mutation failed; reply is visible, thread still open
#   4  thread drifted before reply — refused, no action taken; re-triage required
#   5  thread drifted after our reply — reply is visible, resolve refused; re-evaluate
#
# Example (reply + resolve):
#   comment-resolve.sh 282 PRRT_kwDO... "Fixed in abc1234 — reasoning..." 3119683868 \
#     --prior-comment-ids 3119683868
#
# Example (reply only, for doc-drift escalation):
#   comment-resolve.sh 282 PRRT_kwDO... "Docs say X, code does Y — please decide." 3119683868 \
#     --prior-comment-ids 3119683868 --no-resolve

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=github-remote.sh
source "$SCRIPT_DIR/github-remote.sh"

OWNER="$GITHUB_OWNER"
REPO="$GITHUB_REPO"
PR_NUMBER="${1:-}"
THREAD_ID="${2:-}"
REPLY_MSG="${3:-}"
COMMENT_DB_ID="${4:-}"
NO_RESOLVE=0
PRIOR_IDS=""
HAVE_PRIOR=0
shift 4 2>/dev/null || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-resolve)         NO_RESOLVE=1; shift ;;
    --prior-comment-ids)  PRIOR_IDS="${2:-}"; HAVE_PRIOR=1; shift 2 ;;
    --prior-comment-ids=*) PRIOR_IDS="${1#*=}"; HAVE_PRIOR=1; shift ;;
    -*) echo "comment-resolve.sh: unknown flag: $1" >&2; exit 1 ;;
    *)  echo "comment-resolve.sh: unexpected positional arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$PR_NUMBER" || -z "$THREAD_ID" || -z "$REPLY_MSG" || -z "$COMMENT_DB_ID" ]]; then
  cat >&2 <<EOF
Usage: comment-resolve.sh <pr-number> <thread-id> <reply-message> <comment-database-id> \\
         --prior-comment-ids "id1,id2,..." [--no-resolve]

The first four positional arguments and --prior-comment-ids are required.
The reply must be posted before the thread is resolved; this script will
not resolve silently. --prior-comment-ids is the snapshot of comment
databaseIds taken at gate-confirmation time; the script refuses to act if
the thread has drifted from that snapshot.

See: $(basename "$0") header comment for full arg docs.
EOF
  exit 1
fi

if [[ "$HAVE_PRIOR" -ne 1 ]]; then
  echo "comment-resolve.sh: --prior-comment-ids is required (pass an empty string only if the snapshot truly had zero comments)" >&2
  exit 1
fi

if ! _init_github_auth; then
  echo "Error: No credential found for https://github.com" >&2
  exit 1
fi
_source_github_auth

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# _normalize_id_list <comma-separated-ids>
# Sorts numerically and rejoins. Empty input → empty output.
_normalize_id_list() {
  local raw="$1"
  if [[ -z "$raw" ]]; then
    printf ''
    return 0
  fi
  printf '%s\n' "$raw" | tr ',' '\n' | sed '/^$/d' | sort -n | paste -sd, -
}

# _fetch_thread_comment_ids <thread-id>
# Echoes a normalized comma-separated list of comment databaseIds in the
# thread. Exits the calling shell on API error (so callers can rely on
# the output being authoritative).
_fetch_thread_comment_ids() {
  local thread_id="$1"
  local query='query($threadId: ID!) {
    node(id: $threadId) {
      ... on PullRequestReviewThread {
        comments(first: 100) {
          nodes { databaseId }
        }
      }
    }
  }'
  local payload result ids
  payload=$(jq -n --arg q "$query" --arg tid "$thread_id" \
    '{query: $q, variables: {threadId: $tid}}')
  result=$(_auth_curl \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "https://api.github.com/graphql" 2>/dev/null)
  if echo "$result" | jq -e '.errors' &>/dev/null; then
    echo "[comment-resolve] Thread state fetch failed:" >&2
    echo "$result" | jq -r '.errors[] | "  " + .message' >&2 2>/dev/null || true
    exit 1
  fi
  ids=$(echo "$result" | jq -r '.data.node.comments.nodes[]?.databaseId' \
    | sed '/^$/d' | sort -n | paste -sd, -)
  printf '%s' "$ids"
}

# _diff_id_sets <expected> <actual>
# Prints "added: ..." and "missing: ..." lines for any divergence.
# Returns 0 if sets are equal, 1 otherwise.
_diff_id_sets() {
  local expected="$1" actual="$2"
  local exp_norm act_norm
  exp_norm=$(_normalize_id_list "$expected")
  act_norm=$(_normalize_id_list "$actual")
  if [[ "$exp_norm" == "$act_norm" ]]; then
    return 0
  fi
  local added missing
  added=$(comm -13 \
    <(printf '%s\n' "$exp_norm" | tr ',' '\n' | sed '/^$/d' | sort -u) \
    <(printf '%s\n' "$act_norm" | tr ',' '\n' | sed '/^$/d' | sort -u))
  missing=$(comm -23 \
    <(printf '%s\n' "$exp_norm" | tr ',' '\n' | sed '/^$/d' | sort -u) \
    <(printf '%s\n' "$act_norm" | tr ',' '\n' | sed '/^$/d' | sort -u))
  [[ -n "$added" ]]   && echo "  added in thread:   $(printf '%s' "$added"   | tr '\n' ',' | sed 's/,$//')" >&2
  [[ -n "$missing" ]] && echo "  missing from thread: $(printf '%s' "$missing" | tr '\n' ',' | sed 's/,$//')" >&2
  return 1
}

# ---------------------------------------------------------------------------
# Step 1: Verify thread hasn't drifted since the gate snapshot
# ---------------------------------------------------------------------------
PRE_REPLY_IDS=$(_fetch_thread_comment_ids "$THREAD_ID")

if ! _diff_id_sets "$PRIOR_IDS" "$PRE_REPLY_IDS"; then
  cat >&2 <<EOF
[comment-resolve] Refused: thread $THREAD_ID drifted since gate confirmation.
  prior snapshot:  $(_normalize_id_list "$PRIOR_IDS")
  current state:   $PRE_REPLY_IDS
The user's confirmation was bound to the prior thread state. Re-triage with
the new comments and re-prompt the user before retrying.
EOF
  exit 4
fi

# ---------------------------------------------------------------------------
# Step 2: Post the reply (REST). Capture the new comment's databaseId for
# the post-reply verification.
# ---------------------------------------------------------------------------
REPLY_BODY=$(jq -n --arg body "$REPLY_MSG" '{body: $body}')
RESPONSE_FILE=$(umask 077 && mktemp "${TMPDIR:-/tmp}/.pr-watch-reply.XXXXXX")
trap 'rm -f "$RESPONSE_FILE"' EXIT

HTTP=$(_auth_curl -o "$RESPONSE_FILE" -w "%{http_code}" -X POST \
  -d "$REPLY_BODY" \
  "https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/comments/$COMMENT_DB_ID/replies" \
  2>/dev/null || echo "000")

if [[ "$HTTP" != "201" ]]; then
  echo "[comment-resolve] Error: reply returned HTTP $HTTP (PR #$PR_NUMBER, comment $COMMENT_DB_ID)." >&2
  echo "[comment-resolve] Thread NOT resolved — reply must land before the thread closes." >&2
  exit 2
fi

NEW_REPLY_ID=$(jq -r '.id // empty' "$RESPONSE_FILE")
if [[ -z "$NEW_REPLY_ID" ]]; then
  echo "[comment-resolve] Error: reply POST succeeded (HTTP 201) but response had no .id; cannot verify thread state." >&2
  echo "[comment-resolve] Reply is visible; thread still open. Manual review needed." >&2
  exit 3
fi
echo "[comment-resolve] Replied to comment $COMMENT_DB_ID on PR #$PR_NUMBER (new reply id $NEW_REPLY_ID)"

if [[ "$NO_RESOLVE" == "1" ]]; then
  echo "[comment-resolve] --no-resolve set; leaving thread $THREAD_ID open."
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 3: Verify thread state == prior ∪ {our new reply}, then resolve.
# Anything else means a third comment arrived between our reply and now;
# treat that as drift and refuse to resolve.
# ---------------------------------------------------------------------------
EXPECTED_AFTER=$(_normalize_id_list "$PRIOR_IDS,$NEW_REPLY_ID")
POST_REPLY_IDS=$(_fetch_thread_comment_ids "$THREAD_ID")

if ! _diff_id_sets "$EXPECTED_AFTER" "$POST_REPLY_IDS"; then
  cat >&2 <<EOF
[comment-resolve] Reply posted, but thread $THREAD_ID drifted during our work.
  expected:  $EXPECTED_AFTER  (prior + our reply)
  current:   $POST_REPLY_IDS
Refusing to resolve. Reply is visible; thread stays open. Re-evaluate the
new comments and re-confirm before any further action on this thread.
EOF
  exit 5
fi

MUTATION='mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { isResolved }
  }
}'

RESULT=$(_auth_curl \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg q "$MUTATION" --arg tid "$THREAD_ID" '{query: $q, variables: {threadId: $tid}}')" \
  "https://api.github.com/graphql" 2>/dev/null)

RESOLVED=$(echo "$RESULT" | jq -r '.data.resolveReviewThread.thread.isResolved // "error"')

if [[ "$RESOLVED" == "true" ]]; then
  echo "[comment-resolve] Resolved thread $THREAD_ID"
  exit 0
else
  echo "[comment-resolve] Warning: reply posted, but resolve mutation failed for $THREAD_ID (result: $RESOLVED)." >&2
  echo "[comment-resolve] Reply is visible on the PR; thread is still open. Retry resolve separately if needed." >&2
  echo "$RESULT" | jq '.errors // empty' >&2 2>/dev/null || true
  exit 3
fi
