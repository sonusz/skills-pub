#!/usr/bin/env bash
# Usage: idle-probe.sh --pid P --idle-sec N --idle-cap-sec N [options]
#
# Ask a cheap model whether a quiet vendor process is wedged (kill) or still
# working on a legitimately slow task (extend). The script composes a
# read-only evidence prompt (prompts/idle-probe.md + process tree + stdout,
# stderr, and stream-file tails + idle numbers), sends it through call.sh, and
# prints exactly one verdict line to stdout:
#
#   extend <N>      grant N more idle seconds (N clamped to 1..1800)
#   kill            stop the process now
#
# optionally followed by one `rationale: ...` line for the operator log.
#
# Fail-closed rule: ANY runtime failure (no probe vendor, vendor CLI missing,
# non-zero exit, probe timeout, unparsable answer) prints `kill` plus a
# rationale and still exits 0, so a watchdog can consume the verdict without
# branching on exit status. Only argument/usage errors exit non-zero (2).
#
# Test hooks:
#   VENDORS_IDLE_PROBE_FAKE=<path>          run <path> instead of call.sh; it
#                                           receives the composed prompt on
#                                           stdin and prints the model answer
#                                           (a `VERDICT:` line) on stdout.
#   VENDORS_IDLE_PROBE_FAKE_VERDICT=extend:N|kill
#                                           skip everything and print that
#                                           verdict (--compose-only still
#                                           composes).
#
# Portability: bash 3.2 (macOS) and Linux. The process tree comes from the
# shared process-tree.sh helper (pgrep -P + BSD/procps-common ps keywords);
# the inline fallback is the portable `ps -o pid,ppid,state,etime,command -p`
# form. Never add GNU-only flags (`--forest`, `etimes`, `cmd`) here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAX_EXTEND_SEC=1800
TAIL_LINES=200
VERDICT_RE='^[[:space:]]*VERDICT:[[:space:]]*(extend[[:space:]]+[0-9]+|kill)[[:space:]]*$'

usage() {
  cat <<'USAGE'
Usage:
  scripts/idle-probe.sh --pid P --idle-sec N --idle-cap-sec N [options]

Watched process:
  --pid P                        Root pid to inspect (its tree is captured)
  --idle-sec N                   Seconds since the process last produced output
  --idle-cap-sec N               The idle threshold that just fired
  --label NAME                   Caller label (stage name, call id, ...)
  --stream FILE                  Live stream/transcript file (size, mtime, tail)
  --stdout FILE                  Stdout log (last 200 lines)
  --stderr FILE                  Stderr log (last 200 lines)

Probe model:
  --probe-vendor V               openai|claude|agy|cursor|grok (required unless
                                 a test hook is set)
  --probe-model M                Model override (default: vendors.conf)
  --probe-effort E               min|low|medium|high|xhigh|max
  --probe-timeout S              Probe call timeout in seconds (default: 60)
  --probe-native-arg ARG         Raw probe-vendor CLI arg; repeatable
  --output-dir DIR               call.sh output dir for the probe's own call
                                 (<DIR>/probe/out, status, log). Keep it outside
                                 any reviewed repo. Default: a temp dir that is
                                 removed on exit.
  --prompt-file F                Base prompt (default: ../prompts/idle-probe.md)

Testing:
  --compose-only                 Print the composed prompt and exit 0
  -h, --help                     Show this help

Output: one line, `extend <N>` or `kill`, then an optional `rationale: ...`
line. Exit status is 0 for every verdict, including fail-closed kills.
USAGE
}

die() {
  printf "idle-probe.sh: %s\n" "$*" >&2
  exit 2
}

PID=""
IDLE_SEC=""
IDLE_CAP_SEC=""
LABEL=""
STREAM_FILE=""
STDOUT_FILE=""
STDERR_FILE=""
PROBE_VENDOR=""
PROBE_MODEL=""
PROBE_EFFORT=""
PROBE_TIMEOUT=60
PROBE_NATIVE_ARGS=()
OUTPUT_DIR=""
PROMPT_FILE="$SCRIPT_DIR/../prompts/idle-probe.md"
COMPOSE_ONLY=0

