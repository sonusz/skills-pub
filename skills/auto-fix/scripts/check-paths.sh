#!/usr/bin/env bash
# check-paths.sh <file1> <file2> ...
#
# Refuses if any input path matches a denylist pattern. Used by auto-fix
# before its size gate to keep the agent out of security-critical paths
# (auth, credentials, migrations, schemas) regardless of how the agent
# would otherwise classify the file. Path-level safety is structural
# enforcement, not LLM judgment.
#
# The denylist is the union of:
#   1. Hard-coded patterns below (project-agnostic defense in depth).
#   2. Repo-local extensions in `.auto-fix-paths.deny`, one glob per line,
#      blank lines and `#`-prefixed comments ignored. Looked up at the
#      git toplevel so subdirectory invocations still find it.
#
# Patterns are bash extglob globs matched against the *full path as given*
# (no normalization) AND against the path's basename. Either match denies.
#
# Exit:
#   0  every path is allowed
#   2  at least one path matched the denylist; offending paths are
#      printed to stderr along with the matching pattern

set -euo pipefail
shopt -s extglob nocasematch

if [[ $# -eq 0 ]]; then
  echo "check-paths.sh: at least one file path is required" >&2
  exit 1
fi

# Hard-coded denylist. Patterns deliberately broad — auto-fix is opting
# out of these areas, not trying to identify every sensitive file.
HARD_DENY=(
  '*auth*'
  '*authn*'
  '*authz*'
  '*credential*'
  '*secret*'
  '*token*'
  '*password*'
  '*passwd*'
  '*migration*'
  '*migrations*'
  '*schema*'
  '*security*'
  '*crypto*'
  '*.pem'
  '*.key'
  '*.p12'
  '*.pfx'
  '.env'
  '.env.*'
  'keys/*'
  '*/keys/*'
  'secrets/*'
  '*/secrets/*'
  '.github/workflows/*'
)

# Optional repo-local extensions.
EXTRA_DENY=()
TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null || true)
if [[ -n "$TOPLEVEL" && -r "$TOPLEVEL/.auto-fix-paths.deny" ]]; then
  while IFS= read -r line; do
    line="${line%%#*}"        # strip inline comment
    line="${line#"${line%%[![:space:]]*}"}"  # ltrim
    line="${line%"${line##*[![:space:]]}"}"  # rtrim
    [[ -z "$line" ]] && continue
    EXTRA_DENY+=("$line")
  done < "$TOPLEVEL/.auto-fix-paths.deny"
fi

DENY=("${HARD_DENY[@]}")
if [[ ${#EXTRA_DENY[@]} -gt 0 ]]; then
  DENY+=("${EXTRA_DENY[@]}")
fi

OFFENDERS=()
for path in "$@"; do
  base=$(basename "$path")
  matched=""
  for pat in "${DENY[@]}"; do
    # shellcheck disable=SC2053  # intentional glob, not literal compare
    if [[ "$path" == $pat || "$base" == $pat ]]; then
      matched="$pat"
      break
    fi
  done
  if [[ -n "$matched" ]]; then
    OFFENDERS+=("$path matched pattern '$matched'")
  fi
done

if [[ ${#OFFENDERS[@]} -eq 0 ]]; then
  exit 0
fi

cat >&2 <<EOF
[auto-fix] Refused: one or more affected paths are denylisted.

$(printf '  %s\n' "${OFFENDERS[@]}")

These paths are excluded from autonomous fixes regardless of how small
or innocuous the change appears. Escalate as 'above-minor' with this
output as evidence; a human must take the change.
EOF
exit 2
