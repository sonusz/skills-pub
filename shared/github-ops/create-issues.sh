#!/usr/bin/env bash
# create-issues.sh <markdown-file> [--dry-run] [--repo owner/repo]
#                                  [--label LABEL]... [--state-filter open|all]
#
# Creates one GitHub issue per `## ` heading in a markdown file, using the PAT
# the git credential helper already holds. No `gh` login required — this is the
# whole point: `git` cannot create issues (they are not in the repository), but
# the credential git pushes with is usually enough to call the REST API.
#
# Input format, deliberately boring:
#
#   anything before the first `## ` is a preamble and is IGNORED
#   ## Title of the first issue          <- becomes the issue title
#   body lines...                        <- becomes the issue body
#   <!-- labels: bug, deploy-gated -->   <- optional, stripped from the body
#   ## Title of the second issue
#   ...
#
# A leading `#<digits> ` in a heading is STRIPPED from the title: backfill
# documents tend to number their entries, and GitHub would render a literal
# "#3" in a title as a cross-link to issue 3 — a wrong link, permanently.
# So `## #3 Fix the thing` creates the issue titled `Fix the thing`.
#
# IDEMPOTENT. Existing issues (open and closed) are listed first and any entry
# whose title matches one exactly is skipped, so re-running after a partial
# failure does not duplicate. Titles are the identity; editing a title in the
# input file and re-running creates a second issue.
#
# Output: one JSON line per entry, then a summary line, all on stdout:
#   {"title":"Fix the thing","action":"created","number":12,"url":"https://..."}
#   {"title":"Old thing","action":"skipped","number":4,"reason":"title exists"}
#   {"summary":{"created":11,"skipped":1,"failed":0}}
#
# Exit codes: 0 all entries created or skipped; 1 at least one create failed;
# 2 usage or precondition error (no file, no auth, no Issues permission).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INPUT=""
DRY_RUN=0
REPO_OVERRIDE=""
declare -a COMMON_LABELS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --repo)    REPO_OVERRIDE="${2:-}"; shift 2 ;;
    --label)   COMMON_LABELS+=("${2:-}"); shift 2 ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*)        echo "create-issues.sh: unknown option $1" >&2; exit 2 ;;
    *)         if [[ -n "$INPUT" ]]; then echo "create-issues.sh: more than one input file" >&2; exit 2; fi
               INPUT="$1"; shift ;;
  esac
done

if [[ -z "$INPUT" ]]; then
  echo "usage: create-issues.sh <markdown-file> [--dry-run] [--repo owner/repo] [--label L]..." >&2
  exit 2
fi
if [[ ! -f "$INPUT" ]]; then
  echo "create-issues.sh: no such file: $INPUT" >&2
  exit 2
fi
for dep in curl jq awk; do
  command -v "$dep" >/dev/null || { echo "create-issues.sh: missing dependency: $dep" >&2; exit 2; }
done

# shellcheck source=./github-remote.sh
source "$SCRIPT_DIR/github-remote.sh"
if [[ -n "$REPO_OVERRIDE" ]]; then
  GITHUB_OWNER="${REPO_OVERRIDE%%/*}"
  GITHUB_REPO="${REPO_OVERRIDE##*/}"
fi
API="https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO"

_init_github_auth || { echo "create-issues.sh: git credential helper returned no token for github.com" >&2; exit 2; }
_source_github_auth

WORK="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/create-issues.XXXXXX")"
trap 'rm -rf "$WORK"; _cleanup_github_auth' EXIT

# --- split the markdown into one file per entry -----------------------------
# Entry N lands in $WORK/entry-N.title and $WORK/entry-N.body. awk rather than
# a bash read-loop so a body line beginning with a dash or a backslash cannot
# be reinterpreted.
awk -v dir="$WORK" '
  /^## / {
    n++
    title = substr($0, 4)
    sub(/^#[0-9]+ +/, "", title)         # drop a backfill index like "#3 "
    sub(/[ \t]+$/, "", title)
    printf "%s", title > (dir "/entry-" n ".title")
    close(dir "/entry-" n ".title")
    next
  }
  n > 0 { print >> (dir "/entry-" n ".body") }
  END { print n+0 > (dir "/count") }
' "$INPUT"

COUNT="$(cat "$WORK/count")"
if [[ "$COUNT" -eq 0 ]]; then
  echo "create-issues.sh: $INPUT contains no '## ' heading — nothing to create" >&2
  exit 2
fi

# --- preflight: can this token see issues at all? ---------------------------
# A dry run is allowed to proceed without it: checking that the input parses is
# exactly what you want BEFORE going to fetch a permission, and refusing there
# would make the flag useless in the one situation it is for.
probe="$(_auth_curl -o /dev/null -w '%{http_code}' "$API/issues?per_page=1&state=all")"
if [[ "$probe" != "200" ]]; then
  {
    if [[ "$probe" == "403" || "$probe" == "404" ]]; then
      echo "create-issues.sh: the token cannot read issues on $GITHUB_OWNER/$GITHUB_REPO (HTTP $probe)."
      echo "  A fine-grained PAT needs 'Issues: Read and write' on this repository."
      echo "  github.com/settings/personal-access-tokens -> this token -> Repository permissions -> Issues"
      echo "  (A classic token needs the 'repo' scope.)"
    else
      echo "create-issues.sh: unexpected HTTP $probe listing issues on $GITHUB_OWNER/$GITHUB_REPO."
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "  --dry-run continues anyway; every entry will read as new because"
      echo "  the existing titles could not be fetched."
    else
      echo "  Nothing was created."
    fi
  } >&2
  [[ "$DRY_RUN" -eq 1 ]] || exit 2