while [ "$#" -gt 0 ]; do
  arg="$1"
  shift
  val=""
  has_val=0
  case "$arg" in
    --*=*)
      val="${arg#*=}"
      arg="${arg%%=*}"
      has_val=1
      ;;
  esac
  case "$arg" in
    --pid|--idle-sec|--idle-cap-sec|--label|--stream|--stdout|--stderr|\
    --probe-vendor|--probe-model|--probe-effort|--probe-timeout|\
    --probe-native-arg|--output-dir|--prompt-file)
      if [ "$has_val" = "0" ]; then
        [ "$#" -gt 0 ] || die "$arg requires a value"
        val="$1"
        shift
      fi
      ;;
  esac
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --pid) PID="$val" ;;
    --idle-sec) IDLE_SEC="$val" ;;
    --idle-cap-sec) IDLE_CAP_SEC="$val" ;;
    --label) LABEL="$val" ;;
    --stream) STREAM_FILE="$val" ;;
    --stdout) STDOUT_FILE="$val" ;;
    --stderr) STDERR_FILE="$val" ;;
    --probe-vendor) PROBE_VENDOR="$val" ;;
    --probe-model) PROBE_MODEL="$val" ;;
    --probe-effort) PROBE_EFFORT="$val" ;;
    --probe-timeout) PROBE_TIMEOUT="$val" ;;
    --probe-native-arg) PROBE_NATIVE_ARGS+=("$val") ;;
    --output-dir) OUTPUT_DIR="$val" ;;
    --prompt-file) PROMPT_FILE="$val" ;;
    --compose-only) COMPOSE_ONLY=1 ;;
    *) die "unknown option: $arg" ;;
  esac
done

case "$PID" in
  ''|*[!0-9]*) die "--pid must be a positive integer" ;;
esac
case "$IDLE_SEC" in
  ''|*[!0-9]*) die "--idle-sec must be a non-negative integer" ;;
esac
case "$IDLE_CAP_SEC" in
  ''|*[!0-9]*) die "--idle-cap-sec must be a non-negative integer" ;;
esac
case "$PROBE_TIMEOUT" in
  ''|*[!0-9]*) die "--probe-timeout must be a non-negative integer" ;;
esac

# ---------------------------------------------------------------------------
# Verdict output. Every path below ends in one of these two functions.
# ---------------------------------------------------------------------------

one_line() {
  # Collapse a possibly multi-line rationale into one trimmed line.
  printf '%s' "$1" | tr '\n\r\t' '   ' | sed -e 's/  */ /g' -e 's/^ //' -e 's/ $//'
}

emit_kill() {
  local rationale="${1:-}"
  printf 'kill\n'
  if [ -n "$rationale" ]; then
    printf 'rationale: %s\n' "$(one_line "$rationale")"
  fi
  exit 0
}

emit_extend() {
  local n="$1"
  local rationale="${2:-}"
  # Clamp to 1..MAX_EXTEND_SEC. Very long digit strings would overflow the
  # shell's integer test, so treat anything over nine digits as "huge".
  if [ "${#n}" -gt 9 ] || [ "$n" -gt "$MAX_EXTEND_SEC" ]; then
    n="$MAX_EXTEND_SEC"
  elif [ "$n" -lt 1 ]; then
    n=1
  fi
  printf 'extend %s\n' "$n"
  if [ -n "$rationale" ]; then
    printf 'rationale: %s\n' "$(one_line "$rationale")"
  fi
  exit 0
}

# Hard override: skip composition and any model call (test convenience).
if [ "$COMPOSE_ONLY" != "1" ] && [ -n "${VENDORS_IDLE_PROBE_FAKE_VERDICT:-}" ]; then
  forced="$VENDORS_IDLE_PROBE_FAKE_VERDICT"
  case "$forced" in
    kill)
      emit_kill "forced by env"
      ;;
    extend:*)
      forced_n="${forced#extend:}"
      case "$forced_n" in
        ''|*[!0-9]*) emit_kill "forced verdict malformed: $forced" ;;
      esac
      emit_extend "$forced_n" "forced by env"
      ;;
    *)
      emit_kill "forced verdict malformed: $forced"
      ;;
  esac
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/idle-probe.XXXXXX")
cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Evidence gathering.
# ---------------------------------------------------------------------------

file_mtime_epoch() {
  # Portable mtime (follows symlinks). Selected by uname like vendor-launch.sh's
  # vendors_host_os, never by sniffing one platform's error output.
  local file="$1"
  case "$(uname -s)" in
    Darwin|FreeBSD|OpenBSD|NetBSD|DragonFly) stat -L -f %m "$file" 2>/dev/null || true ;;
    Linux) stat -L -c %Y "$file" 2>/dev/null || true ;;
    *) printf '' ;;
  esac
}

tail_text() {
  # Last TAIL_LINES lines of a file, or a sentinel. Used inside $(...), so the
  # trailing newline is stripped and the caller re-adds exactly one.
  local file="$1"
  if [ -z "$file" ] || [ ! -e "$file" ]; then
    printf '(file not present)'
    return 0
  fi
  if [ ! -r "$file" ]; then
    printf '(read error: cannot read %s)' "$file"
    return 0
  fi
  tail -n "$TAIL_LINES" "$file" 2>/dev/null \
    || printf '(read error: tail failed for %s)' "$file"
}

