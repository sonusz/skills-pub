#!/bin/bash
# doctor.sh -- diagnose GitHub-ops environment issues
#
# Run this when GitHub-ops scripts (pr-watch-auto and friends) aren't working.
# It checks every dependency, credential, and permission needed.
#
# Usage (from any skill that symlinks shared/):
#   bash shared/github-ops/doctor.sh
#
# Output helpers come from shared/doctor/doctor-lib.sh (sibling module).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ../doctor resolves to the consuming skill's shared/doctor link when this
# module is reached through skills/<skill>/shared/github-ops; fall back to the
# physical location for a bare checkout.
DOCTOR_LIB="$SCRIPT_DIR/../doctor/doctor-lib.sh"
[[ -f "$DOCTOR_LIB" ]] || DOCTOR_LIB="$(cd "$SCRIPT_DIR" && pwd -P)/../doctor/doctor-lib.sh"
if [[ ! -f "$DOCTOR_LIB" ]]; then
  echo "doctor.sh: cannot find shared/doctor/doctor-lib.sh next to shared/github-ops" >&2
  exit 2
fi
# shellcheck source=../doctor/doctor-lib.sh
. "$DOCTOR_LIB"

echo "=== github-ops doctor ==="
echo ""

# 1. Git repo check
doctor_section "Git repository"
if git rev-parse --show-toplevel &>/dev/null; then
  doctor_pass "Inside a git repository"
else
  doctor_fail "Not inside a git repository"
  doctor_note "Run this from within a git repo."
  doctor_summary || true
  exit 1
fi

# 2. GitHub remote check
doctor_section "GitHub remote"
REMOTE_URL=$(git remote get-url origin 2>/dev/null || git remote get-url "$(git remote | head -n1)" 2>/dev/null || true)
GITHUB_CONTEXT_READY=0
if [[ -z "$REMOTE_URL" ]]; then
  doctor_fail "No git remote found"
  doctor_note "Add a remote: git remote add origin <url>"
elif source "$SCRIPT_DIR/github-remote.sh" 2>/dev/null; then
  doctor_pass "GitHub remote: $GITHUB_OWNER/$GITHUB_REPO"
  GITHUB_CONTEXT_READY=1
else
  doctor_fail "Remote is not GitHub: $REMOTE_URL"
  doctor_note "These primitives only work with GitHub repositories."
fi

# 3. Dependencies
doctor_section "Dependencies"
for dep in curl jq git; do
  case "$dep" in
    jq)   HINT="Install: brew install jq  OR  apt install jq" ;;
    curl) HINT="Install: brew install curl  OR  apt install curl" ;;
    *)    HINT="" ;;
  esac
  doctor_require_cmd "$dep" "$HINT"
done

# 4. Git credential helper
doctor_section "GitHub authentication"
if [[ "$GITHUB_CONTEXT_READY" != "1" ]]; then
  doctor_fail "Skipped credential check because GitHub remote detection failed"
elif _init_github_auth 2>/dev/null; then
  _source_github_auth
  trap '_cleanup_github_auth 2>/dev/null || true' EXIT
  doctor_pass "Credential helper returned a token"

  # 5. Test API access with the token
  doctor_section "API access"
  USER_HTTP=$(_auth_curl -o /dev/null -w "%{http_code}" \
    "https://api.github.com/user" 2>/dev/null || echo "000")
  if [[ "$USER_HTTP" == "200" ]]; then
    doctor_pass "GitHub identity endpoint accessible"
  else
    doctor_warn "GitHub identity endpoint returned HTTP $USER_HTTP"
    doctor_note "Fine-grained repo tokens may not allow /user; repo checks below are authoritative."
  fi

  PR_HTTP=$(_auth_curl -o /dev/null -w "%{http_code}" \
    "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/pulls?per_page=1" 2>/dev/null || echo "000")
  if [[ "$PR_HTTP" == "200" ]]; then
    doctor_pass "Pull requests API accessible"
  else
    doctor_fail "Pull requests API returned HTTP $PR_HTTP"
    doctor_note "PAT needs 'Pull requests: Read' permission."
  fi

  # Issues are a separate fine-grained permission from Pull requests, and the
  # two are easy to confuse because `/issues` also returns PRs. A token that
  # reads PRs perfectly well can still be 403 here — which is invisible until
  # create-issues.sh refuses.
  ISSUES_HTTP=$(_auth_curl -o /dev/null -w "%{http_code}" \
    "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/issues?per_page=1&state=all" 2>/dev/null || echo "000")
  if [[ "$ISSUES_HTTP" == "200" ]]; then
    doctor_pass "Issues API accessible"
  else
    doctor_fail "Issues API returned HTTP $ISSUES_HTTP"
    doctor_note "PAT needs 'Issues: Read and write' permission (create-issues.sh needs write)."
  fi

  ACTIONS_HTTP=$(_auth_curl -o /dev/null -w "%{http_code}" \
    "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/actions/runs?per_page=1" 2>/dev/null || echo "000")
  if [[ "$ACTIONS_HTTP" == "200" ]]; then
    doctor_pass "Actions API accessible"
  else
    doctor_fail "Actions API returned HTTP $ACTIONS_HTTP"
    doctor_note "PAT needs 'Actions: Read' permission."
  fi
else
  doctor_fail "No credential found for github.com"
  doctor_note "Configure: git config --global credential.helper osxkeychain"
  doctor_note "Then store a fine-grained PAT with these permissions:"
  doctor_note "  - Pull requests: Read"
  doctor_note "  - Actions: Read"
  doctor_note "  - Contents: Read"
fi

# Summary
if ! doctor_summary; then
  echo ""
  echo "Fix the failures above, then re-run this doctor."
  exit 1
fi
