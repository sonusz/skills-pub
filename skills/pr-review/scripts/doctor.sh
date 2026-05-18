#!/usr/bin/env bash
# doctor.sh -- diagnose pr-review environment issues
#
# Wraps shared/github-ops/doctor.sh (generic GitHub-primitive checks) and
# adds pr-review-specific checks (panel-review sibling skill, jq for JSON).
#
# Usage:
#   bash scripts/doctor.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_ROOT="$(cd "$SKILL_DIR/.." && pwd)"

bash "$SKILL_DIR/shared/github-ops/doctor.sh"
SHARED_RC=$?

echo ""
echo "=== pr-review extras ==="
echo ""

EXTRA_FAIL=0

# Sibling skills the agent depends on
echo "Sibling skills:"
if [[ -f "$SKILLS_ROOT/panel-review/SKILL.md" ]]; then
  echo "  [OK]   panel-review skill found"
else
  echo "  [FAIL] panel-review skill not found at skills/panel-review"
  echo "         pr-review delegates to panel-review for both phases."
  EXTRA_FAIL=1
fi

# Local-mode requirements (gh mode is covered by shared doctor)
echo ""
echo "Local-mode dependencies:"
if command -v jq >/dev/null 2>&1; then
  echo "  [OK]   jq present"
else
  echo "  [FAIL] jq not found — required for context-summary and comment payloads"
  EXTRA_FAIL=1
fi

# A clean working tree is required before a review run, but doctor doesn't
# enforce it (the agent does). Just report status here so the user knows.
echo ""
echo "Working tree:"
if git rev-parse --show-toplevel >/dev/null 2>&1; then
  if [[ -z "$(git status --porcelain 2>/dev/null)" ]]; then
    echo "  [OK]   clean"
  else
    echo "  [WARN] working tree has uncommitted changes — stash or commit before running pr-review"
  fi
else
  echo "  [WARN] not inside a git repository — local mode will fail"
fi

if [[ "$SHARED_RC" -ne 0 ]] || [[ "$EXTRA_FAIL" -ne 0 ]]; then
  echo ""
  echo "Fix the failures above, then re-run: bash scripts/doctor.sh"
  exit 1
fi