write_process_tree() {
  # Primary: shared process-tree.sh (pid + descendants). Fallback: portable ps
  # for the root pid only. Sentinel when the process is gone.
  local out="$1"
  if [ -r "$SCRIPT_DIR/process-tree.sh" ] \
      && bash "$SCRIPT_DIR/process-tree.sh" "$PID" > "$out" 2>/dev/null \
      && [ -n "$(tr -d '[:space:]' < "$out")" ]; then
    return 0
  fi
  if ps -o pid,ppid,state,etime,command -p "$PID" > "$out" 2>/dev/null \
      && [ -n "$(tr -d '[:space:]' < "$out")" ]; then
    return 0
  fi
  printf '(could not read process tree for pid %s)' "$PID" > "$out"
}

compose_prompt() {
  local out="$1"
  local tree_file="$WORK/tree"
  local size_bytes=0
  local seconds_since="$IDLE_SEC"
  local mtime=""
  local now=""

  write_process_tree "$tree_file"

  {
    cat "$PROMPT_FILE"
    printf '\n\n---\n\n## Probe inputs\n\n'
    printf -- '- **Stage**: `%s`\n' "$LABEL"
    printf -- '- **Subagent pid**: `%s`\n' "$PID"
    printf -- '- **Idle duration**: `%ss`\n' "$IDLE_SEC"
    printf -- '- **Configured idle cap**: `%ss`\n' "$IDLE_CAP_SEC"
    printf -- '- **Stdout path**: `%s`\n' "$STDOUT_FILE"
    printf -- '- **Stderr path**: `%s`\n' "$STDERR_FILE"
    if [ -n "$STREAM_FILE" ]; then
      if [ -e "$STREAM_FILE" ]; then
        size_bytes=$(wc -c < "$STREAM_FILE" 2>/dev/null | tr -d '[:space:]' || true)
        [ -n "$size_bytes" ] || size_bytes=0
        mtime=$(file_mtime_epoch "$STREAM_FILE")
        if [ -n "$mtime" ]; then
          now=$(date +%s)
          if [ "$now" -gt "$mtime" ]; then
            seconds_since=$((now - mtime))
          else
            seconds_since=0
          fi
        fi
      fi
      printf -- '- **Stream output file path**: `%s`\n' "$STREAM_FILE"
      printf -- '- **Stream output file size_bytes**: `%s`\n' "$size_bytes"
      printf -- '- **Stream output file seconds_since_modified**: `%ss`\n' "$seconds_since"
      printf '\n### Stream output tail (last %s lines)\n\n```\n' "$TAIL_LINES"
      printf '%s\n```\n' "$(tail_text "$STREAM_FILE")"
    fi
    printf '\n### Process tree\n\n```\n'
    cat "$tree_file"
    printf '\n```\n\n'
    printf '### Stdout tail (last %s lines)\n\n```\n' "$TAIL_LINES"
    printf '%s\n```\n\n' "$(tail_text "$STDOUT_FILE")"
    printf '### Stderr tail (last %s lines)\n\n```\n' "$TAIL_LINES"
    printf '%s\n```\n' "$(tail_text "$STDERR_FILE")"
  } > "$out"
}

PROMPT_OUT="$WORK/prompt.md"
if [ ! -r "$PROMPT_FILE" ]; then
  if [ "$COMPOSE_ONLY" = "1" ]; then
    die "cannot read prompt file: $PROMPT_FILE"
  fi
  emit_kill "probe unavailable: cannot read prompt file $PROMPT_FILE"
fi
compose_prompt "$PROMPT_OUT"

if [ "$COMPOSE_ONLY" = "1" ]; then
  cat "$PROMPT_OUT"
  exit 0
fi

# ---------------------------------------------------------------------------
# Model call (fake invoker or call.sh) and verdict parsing.
# ---------------------------------------------------------------------------

kill_tree() {
  local pid="$1"
  local signal_name="${2:-TERM}"
  local child=""

  [ -n "$pid" ] || return 0
  while read -r child; do
    [ -n "$child" ] || continue
    kill_tree "$child" "$signal_name"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill "-$signal_name" "$pid" 2>/dev/null || true
}

parse_verdict_file() {
  # Reads the model answer in $1; emits the verdict and exits.
  local raw="$1"
  local verdict_line=""
  local n=""
  local rationale=""
  local excerpt=""

  verdict_line=$(grep -E "$VERDICT_RE" "$raw" 2>/dev/null | head -n 1 || true)
  if [ -z "$verdict_line" ]; then
    excerpt=$(head -c 160 "$raw" 2>/dev/null | tr -d '\000' || true)
    emit_kill "probe did not emit parseable VERDICT line (output starts: $excerpt)"
  fi
  # Rationale = everything after the first VERDICT line.
  rationale=$(awk -v re="$VERDICT_RE" '
    found { print; next }
    $0 ~ re { found = 1 }
  ' "$raw" 2>/dev/null || true)
  case "$verdict_line" in
    *kill*)
      emit_kill "${rationale:-probe said kill}"
      ;;
  esac
  n=$(printf '%s' "$verdict_line" | sed -E 's/^[[:space:]]*VERDICT:[[:space:]]*extend[[:space:]]+([0-9]+)[[:space:]]*$/\1/')
  case "$n" in
    ''|*[!0-9]*) emit_kill "probe emitted malformed extend verdict: $verdict_line" ;;
  esac
  emit_extend "$n" "$rationale"
}

