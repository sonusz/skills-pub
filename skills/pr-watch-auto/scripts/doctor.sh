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

# shellcheck source=../shared/doctor/doctor-lib.sh
. "$SKILL_DIR/shared/doctor/doctor-lib.sh"

# The shared doctor runs as a subprocess and prints its own summary; only its
# exit code carries over. The extras below are counted separately.
bash "$SKILL_DIR/shared/github-ops/doctor.sh"
SHARED_RC=$?

echo ""
echo "=== pr-watch-auto extras ==="
echo ""

doctor_section "Sibling skills"
if [[ -f "$SKILL_DIR/skills/auto-fix/SKILL.md" ]]; then
  doctor_pass "auto-fix skill found"
else
  doctor_warn "auto-fix skill not found at skills/auto-fix"
  doctor_note "Fix/comment automation requires the auto-fix skill."
fi

doctor_summary
EXTRA_RC=$?

if [[ "$SHARED_RC" -ne 0 ]] || [[ "$EXTRA_RC" -ne 0 ]]; then
  echo ""
  echo "Fix the failures above, then re-run: bash scripts/doctor.sh"
  exit 1
fi
