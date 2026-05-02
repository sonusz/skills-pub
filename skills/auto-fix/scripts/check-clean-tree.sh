#!/usr/bin/env bash
# check-clean-tree.sh
#
# Preflight for auto-fix's apply mode: refuse if the working tree has any
# uncommitted changes. This prevents the §6 stuck-mid-fix revert
# (`git checkout HEAD -- <files>`) from racing against, or being mistaken
# for, the user's parallel work.
#
# Exit:
#   0  working tree is clean
#   2  working tree is dirty; output lists the offending paths

set -euo pipefail

if ! git rev-parse --show-toplevel &>/dev/null; then
  echo "check-clean-tree.sh: not inside a git repository" >&2
  exit 1
fi

DIRTY=$(git status --porcelain)

if [[ -z "$DIRTY" ]]; then
  exit 0
fi

cat >&2 <<EOF
[auto-fix] Refused to start: working tree is not clean.

The following paths have uncommitted changes:
$(printf '%s\n' "$DIRTY" | sed 's/^/  /')

auto-fix's apply mode reverts on stuck-mid-fix; running it now would
either destroy this in-progress work or be confused with it. Commit,
stash, or discard the changes above before re-invoking auto-fix.
EOF
exit 2
