#!/usr/bin/env bash
# redact-secrets.sh
#
# Thin wrapper: delegates to the shared secrets module
# (skills/auto-fix/shared/secrets -> shared/secrets). Kept so every path in
# SKILL.md stays valid: pipe evidence / error_log content through this script
# before it lands in the structured output.
#
# Contract (unchanged): stdin -> stdout with well-known secret patterns replaced
# by `<redacted>`; exit 1 with a message if perl is missing. The denylist is
# shared/secrets/patterns.pl -- a high-confidence list, not a guarantee.
#
# Usage:
#   echo "$evidence" | scripts/redact-secrets.sh
#   scripts/redact-secrets.sh < /path/to/build.log

exec "$(cd "$(dirname "${BASH_SOURCE[0]}")/../shared/secrets" && pwd)/redact.sh" "$@"
