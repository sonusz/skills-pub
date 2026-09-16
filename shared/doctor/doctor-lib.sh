#!/usr/bin/env bash
# doctor-lib.sh -- shared output + counters for skill doctor scripts.
#
# Source it; do not execute it. Works under macOS system bash 3.2.
#
#   . "$(dirname "${BASH_SOURCE[0]}")/../shared/doctor/doctor-lib.sh"
#
#   doctor_section "Dependencies"        # blank line + "Dependencies:"
#   doctor_require_cmd jq "brew install jq"
#   doctor_require_file "$HOME/.claude/settings.json"
#   doctor_pass "credential helper returned a token"
#   doctor_warn "working tree has uncommitted changes"
#   doctor_fail "no git remote found"
#   doctor_note "Add a remote: git remote add origin <url>"   # indented hint
#   doctor_summary                        # === Results: N passed, ... ===; rc 1 if failures
#
# Output format (fixed -- other tooling greps for it):
#   [OK]   msg
#   [WARN] msg
#   [FAIL] msg
#
# Counters live in DOCTOR_PASS / DOCTOR_WARN / DOCTOR_FAIL. A doctor that runs
# another doctor as a subprocess does not inherit its counters; print the
# child's output, keep its exit code, and summarize only your own checks.

if [ -n "${DOCTOR_LIB_LOADED:-}" ]; then
  return 0 2>/dev/null || exit 0
fi
DOCTOR_LIB_LOADED=1

DOCTOR_PASS=0
DOCTOR_WARN=0
DOCTOR_FAIL=0
DOCTOR_SECTIONS=0

# doctor_section TITLE -- prints "TITLE:" preceded by a blank line (except
# for the first section, which follows the caller's own banner).
doctor_section() {
  if [ "$DOCTOR_SECTIONS" -gt 0 ]; then
    echo ""
  fi
  DOCTOR_SECTIONS=$((DOCTOR_SECTIONS + 1))
  echo "$1:"
}

doctor_pass() { echo "  [OK]   $1"; DOCTOR_PASS=$((DOCTOR_PASS + 1)); }
doctor_warn() { echo "  [WARN] $1"; DOCTOR_WARN=$((DOCTOR_WARN + 1)); }
doctor_fail() { echo "  [FAIL] $1"; DOCTOR_FAIL=$((DOCTOR_FAIL + 1)); }

# doctor_note MSG -- an indented hint under the previous line; not counted.
doctor_note() { echo "         $1"; }

# doctor_require_cmd NAME [HINT] -- pass (with the first non-empty line of
# `NAME --version` when the command supports it) or fail + hint.
doctor_require_cmd() {
  local name="$1" hint="${2:-}" ver=""
  if command -v "$name" >/dev/null 2>&1; then
    ver="$("$name" --version </dev/null 2>/dev/null | awk 'NF { print; exit }')" || ver=""
    if [ -n "$ver" ]; then
      doctor_pass "$name ($ver)"
    else
      doctor_pass "$name present"
    fi
  else
    doctor_fail "$name not found"
    if [ -n "$hint" ]; then
      doctor_note "$hint"
    fi
  fi
}

# doctor_require_file PATH [HINT] -- pass if PATH exists (file, dir, or
# symlink target), else fail + hint.
doctor_require_file() {
  local path="$1" hint="${2:-}"
  if [ -e "$path" ]; then
    doctor_pass "$path"
  else
    doctor_fail "$path not found"
    if [ -n "$hint" ]; then
      doctor_note "$hint"
    fi
  fi
}

# doctor_summary -- print the totals line; return 1 if any check failed.
doctor_summary() {
  echo ""
  echo "=== Results: $DOCTOR_PASS passed, $DOCTOR_WARN warning(s), $DOCTOR_FAIL failure(s) ==="
  [ "$DOCTOR_FAIL" -eq 0 ]
}
