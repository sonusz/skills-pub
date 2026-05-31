#!/usr/bin/env bash
# Usage:
#   process-tree.sh <pid>           Print a process tree rooted at <pid>
#                                   (the pid plus all of its descendants),
#                                   indented by depth.
#   process-tree.sh --alive <pid>   Exit 0 if <pid> is alive, 1 if not.
#
# Why this exists: liveness/idle probes (e.g. auto-dev's idle-timeout probe)
# need to read a vendor subprocess's process tree to judge "wedged vs working".
# The naive `ps --forest -o ...,cmd` form is GNU-only and FAILS on macOS/BSD ps
# ("illegal option -- -" / "cmd: keyword not found"), which silently degrades
# the probe's input to nothing and biases it toward a false-positive kill.
#
# This helper is portable across macOS (BSD ps) and Linux (GNU ps):
#   - child enumeration uses `pgrep -P` (mirrors call.sh's kill_tree), not
#     `ps --forest`;
#   - per-process detail uses `ps -o pid,ppid,state,etime,command -p`, all of
#     which are keywords common to BSD and GNU ps (NOT the GNU-only `cmd`).
#
# NOTE: this repo is developed and supported on macOS only (see repo README).
# The GNU fallbacks here are best-effort portability, not a supported target.
set -eo pipefail

MAX_DEPTH=20  # defensive: pgrep -P is acyclic, but cap recursion regardless.

# Print one ps detail line for a pid, indented by depth. Header is suppressed
# (keyword= form); the root caller prints a single shared header. Missing/dead
# pids print nothing (the `|| true` keeps `set -e` from aborting the walk).
ps_line() {
  local pid="$1" depth="$2" indent="" i=0 line=""
  while [ "$i" -lt "$depth" ]; do indent="${indent}  "; i=$((i + 1)); done
  line="$(ps -o pid=,ppid=,state=,etime=,command= -p "$pid" 2>/dev/null || true)"
  [ -n "$line" ] || return 0
  # Collapse leading whitespace ps adds for short pids, then re-indent by depth.
  printf '%s%s\n' "$indent" "$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//')"
}

walk() {
  local pid="$1" depth="$2" child=""
  [ "$depth" -le "$MAX_DEPTH" ] || return 0
  ps_line "$pid" "$depth"
  while read -r child; do
    [ -n "$child" ] || continue
    walk "$child" $((depth + 1))
  done < <(pgrep -P "$pid" 2>/dev/null || true)
}

main() {
  case "${1:-}" in
    --alive)
      local pid="${2:-}"
      [ -n "$pid" ] || { echo "usage: process-tree.sh --alive <pid>" >&2; exit 2; }
      if kill -0 "$pid" 2>/dev/null; then exit 0; else exit 1; fi
      ;;
    "" | -h | --help)
      echo "usage: process-tree.sh <pid> | --alive <pid>" >&2
      exit 2
      ;;
    *)
      local pid="$1"
      printf 'PID PPID STAT ELAPSED COMMAND\n'
      walk "$pid" 0
      ;;
  esac
}

main "$@"