RAW_ANSWER="$WORK/answer.txt"

if [ -n "${VENDORS_IDLE_PROBE_FAKE:-}" ]; then
  fake="$VENDORS_IDLE_PROBE_FAKE"
  fake_status=0
  timed_out_marker="$WORK/fake-timed-out"
  "$fake" < "$PROMPT_OUT" > "$RAW_ANSWER" 2> "$WORK/fake.err" &
  fake_pid=$!
  timer_pid=""
  if [ "$PROBE_TIMEOUT" -gt 0 ]; then
    (
      sleep "$PROBE_TIMEOUT"
      : > "$timed_out_marker"
      kill_tree "$fake_pid" TERM
      sleep 2
      kill_tree "$fake_pid" KILL
    ) &
    timer_pid=$!
  fi
  # 2>/dev/null hides bash's "Terminated" job notice when the timer fires.
  wait "$fake_pid" 2>/dev/null || fake_status=$?
  if [ -n "$timer_pid" ]; then
    kill_tree "$timer_pid" TERM
    wait "$timer_pid" 2>/dev/null || true
  fi
  if [ -e "$timed_out_marker" ]; then
    emit_kill "probe itself timed out after ${PROBE_TIMEOUT}s"
  fi
  if [ "$fake_status" -ne 0 ]; then
    emit_kill "probe exited $fake_status; stderr: $(tail -c 200 "$WORK/fake.err" 2>/dev/null || true)"
  fi
  parse_verdict_file "$RAW_ANSWER"
fi

[ -n "$PROBE_VENDOR" ] || emit_kill "no probe vendor configured; defaulting to kill"

if [ -z "$OUTPUT_DIR" ]; then
  OUTPUT_DIR="$WORK/call"
fi
mkdir -p "$OUTPUT_DIR" 2>/dev/null || emit_kill "cannot create probe output dir: $OUTPUT_DIR"

call_args=(
  "$SCRIPT_DIR/call.sh"
  --vendor "$PROBE_VENDOR"
  --id probe
  --timeout "$PROBE_TIMEOUT"
  --prompt-file "$PROMPT_OUT"
  --output-dir "$OUTPUT_DIR"
  --min-success 1
)
[ -z "$PROBE_MODEL" ] || call_args+=(--model "$PROBE_MODEL")
[ -z "$PROBE_EFFORT" ] || call_args+=(--effort "$PROBE_EFFORT")
for native_arg in ${PROBE_NATIVE_ARGS[@]+"${PROBE_NATIVE_ARGS[@]}"}; do
  call_args+=(--native-arg "$native_arg")
done

call_status=0
"${call_args[@]}" > "$WORK/call.stdout" 2> "$WORK/call.stderr" || call_status=$?

probe_status_file="$OUTPUT_DIR/probe/status"
probe_out_file="$OUTPUT_DIR/probe/out"
probe_log_file="$OUTPUT_DIR/probe/log"
probe_exit=""
probe_reason=""
if [ -r "$probe_status_file" ]; then
  probe_exit=$(awk -F= '$1 == "exit_code" { print $2; exit }' "$probe_status_file" || true)
  probe_reason=$(awk -F= '$1 == "reason" { print $2; exit }' "$probe_status_file" || true)
fi

if [ "$call_status" -ne 0 ] || [ "$probe_exit" != "0" ]; then
  # Surface the wrapper's own diagnostics for the operator log, then fail closed.
  if [ -s "$WORK/call.stderr" ]; then
    sed -e 's/^/idle-probe.sh: call.sh: /' "$WORK/call.stderr" >&2 || true
  fi
  if [ "$probe_reason" = "timeout" ]; then
    emit_kill "probe itself timed out after ${PROBE_TIMEOUT}s"
  fi
  detail=""
  if [ -s "$probe_log_file" ]; then
    detail=$(tail -c 200 "$probe_log_file" 2>/dev/null || true)
  elif [ -s "$WORK/call.stderr" ]; then
    detail=$(tail -c 200 "$WORK/call.stderr" 2>/dev/null || true)
  fi
  emit_kill "probe exited ${probe_exit:-$call_status}${probe_reason:+ ($probe_reason)}; log: $detail"
fi

[ -r "$probe_out_file" ] || emit_kill "probe produced no output file"
parse_verdict_file "$probe_out_file"
