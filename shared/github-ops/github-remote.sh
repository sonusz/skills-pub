#!/usr/bin/env bash
# github-remote.sh -- shared helper to auto-detect GitHub owner/repo from git remote
#
# Usage (source from other scripts):
#   source "$(dirname "${BASH_SOURCE[0]}")/github-remote.sh"
#   # Now GITHUB_OWNER and GITHUB_REPO are set, or the script has exited with error.

if ! git rev-parse --show-toplevel &>/dev/null; then
  echo "Error: not inside a git repository" >&2
  return 1 2>/dev/null || exit 1
fi

# Try origin first, fall back to first remote
_REMOTE_URL=$(git remote get-url origin 2>/dev/null || git remote get-url "$(git remote | head -n1)" 2>/dev/null || true)

if [[ -z "$_REMOTE_URL" ]]; then
  echo "Error: no git remote found" >&2
  return 1 2>/dev/null || exit 1
fi

# Parse owner/repo from GitHub remote URL
# Supports: https://github.com/owner/repo.git, git@github.com:owner/repo.git, ssh://git@github.com/owner/repo.git
if [[ "$_REMOTE_URL" =~ github\.com[:/]([^/]+)/([^/]+)$ ]]; then
  GITHUB_OWNER="${BASH_REMATCH[1]}"
  GITHUB_REPO="${BASH_REMATCH[2]%.git}"
else
  echo "Error: remote '$_REMOTE_URL' is not a GitHub URL" >&2
  return 1 2>/dev/null || exit 1
fi

unset _REMOTE_URL

_PR_WATCH_ENV=""

# _init_github_auth retrieves the PAT from the git credential helper and writes
# it to a temp env file created with mktemp (unpredictable path, mode 0600).
# The value never appears in tool output or stdout. The cleanup EXIT trap is
# registered immediately after mktemp so a SIGKILL between mktemp and a
# caller's later `trap` registration cannot leak the file.
# Callers: call _source_github_auth, then use _auth_curl.
_init_github_auth() {
  _PR_WATCH_ENV="$(umask 077 && mktemp "${TMPDIR:-/tmp}/.pr-watch-env.XXXXXX")"
  trap _cleanup_github_auth EXIT
  bash -c '
    _tok=$(printf "protocol=https\nhost=github.com\n" \
      | git credential fill 2>/dev/null \
      | awk -F= "/password=/{print \$2}")
    if [ -z "$_tok" ]; then exit 1; fi
    # Write the token as plain key=value data (no shell quoting). The loader
    # reads it as a string via `read`, never via `source`, so a token
    # containing `"`, `$`, `` ` ``, or `\` cannot become attacker-influenced
    # shell. PATs are alphanumeric today; this is defense in depth.
    printf "%s\n" "_GITHUB_TOKEN=$_tok" > "'"$_PR_WATCH_ENV"'"
  ' && return 0
  _cleanup_github_auth
  return 1
}

# _cleanup_github_auth removes the temp env file and unsets the token variable.
_cleanup_github_auth() {
  rm -f "$_PR_WATCH_ENV"
  unset _GITHUB_TOKEN
}

# _auth_curl wraps curl to pass the Authorization header via --config (process
# substitution) instead of -H on the command line, preventing token exposure in
# the process list (ps / /proc/PID/cmdline). Reads $_GITHUB_TOKEN from the
# loaded env file — no token argument needed.
# Disables xtrace around the sensitive line to prevent bash -x from printing the token.
#
# Usage: _auth_curl [curl-args...]
_auth_curl() {
  { local _xtrace; _xtrace="$(set +o | grep xtrace || true)"; set +x; } 2>/dev/null
  curl -sS --config <(printf -- '-H "Authorization: Bearer %s"' "$_GITHUB_TOKEN") \
    -H "Accept: application/vnd.github+json" \
    "$@"
  local _rc=$?
  { eval "$_xtrace"; } 2>/dev/null
  return $_rc
}

# _source_github_auth loads the token from the env file by reading the line as
# plain data and stripping the `_GITHUB_TOKEN=` prefix — never `source`s it,
# so a token with shell metacharacters cannot execute. xtrace is disabled
# around the load to prevent bash -x from printing the value.
_source_github_auth() {
  { local _xtrace; _xtrace="$(set +o | grep xtrace || true)"; set +x; } 2>/dev/null
  local _line=""
  IFS= read -r _line < "$_PR_WATCH_ENV" || true
  _GITHUB_TOKEN="${_line#_GITHUB_TOKEN=}"
  { eval "$_xtrace"; } 2>/dev/null
}
