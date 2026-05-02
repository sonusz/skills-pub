#!/usr/bin/env bash
# affected-line-age.sh <file> <start-line> <end-line>
#
# Prints the most-recent committer-time (Unix seconds) among the requested
# line range — the "behavior age" for §2's drift-exception age guard.
# Compute age-in-days as `(($(date +%s) - $(this script's output)) / 86400))`.
#
# Returns "0" with exit 0 if the file is untracked / no blame data — the
# agent should treat that as "fresh" (no historical claim to deliberateness).
#
# Usage:
#   scripts/affected-line-age.sh src/foo.go 42 58

set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "affected-line-age.sh: usage: $0 <file> <start-line> <end-line>" >&2
  exit 1
fi

file="$1"; start="$2"; end="$3"

case "$start$end" in
  *[!0-9]*) echo "affected-line-age.sh: line numbers must be positive integers" >&2; exit 1 ;;
esac

if [[ ! -f "$file" ]]; then
  echo "affected-line-age.sh: file not found: $file" >&2
  exit 1
fi

ts=$( { git blame --line-porcelain -L "$start,$end" -- "$file" 2>/dev/null || true; } \
  | awk '/^committer-time/ {print $2}' \
  | sort -n | tail -1)

printf '%s\n' "${ts:-0}"
