#!/usr/bin/env bash
# doctor.sh -- diagnose pr-review environment issues
#
# Wraps shared/github-ops/doctor.sh (generic GitHub-primitive checks) and
# adds pr-review-specific checks (panel-review sibling skill, jq for JSON,
# the shared secrets scan gather-context.sh relies on).
#
# Usage:
#   bash scripts/doctor.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_ROOT="$(cd "$SKILL_DIR/.." && pwd)"

# shellcheck source=../shared/doctor/doctor-lib.sh
. "$SKILL_DIR/shared/doctor/doctor-lib.sh"

# The shared doctor runs as a subprocess and prints its own summary; only its
# exit code carries over. The extras below are counted separately.
bash "$SKILL_DIR/shared/github-ops/doctor.sh"
SHARED_RC=$?

echo ""
echo "=== pr-review extras ==="
echo ""

# Sibling skills the agent depends on
doctor_section "Sibling skills"
if [[ -f "$SKILLS_ROOT/panel-review/SKILL.md" ]]; then
  doctor_pass "panel-review skill found"
else
  doctor_fail "panel-review skill not found at skills/panel-review"
  doctor_note "pr-review delegates to panel-review for both phases."
fi

# Local-mode requirements (gh mode is covered by shared doctor)
doctor_section "Local-mode dependencies"
if command -v jq >/dev/null 2>&1; then
  doctor_pass "jq present"
else
  doctor_fail "jq not found — required for context-summary and comment payloads"
fi

# Secret scan gather-context.sh runs over the diff (records hits in secrets.txt)
doctor_section "Secret scan"
if [[ -f "$SKILL_DIR/shared/secrets/scan.sh" ]]; then
  doctor_pass "shared/secrets/scan.sh present"
else
  doctor_warn "shared/secrets/scan.sh not found — gather-context.sh will mark the diff unscanned"
  doctor_note "Expected link: shared/secrets -> ../../../shared/secrets"
fi
if command -v perl >/dev/null 2>&1; then
  doctor_pass "perl present (required by scan.sh)"
else
  doctor_warn "perl not found — scan.sh cannot run; diffs will be marked unscanned"
fi

# A clean working tree is required before a review run, but doctor doesn't
# enforce it (the agent does). Just report status here so the user knows.
doctor_section "Working tree"
if git rev-parse --show-toplevel >/dev/null 2>&1; then
  if [[ -z "$(git status --porcelain 2>/dev/null)" ]]; then
    doctor_pass "clean"
  else
    doctor_warn "working tree has uncommitted changes — stash or commit before running pr-review"
  fi
else
  doctor_warn "not inside a git repository — local mode will fail"
fi

doctor_summary
EXTRA_RC=$?

if [[ "$SHARED_RC" -ne 0 ]] || [[ "$EXTRA_RC" -ne 0 ]]; then
  echo ""
  echo "Fix the failures above, then re-run: bash scripts/doctor.sh"
  exit 1
fi
