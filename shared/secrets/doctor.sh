#!/usr/bin/env bash
# doctor.sh -- verify the secrets module works on this machine.
#
# Checks perl, that patterns.pl loads, and runs a self-test with synthetic
# secrets (built at runtime so no token-shaped string lives in this file):
# redact.sh must scrub them, scan.sh must report them with exit 1, and a clean
# line must pass both with exit 0. Nothing secret is ever printed.
#
# Usage:
#   bash shared/secrets/doctor.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DOCTOR_LIB="$SCRIPT_DIR/../doctor/doctor-lib.sh"
if [ ! -f "$DOCTOR_LIB" ]; then
  DOCTOR_LIB="$(cd "$SCRIPT_DIR" && pwd -P)/../doctor/doctor-lib.sh"
fi
if [ ! -f "$DOCTOR_LIB" ]; then
  echo "doctor.sh: cannot find shared/doctor/doctor-lib.sh next to shared/secrets" >&2
  exit 2
fi
# shellcheck source=../doctor/doctor-lib.sh
. "$DOCTOR_LIB"

REDACT="$SCRIPT_DIR/redact.sh"
SCAN="$SCRIPT_DIR/scan.sh"
PATTERNS="$SCRIPT_DIR/patterns.pl"

echo "=== secrets doctor ==="
echo ""

doctor_section "Dependencies"
doctor_require_cmd perl "macOS ships perl; otherwise: apt install perl  OR  brew install perl"
doctor_require_file "$REDACT"
doctor_require_file "$SCAN"
doctor_require_file "$PATTERNS"

if ! command -v perl >/dev/null 2>&1; then
  doctor_summary
  echo "Fix the failures above, then re-run this doctor."
  exit 1
fi

doctor_section "Patterns"
if perl -c "$PATTERNS" >/dev/null 2>&1; then
  doctor_pass "patterns.pl compiles"
else
  doctor_fail "patterns.pl does not compile (perl -c $PATTERNS)"
fi
COUNT="$(perl -e 'my $p = do shift; print ref $p eq "ARRAY" ? scalar(@$p) : 0' "$PATTERNS" 2>/dev/null)"
if [ "${COUNT:-0}" -gt 0 ] 2>/dev/null; then
  doctor_pass "patterns.pl loads ($COUNT patterns)"
else
  doctor_fail "patterns.pl did not return a non-empty array ref"
fi

doctor_section "Self-test"

# Synthetic secrets, built at runtime. Never echo them.
GHP="ghp_$(printf 'a%.0s' $(seq 1 30))"
AKIA="AKIA$(printf 'A%.0s' $(seq 1 16))"

# redact: token must vanish, marker must appear
OUT="$(printf 'token %s\n' "$GHP" | bash "$REDACT" 2>/dev/null)"
case "$OUT" in
  *"$GHP"*) doctor_fail "redact.sh left a synthetic GitHub PAT in place" ;;
  *"gh<redacted>"*) doctor_pass "redact.sh scrubs a synthetic GitHub PAT" ;;
  *) doctor_fail "redact.sh produced unexpected output for a GitHub PAT" ;;
esac
OUT="$(printf 'key=%s\n' "$AKIA" | bash "$REDACT" 2>/dev/null)"
case "$OUT" in
  *"$AKIA"*) doctor_fail "redact.sh left a synthetic AWS key in place" ;;
  *"AKIA<redacted>"*) doctor_pass "redact.sh scrubs a synthetic AWS access key" ;;
  *) doctor_fail "redact.sh produced unexpected output for an AWS key" ;;
esac

# scan: must report the hit by name with exit 1, never the value
OUT="$(printf 'token %s\n' "$GHP" | bash "$SCAN" 2>/dev/null)"; RC=$?
if [ "$RC" -eq 1 ] && [ "$OUT" = "-:1:github-pat" ]; then
  doctor_pass "scan.sh reports -:1:github-pat with exit 1"
else
  doctor_fail "scan.sh on a GitHub PAT: exit $RC, expected 1 with '-:1:github-pat'"
fi
OUT="$(printf 'clean line\nkey=%s\n' "$AKIA" | bash "$SCAN" 2>/dev/null)"; RC=$?
if [ "$RC" -eq 1 ] && [ "$OUT" = "-:2:aws-access-key" ]; then
  doctor_pass "scan.sh reports -:2:aws-access-key with exit 1"
else
  doctor_fail "scan.sh on an AWS key: exit $RC, expected 1 with '-:2:aws-access-key'"
fi
case "$OUT" in
  *"$AKIA"*) doctor_fail "scan.sh printed the matched secret" ;;
  *) doctor_pass "scan.sh output never contains the matched value" ;;
esac

# scan --diff: added line addressed by new-file path and line number
DIFF="$(printf 'diff --git a/cfg.env b/cfg.env\n--- a/cfg.env\n+++ b/cfg.env\n@@ -3,2 +5,3 @@\n ctx\n+key=%s\n ctx\n' "$AKIA")"
OUT="$(printf '%s\n' "$DIFF" | bash "$SCAN" --diff 2>/dev/null)"; RC=$?
if [ "$RC" -eq 1 ] && [ "$OUT" = "cfg.env:6:aws-access-key" ]; then
  doctor_pass "scan.sh --diff reports cfg.env:6:aws-access-key with exit 1"
else
  doctor_fail "scan.sh --diff: exit $RC, expected 1 with 'cfg.env:6:aws-access-key'"
fi

# clean input passes both untouched
OUT="$(printf 'hello world\n' | bash "$SCAN" 2>/dev/null)"; RC=$?
if [ "$RC" -eq 0 ] && [ -z "$OUT" ]; then
  doctor_pass "scan.sh exits 0 with no output on a clean line"
else
  doctor_fail "scan.sh on a clean line: exit $RC (expected 0, no output)"
fi
OUT="$(printf 'hello world\n' | bash "$REDACT" 2>/dev/null)"
if [ "$OUT" = "hello world" ]; then
  doctor_pass "redact.sh leaves a clean line unchanged"
else
  doctor_fail "redact.sh altered a clean line"
fi

if ! doctor_summary; then
  echo "Fix the failures above, then re-run this doctor."
  exit 1
fi
