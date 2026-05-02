#!/usr/bin/env bash
# push-with-snapshot.sh --prior-head-sha <sha> [--branch <name>] [--remote <name>]
#
# Pushes the current branch only if the remote head still matches the SHA the
# caller captured at gate-confirmation time. If the remote head has moved
# since (someone else pushed), the script refuses; the caller must re-fetch
# state and re-confirm with the user before retrying.
#
# This is the structural enforcement of the stateful-gate invariant for the
# CI-fix flow: user confirmation is bound to a specific (action, state) pair,
# and consent is invalid if state has drifted before the action lands.
#
# Usage:
#   scripts/push-with-snapshot.sh --prior-head-sha <sha> [--branch BR] [--remote RM]
#
# Required:
#   --prior-head-sha SHA   The remote-head SHA captured when the user confirmed
#                          the gate. Push is allowed only if the current remote
#                          head equals this value.
# Optional:
#   --branch NAME          Branch to push. Default: current local branch.
#   --remote NAME          Remote name. Default: origin.
#
# Exit codes:
#   0  push succeeded
#   1  usage / setup error
#   2  git push itself failed (after the snapshot check passed)
#   4  remote head moved since the snapshot — refused, no push attempted

set -euo pipefail

# This script uses `git ls-remote` and `git push`, both of which use the same
# credential helper as the rest of the skill. No bespoke auth wiring needed.

PRIOR_SHA=""
BRANCH=""
REMOTE="origin"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prior-head-sha)  PRIOR_SHA="${2:-}"; shift 2 ;;
    --prior-head-sha=*) PRIOR_SHA="${1#*=}"; shift ;;
    --branch)          BRANCH="${2:-}"; shift 2 ;;
    --branch=*)        BRANCH="${1#*=}"; shift ;;
    --remote)          REMOTE="${2:-}"; shift 2 ;;
    --remote=*)        REMOTE="${1#*=}"; shift ;;
    -h|--help)
      sed -n '2,28p' "$0" | sed 's|^# \{0,1\}||'
      exit 0
      ;;
    -*) echo "push-with-snapshot.sh: unknown flag: $1" >&2; exit 1 ;;
    *)  echo "push-with-snapshot.sh: unexpected positional arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$PRIOR_SHA" ]]; then
  echo "push-with-snapshot.sh: --prior-head-sha is required" >&2
  exit 1
fi

if ! [[ "$PRIOR_SHA" =~ ^[0-9a-f]{7,40}$ ]]; then
  echo "push-with-snapshot.sh: --prior-head-sha must be a hex SHA: $PRIOR_SHA" >&2
  exit 1
fi

if [[ -z "$BRANCH" ]]; then
  BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  if [[ -z "$BRANCH" || "$BRANCH" == "HEAD" ]]; then
    echo "push-with-snapshot.sh: could not determine current branch; pass --branch" >&2
    exit 1
  fi
fi

# Query the remote's current head for this branch. ls-remote uses the same
# credential helper as the rest of the skill, so no separate auth setup.
REMOTE_LINE=$(git ls-remote "$REMOTE" "refs/heads/$BRANCH" 2>/dev/null || true)
if [[ -z "$REMOTE_LINE" ]]; then
  echo "push-with-snapshot.sh: could not fetch remote head for $REMOTE/$BRANCH" >&2
  exit 1
fi
REMOTE_SHA=$(printf '%s\n' "$REMOTE_LINE" | awk '{print $1}')

# Allow short-SHA --prior-head-sha by comparing on the shorter length.
PRIOR_LEN=${#PRIOR_SHA}
REMOTE_PREFIX="${REMOTE_SHA:0:$PRIOR_LEN}"

if [[ "$REMOTE_PREFIX" != "$PRIOR_SHA" ]]; then
  cat >&2 <<EOF
[push-with-snapshot] Refused: remote head moved since the gate was confirmed.
  prior:   $PRIOR_SHA
  current: $REMOTE_SHA
  branch:  $REMOTE/$BRANCH
The user's confirmation was bound to the prior state. Re-fetch state and
re-prompt the user before retrying.
EOF
  exit 4
fi

# Snapshot still matches; perform the push.
git push "$REMOTE" "$BRANCH"
