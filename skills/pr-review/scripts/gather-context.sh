#!/usr/bin/env bash
# gather-context.sh -- build a unified review-context bundle
#
# Two modes:
#   --branches <base> <head>    Local two-branch diff (no network)
#   --pr <number>               GitHub PR (fetches head, no checkout)
#
# What this script does (and what it deliberately doesn't):
#
#   * It produces a verbatim list of files changed in the diff, the diff
#     itself, and the diff-stat. That's it.
#   * It does NOT classify, filter, or interpret. No "skip lockfiles", no
#     "exclude node_modules", no "is this a doc". Repo conventions vary;
#     hardcoded heuristics always have corner cases (a vendored dir might
#     contain the bug being investigated; a lockfile change might BE the
#     point of the PR). The agent reads the file list, sees the diff, and
#     decides what's anchor-doc vs implementation vs test vs noise. The
#     agent has full context this script lacks — let it judge.
#
# Output: writes structured files into --out <dir>:
#   mode             "branches" or "pr"
#   pr_number        PR number (pr mode only)
#   head_sha         SHA of head commit
#   merge_base_sha   merge-base used for the review diff
#   repo_root        absolute audit root reviewers use as --cwd
#   base_ref         base branch name (origin/ prefix stripped)
#   head_ref         head branch name (origin/ prefix stripped)
#   diff.patch       full unified diff
#   diff-stat.txt    git diff --stat output
#   files.txt        all changed files (added or modified — not deleted),
#                    sorted alphabetically. No filtering. The agent decides
#                    which are anchor docs, implementation, tests, binaries,
#                    or generated noise.
#   summary.json     machine-readable summary
#
# Usage:
#   bash scripts/gather-context.sh --branches main feature/foo --out /tmp/run
#   bash scripts/gather-context.sh --pr 21 --out /tmp/run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

usage() {
  cat <<EOF >&2
Usage:
  $0 --branches <base> <head> --out <dir>
  $0 --pr <number> --out <dir>
EOF
  exit 1
}

MODE=""
BASE=""
HEAD=""
PR_NUMBER=""
OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branches)
      MODE="branches"
      BASE="${2:-}"
      HEAD="${3:-}"
      [[ -z "$BASE" || -z "$HEAD" ]] && usage
      shift 3
      ;;
    --pr)
      MODE="pr"
      PR_NUMBER="${2:-}"
      [[ -z "$PR_NUMBER" ]] && usage
      shift 2
      ;;
    --out)
      OUT="${2:-}"
      [[ -z "$OUT" ]] && usage
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      ;;
  esac
done

[[ -z "$MODE" || -z "$OUT" ]] && usage

mkdir -p "$OUT"
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
  echo "Error: current directory is not inside a git repository." >&2
  exit 2
}
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"

# ---------------------------------------------------------------------------
# Resolve base / head SHAs
# ---------------------------------------------------------------------------
if [[ "$MODE" == "branches" ]]; then
  if ! git rev-parse --verify "$BASE" >/dev/null 2>&1; then
    echo "Error: base ref '$BASE' not found locally. Did you fetch?" >&2
    exit 2
  fi
  if ! git rev-parse --verify "$HEAD" >/dev/null 2>&1; then
    echo "Error: head ref '$HEAD' not found locally. Did you fetch?" >&2
    exit 2
  fi
  BASE_SHA=$(git rev-parse "$BASE")
  HEAD_SHA=$(git rev-parse "$HEAD")
  BASE_REF="${BASE#origin/}"
  HEAD_REF="${HEAD#origin/}"