fi

# --- existing titles, open and closed, all pages ----------------------------
: > "$WORK/existing.json"
if [[ "$probe" == "200" ]]; then
  page=1
  while :; do
    _auth_curl "$API/issues?state=all&per_page=100&page=$page" > "$WORK/page.json"
    got="$(jq 'length' "$WORK/page.json")"
    # Pull requests come back on this endpoint too; they share the number space
    # and a PR title must not shadow an issue title, so drop them.
    jq -c '.[] | select(has("pull_request") | not) | {number, title}' "$WORK/page.json" >> "$WORK/existing.json"
    [[ "$got" -lt 100 ]] && break
    page=$((page + 1))
  done
fi

created=0; skipped=0; failed=0

# --- make sure every label exists before any issue references one -----------
# GitHub does create a missing label when an issue names it, but only for a
# token with push access, and it is silently dropped otherwise — a difference
# that shows up as issues quietly missing their labels. Creating them up front
# turns that into an explicit 201/422 we can see. 422 means it already exists.
_ensure_label() {
  local name="$1" code
  [[ -n "$name" ]] || return 0
  code="$(jq -nc --arg n "$name" '{name:$n, color:"ededed"}' \
    | _auth_curl -o /dev/null -w '%{http_code}' -X POST \
        -H "Content-Type: application/json" --data-binary @- "$API/labels")"
  case "$code" in
    201) echo "create-issues.sh: created label '$name'" >&2 ;;
    422) : ;;  # already exists
    *)   echo "create-issues.sh: could not ensure label '$name' (HTTP $code); it may be dropped" >&2 ;;
  esac
}

for ((i = 1; i <= COUNT; i++)); do
  title="$(cat "$WORK/entry-$i.title")"
  [[ -f "$WORK/entry-$i.body" ]] || : > "$WORK/entry-$i.body"

  # Per-entry labels from an HTML comment, then stripped from the body.
  # No `mapfile` and no `sed -i`: macOS ships Bash 3.2, which has neither that
  # builtin nor GNU sed's in-place form (BSD sed -i demands a suffix argument).
  entry_labels=()
  while IFS= read -r _label; do
    [[ -n "$_label" ]] && entry_labels+=("$_label")
  done < <(
    sed -n 's/^<!-- *labels: *\(.*\) *-->$/\1/p' "$WORK/entry-$i.body" \
      | tr ',' '\n' | sed 's/^[ 	]*//; s/[ 	]*$//' | grep -v '^$' || true
  )
  awk '!/^<!-- *labels:.*-->$/' "$WORK/entry-$i.body" > "$WORK/entry-$i.body.tmp"
  mv "$WORK/entry-$i.body.tmp" "$WORK/entry-$i.body"

  existing_number="$(jq -rs --arg t "$title" 'map(select(.title == $t)) | if length > 0 then .[0].number else "" end' "$WORK/existing.json")"
  if [[ -n "$existing_number" ]]; then
    jq -nc --arg t "$title" --argjson n "$existing_number" \
      '{title:$t, action:"skipped", number:$n, reason:"title exists"}'
    skipped=$((skipped + 1))
    continue
  fi

  all_labels=("${COMMON_LABELS[@]:-}" "${entry_labels[@]:-}")
  jq -n --arg t "$title" --rawfile b "$WORK/entry-$i.body" \
        --args '{title:$t, body:$b, labels:($ARGS.positional | map(select(. != "")))}' \
        -- "${all_labels[@]:-}" > "$WORK/payload.json"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    jq -nc --arg t "$title" --argjson bytes "$(wc -c < "$WORK/entry-$i.body")" \
           --argjson l "$(jq '.labels' "$WORK/payload.json")" \
      '{title:$t, action:"would-create", body_bytes:$bytes, labels:$l}'
    created=$((created + 1))
    continue
  fi

  while IFS= read -r _needed; do _ensure_label "$_needed"; done \
    < <(jq -r '.labels[]' "$WORK/payload.json")

  code="$(_auth_curl -o "$WORK/resp.json" -w '%{http_code}' \
    -X POST -H "Content-Type: application/json" \
    --data-binary @"$WORK/payload.json" "$API/issues")"
  if [[ "$code" == "201" ]]; then
    jq -c --arg t "$title" '{title:$t, action:"created", number:.number, url:.html_url}' "$WORK/resp.json"
    created=$((created + 1))
  else
    jq -nc --arg t "$title" --arg c "$code" \
           --arg m "$(jq -r '.message // "no message"' "$WORK/resp.json" 2>/dev/null || echo 'unparseable response')" \
      '{title:$t, action:"failed", http:$c, message:$m}'
    failed=$((failed + 1))
  fi
done

jq -nc --argjson c "$created" --argjson s "$skipped" --argjson f "$failed" \
  '{summary:{created:$c, skipped:$s, failed:$f}}'

[[ "$failed" -eq 0 ]] || exit 1
