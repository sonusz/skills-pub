#!/bin/bash
# doctor.sh -- diagnose pr-watch environment issues
#
# Run this when pr-watch scripts aren't working.
# It checks every dependency, credential, and permission needed.
#
# Usage:
#   bash scripts/doctor.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
WARN=0
FAIL=0

pass() { echo "  [OK]   $1"; PASS=$((PASS + 1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN + 1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }

echo "=== pr-watch doctor ==="
echo ""

# 1. Git repo check
echo "Git repository:"
if git rev-parse --show-toplevel &>/dev/null; then
  pass "Inside a git repository"
else
  fail "Not inside a git repository"
  echo "         Run this from within a git repo."
  echo ""
  echo "=== $FAIL problem(s) found ==="
  exit 1
fi

# 2. GitHub remote check
echo ""
echo "GitHub remote:"
REMOTE_URL=$(git remote get-url origin 2>/dev/null || git remote get-url "$(git remote | head -n1)" 2>/dev/null || true)
GITHUB_CONTEXT_READY=0
if [[ -z "$REMOTE_URL" ]]; then
  fail "No git remote found"
  echo "         Add a remote: git remote add origin <url>"
elif source "$SCRIPT_DIR/github-remote.sh" 2>/dev/null; then
  pass "GitHub remote: $GITHUB_OWNER/$GITHUB_REPO"
  GITHUB_CONTEXT_READY=1
else
  fail "Remote is not GitHub: $REMOTE_URL"
  echo "         pr-watch only works with GitHub repositories."
fi

# 3. Dependencies
echo ""
echo "Dependencies:"
for dep in curl jq git; do
  if command -v "$dep" &>/dev/null; then
    pass "$dep ($( "$dep" --version 2>&1 | head -n1 ))"
  else
    fail "$dep not found"
    case "$dep" in
      jq)   echo "         Install: brew install jq  OR  apt install jq" ;;
      curl) echo "         Install: brew install curl  OR  apt install curl" ;;
    esac
  fi
done

SKILLS_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [[ -f "$SKILLS_ROOT/auto-fix/SKILL.md" ]]; then
  pass "auto-fix skill found"
else
  warn "auto-fix skill not found next to pr-watch-auto"
  echo "         Fix/comment automation requires the auto-fix skill."
fi

# 4. Git credential helper
echo ""
echo "GitHub authentication:"
if [[ "$GITHUB_CONTEXT_READY" != "1" ]]; then
  fail "Skipped credential check because GitHub remote detection failed"
elif _init_github_auth 2>/dev/null; then
  _source_github_auth
  trap '_cleanup_github_auth 2>/dev/null || true' EXIT
  pass "Credential helper returned a token"

  # 5. Test API access with the token
  echo ""
  echo "API access:"
  USER_HTTP=$(_auth_curl -o /dev/null -w "%{http_code}" \
    "https://api.github.com/user" 2>/dev/null || echo "000")
  if [[ "$USER_HTTP" == "200" ]]; then
    pass "GitHub identity endpoint accessible"
  else
    warn "GitHub identity endpoint returned HTTP $USER_HTTP"
    echo "         Fine-grained repo tokens may not allow /user; repo checks below are authoritative."
  fi

  PR_HTTP=$(_auth_curl -o /dev/null -w "%{http_code}" \
    "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/pulls?per_page=1" 2>/dev/null || echo "000")
  if [[ "$PR_HTTP" == "200" ]]; then
    pass "Pull requests API accessible"
  else
    fail "Pull requests API returned HTTP $PR_HTTP"
    echo "         PAT needs 'Pull requests: Read' permission."
  fi

  ACTIONS_HTTP=$(_auth_curl -o /dev/null -w "%{http_code}" \
    "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/actions/runs?per_page=1" 2>/dev/null || echo "000")
  if [[ "$ACTIONS_HTTP" == "200" ]]; then
    pass "Actions API accessible"
  else
    fail "Actions API returned HTTP $ACTIONS_HTTP"
    echo "         PAT needs 'Actions: Read' permission."
  fi
else
  fail "No credential found for github.com"
  echo "         Configure: git config --global credential.helper osxkeychain"
  echo "         Then store a fine-grained PAT with these permissions:"
  echo "           - Pull requests: Read"
  echo "           - Actions: Read"
  echo "           - Contents: Read"
fi

# Summary
echo ""
echo "=== Results: $PASS passed, $WARN warning(s), $FAIL failure(s) ==="

if [[ "$FAIL" -gt 0 ]]; then
  echo ""
  echo "Fix the failures above, then re-run: bash scripts/doctor.sh"
  exit 1
fi
