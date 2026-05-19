#!/bin/bash
# doctor.sh -- diagnose pr-watch-auto environment issues
#
# Wraps shared/github-ops/doctor.sh (generic GitHub-primitive checks) and
# adds pr-watch-auto-specific checks (auto-fix sub-skill presence).
#
# Usage:
#   bash scripts/doctor.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

bash "$SKILL_DIR/shared/github-ops/doctor.sh"
SHARED_RC=$?

echo ""
echo "=== pr-watch-auto extras ==="
echo ""
echo "Sibling skills:"
EXTRA_FAIL=0
if [[ -f "$SKILL_DIR/skills/auto-fix/SKILL.md" ]]; then
  echo "  [OK]   auto-fix skill found"
else
  echo "  [WARN] auto-fix skill not found at skills/auto-fix"
  echo "         Fix/comment automation requires the auto-fix skill."
fi

if [[ "$SHARED_RC" -ne 0 ]] || [[ "$EXTRA_FAIL" -ne 0 ]]; then
  echo ""
  echo "Fix the failures above, then re-run: bash scripts/doctor.sh"
  exit 1
fi
