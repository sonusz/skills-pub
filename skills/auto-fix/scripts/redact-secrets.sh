#!/usr/bin/env bash
# redact-secrets.sh
#
# Reads text on stdin and writes it to stdout with well-known secret
# patterns replaced by `<redacted>` markers. Used by auto-fix on any
# evidence/error_log content that flows into the structured output —
# build logs and stack traces routinely contain credentials that would
# otherwise be persisted in tool results.
#
# This is a denylist of high-confidence patterns, not a guarantee of
# completeness. A novel secret format will pass through; the agent must
# still avoid quoting raw blocks of `error_log` verbatim if the surrounding
# context suggests sensitive content. Defense in depth, not silver bullet.
#
# Patterns covered:
#   - AWS access key (AKIA...) and session-token key (ASIA...)
#   - GitHub PATs (ghp_, gho_, ghu_, ghs_, ghr_)
#   - JWTs (three-part base64url)
#   - Slack tokens (xoxb-/xoxp-/xoxa-/xoxr-/xoxs-)
#   - Authorization: <scheme> <token>  HTTP headers
#   - Bearer <token>  inline references
#   - URI-with-creds (scheme://user:pass@host)
#
# Usage:
#   echo "$evidence" | scripts/redact-secrets.sh
#   scripts/redact-secrets.sh < /path/to/build.log

set -euo pipefail

if ! command -v perl >/dev/null 2>&1; then
  echo "redact-secrets.sh: perl is required" >&2
  exit 1
fi

exec perl -pe '
  # AWS access keys
  s/\bAKIA[0-9A-Z]{16}\b/AKIA<redacted>/g;
  s/\bASIA[0-9A-Z]{16}\b/ASIA<redacted>/g;
  # GitHub PATs (ghp_, gho_, ghu_, ghs_, ghr_)
  s/\bgh[pousr]_[A-Za-z0-9]{16,}\b/gh<redacted>/g;
  # JWTs (header.payload.signature, all base64url)
  s/\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b/eyJ<redacted>.<redacted>.<redacted>/g;
  # Slack tokens
  s/\bxox[baprs]-[A-Za-z0-9-]{10,}\b/xox<redacted>/g;
  # Authorization headers (full `Authorization: Scheme value` line)
  s/(Authorization:\s*)\S+\s+\S+/$1<redacted>/gi;
  # Bearer/Basic tokens not preceded by an Authorization header that already matched
  s/\b(Bearer|Basic)\s+[A-Za-z0-9._\-=+\/]{16,}/$1 <redacted>/gi;
  # URIs with embedded credentials: scheme://user:pass@host -> scheme://user:<redacted>@host
  s|(://[^:/\s@]+):[^@\s]+@|$1:<redacted>@|g;
'