else
  source "$SKILL_DIR/shared/github-ops/github-remote.sh"
  if ! _init_github_auth >/dev/null; then
    echo "Error: GitHub credential helper failed. Run shared/github-ops/doctor.sh." >&2
    exit 2
  fi
  _source_github_auth
  trap _cleanup_github_auth EXIT

  PR_JSON=$(_auth_curl "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/pulls/$PR_NUMBER")
  if echo "$PR_JSON" | jq -e '.message' >/dev/null 2>&1; then
    echo "Error: GitHub API returned: $(echo "$PR_JSON" | jq -r .message)" >&2
    exit 2
  fi
  HEAD_SHA=$(echo "$PR_JSON" | jq -r '.head.sha')
  HEAD_REF=$(echo "$PR_JSON" | jq -r '.head.ref')
  BASE_REF=$(echo "$PR_JSON" | jq -r '.base.ref')

  # Fetch base and head separately. `git fetch origin A B` is atomic — one
  # missing ref fails both. The head branch may have been deleted (e.g. after
  # an auto-merge / squash) even though the PR is still open, so fetch each
  # ref individually and fall back to refs/pull/N/head, which is always
  # available while the PR is open (works for forks too).
  git fetch --quiet origin "$BASE_REF" 2>/dev/null || true
  git fetch --quiet origin "$HEAD_REF" 2>/dev/null || true
  if ! git rev-parse --verify "$HEAD_SHA" >/dev/null 2>&1; then
    git fetch --quiet origin "pull/$PR_NUMBER/head" 2>/dev/null || true
  fi
  if ! git rev-parse --verify "$HEAD_SHA" >/dev/null 2>&1; then
    echo "Error: could not fetch head SHA $HEAD_SHA — token may lack access to this PR." >&2
    exit 2
  fi
  BASE_SHA=$(git rev-parse "origin/$BASE_REF" 2>/dev/null || git rev-parse "$BASE_REF" 2>/dev/null || true)
  if [[ -z "$BASE_SHA" ]]; then
    echo "Error: base ref '$BASE_REF' did not resolve locally after fetch." >&2
    exit 2
  fi
fi

# Use the merge-base so the diff reflects what the PR adds, not unrelated
# changes on base since the branch diverged.
MERGE_BASE=$(git merge-base "$BASE_SHA" "$HEAD_SHA" 2>/dev/null || echo "$BASE_SHA")

# ---------------------------------------------------------------------------
# Produce diff + a single, lightly-filtered file list
# ---------------------------------------------------------------------------
git diff "$MERGE_BASE..$HEAD_SHA" > "$OUT/diff.patch"
git diff --stat "$MERGE_BASE..$HEAD_SHA" > "$OUT/diff-stat.txt"

# All changed files (added or modified — not deleted), sorted. No filtering.
# The agent classifies these from the file list and the diff content.
git diff --name-only --diff-filter=AM "$MERGE_BASE..$HEAD_SHA" | sort -u > "$OUT/files.txt"

# ---------------------------------------------------------------------------
# Write metadata
# ---------------------------------------------------------------------------
echo "$MODE" > "$OUT/mode"
[[ "$MODE" == "pr" ]] && echo "$PR_NUMBER" > "$OUT/pr_number"
echo "$HEAD_SHA" > "$OUT/head_sha"
echo "$MERGE_BASE" > "$OUT/merge_base_sha"
echo "$REPO_ROOT" > "$OUT/repo_root"
echo "$BASE_REF" > "$OUT/base_ref"
echo "$HEAD_REF" > "$OUT/head_ref"

FILE_COUNT=$(wc -l < "$OUT/files.txt" | tr -d ' ')
DIFF_BYTES=$(wc -c < "$OUT/diff.patch" | tr -d ' ')

jq -n \
  --arg mode "$MODE" \
  --arg pr_number "${PR_NUMBER:-}" \
  --arg head_sha "$HEAD_SHA" \
  --arg merge_base_sha "$MERGE_BASE" \
  --arg repo_root "$REPO_ROOT" \
  --arg base_ref "$BASE_REF" \
  --arg head_ref "$HEAD_REF" \
  --argjson file_count "$FILE_COUNT" \
  --argjson diff_bytes "$DIFF_BYTES" \
  --rawfile files "$OUT/files.txt" \
  '{
    mode: $mode,
    pr_number: (if $pr_number == "" then null else ($pr_number | tonumber) end),
    head_sha: $head_sha,
    merge_base_sha: $merge_base_sha,
    repo_root: $repo_root,
    base_ref: $base_ref,
    head_ref: $head_ref,
    file_count: $file_count,
    diff_bytes: $diff_bytes,
    files: ($files | split("\n") | map(select(length > 0)))
  }' > "$OUT/summary.json"

echo "Context bundle written to: $OUT"
echo ""
jq '{mode, pr_number, head_sha, merge_base_sha, repo_root, base_ref, head_ref, file_count, diff_bytes}' "$OUT/summary.json"
