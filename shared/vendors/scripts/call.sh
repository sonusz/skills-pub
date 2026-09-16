#!/usr/bin/env bash
# Usage: call.sh --vendor openai|claude|agy|cursor|grok [--vendor ...] [options] [prompt]
#
# Unified vendor interface. A single --vendor behaves like a normal CLI call;
# repeated --vendor values fan the same prompt out in parallel.
set -eo pipefail

VENDORS_INVOCATION_CWD=$(pwd -P)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=vendor-launch.sh
. "$SCRIPT_DIR/vendor-launch.sh"

# A keyed invocation owns durable leases, so it must also own a kernel process
# group whose emptiness can be verified during handled-signal cleanup. The
# Python re-exec preserves pid, argv, cwd, environment, and stdin; os.setsid()
# gives direct shell callers the same isolation that auto-dev's
# start_new_session=True already provides.
VENDORS_RAW_SESSION_REQUESTED=0
for vendors_raw_arg in "$@"; do
  case "$vendors_raw_arg" in
    --session-key|--session-key=*)
      VENDORS_RAW_SESSION_REQUESTED=1
      break
      ;;
  esac
done
if [ "$VENDORS_RAW_SESSION_REQUESTED" = "1" ]; then
  vendors_bootstrap_python=$(vendors_session_python || true)
  if [ -n "$vendors_bootstrap_python" ]; then
    vendors_current_pgid=$(
      "$vendors_bootstrap_python" -c 'import os; print(os.getpgrp())'
    )
    if [ "$vendors_current_pgid" != "$$" ]; then
      exec "$vendors_bootstrap_python" -c '
import os
import sys
script = sys.argv[1]
os.setsid()
os.execv("/bin/bash", ["/bin/bash", script, *sys.argv[2:]])
' "$SCRIPT_DIR/call.sh" "$@"
    fi
  fi
fi

usage() {
  cat <<'USAGE'
Usage:
  scripts/call.sh --vendor openai|claude|agy|cursor|grok [--vendor NAME...] [options] [prompt]

Vendor selection:
  --vendor NAME                  openai, claude, agy, cursor, or grok; repeatable
                                 grok and xai select the official grok CLI
  --min-success N                Required successful calls. Default: all selected

Output:
  --output-dir DIR               Output root for <id>/out, <id>/status, <id>/log
                                 If omitted, a temp dir is kept and printed
  --id ID                        Output directory id; repeatable with --vendor
                                 Default: normalized vendor id, suffixed on duplicates
  --session-key KEY              Persist/resume one logical agent conversation.
                                 Repeat once per --vendor in fan-out calls.
  --session-max-turns N          Start a fresh native session after N successful
                                 keyed turns; applies to every selected vendor.

Selection hints:
  --effort min|low|medium|high|xhigh|max
                                 Best-effort reasoning hint
  --model MODEL                  Override vendors.conf model selection
  --yolo                         Map to each vendor's no-approval mode
  --config FILE                  Model mapping config (default: ../vendors.conf)
  --schema-file FILE             JSON Schema constraining the response shape.
                                 Output lands at <output-dir>/<id>/out as
                                 {"structured_output": <conforming-object>}.
                                 Supported on claude, openai (codex), and grok.
                                 agy and cursor do not enforce schemas natively.

Prompt and instruction input:
  --prompt TEXT                  Prompt text; repeatable
  --prompt-file FILE             Prompt file; repeatable
  --resume-prompt TEXT           Smaller continuation prompt used only when
                                 --session-key resolves to an existing session
  --resume-prompt-file FILE      File form of --resume-prompt
  --system TEXT                  System instruction; repeatable
  --system-file FILE             System instruction file; repeatable
  --instruction TEXT             Additional instruction; repeatable
  --instruction-file FILE        Additional instruction file; repeatable
  --context-file FILE            Inline a referenced artifact with path/hash;
                                 repeatable
  prompt                         Positional prompt text
  stdin                          Used as prompt when no prompt args are given

Runtime and native options:
  --cwd DIR                      Run from this working directory
  --timeout SECONDS              Per-vendor timeout
  --timeout-extend SECONDS       At the timeout deadline, if the vendor's
                                 stream output grew since the last check,
                                 wait another SECONDS instead of killing;
                                 repeats while output keeps growing. Kill
                                 happens only after a full window with no
                                 new output. 0 disables (default).
  --idle-probe-vendor NAME       When the watchdog is about to kill a silent
                                 call (deadline reached, no new output for a
                                 full extend window), first ask this vendor's
                                 cheap model whether to extend or kill (see
                                 idle-probe.sh). Any probe failure kills.
                                 Requires --timeout. Off by default.
  --idle-probe-model MODEL       Probe model (default: vendors.conf model)
  --idle-probe-effort EFFORT     Probe effort hint (default: none)
  --idle-probe-timeout SECONDS   Probe call timeout (default: 60; must be > 0)
  --idle-probe-max-total SECONDS Total extra seconds all probe verdicts may
                                 grant to one call (default: 3600)
  --native-arg ARG               Raw selected-vendor CLI arg; repeatable
  --env NAME=VALUE               Per-call environment override; repeatable
  --dry-run                      Print resolved command(s) without calling models
  -h, --help                     Show this help
USAGE
}

die() {
  printf "call.sh: %s\n" "$*" >&2
  exit 2
}

require_value() {
  local opt="$1"
  local value="${2-}"

  if [ -z "$value" ]; then
    die "$opt requires a value"
  fi
}

file_sha256() {
  local file="$1"

  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  else
    printf "unknown"
  fi
}

CONFIG_FILE="$SCRIPT_DIR/../vendors.conf"
VENDOR_RAWS=()
EFFORT=""
MODEL_OVERRIDE=""
VENDORS_YOLO=0
OUTPUT_DIR=""
CALL_IDS=()
SESSION_KEYS=()
SESSION_MAX_TURNS=""
MIN_SUCCESS=""
TIMEOUT_SECONDS=0
TIMEOUT_EXTEND_SECONDS=0
IDLE_PROBE_VENDOR=""
IDLE_PROBE_MODEL=""
IDLE_PROBE_EFFORT=""
IDLE_PROBE_TIMEOUT=60
IDLE_PROBE_MAX_TOTAL=3600

VENDORS_CWD=""
VENDORS_EFFECTIVE_CWD=""
VENDORS_DRY_RUN=0
VENDORS_NATIVE_ARGS=()
VENDORS_ENV=()
VENDORS_SYSTEM_PROMPT=""
VENDORS_TRANSCRIPT_FILE=""
VENDORS_SCHEMA_FILE=""
VENDORS_SESSION_KEY=""
VENDORS_SESSION_MODE=""
VENDORS_SESSION_ID=""
VENDORS_SESSION_PLAN_FILE=""
VENDORS_SESSION_RESULT_FILE=""

PROMPT_TEXTS=()
PROMPT_FILES=()
RESUME_PROMPT_TEXTS=()
RESUME_PROMPT_FILES=()
SYSTEM_TEXTS=()
SYSTEM_FILES=()
INSTRUCTION_TEXTS=()
INSTRUCTION_FILES=()
CONTEXT_FILES=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --vendor)
      require_value "$1" "${2-}"
      VENDOR_RAWS+=("$2")
      shift 2
      ;;
    --vendor=*)
      VENDOR_RAWS+=("${1#*=}")
      shift
      ;;
    --min-success)
      require_value "$1" "${2-}"
      MIN_SUCCESS="$2"
      shift 2
      ;;
    --min-success=*)
      MIN_SUCCESS="${1#*=}"
      shift
      ;;
    --output|--output=*)
      die "use --output-dir; outputs are always <output-dir>/<id>/out, status, and log"
      ;;
    --output-dir)
      require_value "$1" "${2-}"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --output-dir=*)
      OUTPUT_DIR="${1#*=}"
      shift
      ;;
    --id)
      require_value "$1" "${2-}"
      CALL_IDS+=("$2")
      shift 2
      ;;
    --id=*)
      CALL_IDS+=("${1#*=}")
      shift
      ;;
    --session-key)
      require_value "$1" "${2-}"
      SESSION_KEYS+=("$2")
      shift 2
      ;;
    --session-key=*)
      SESSION_KEYS+=("${1#*=}")
      shift
      ;;
    --session-max-turns)
      require_value "$1" "${2-}"
      SESSION_MAX_TURNS="$2"
      shift 2
      ;;
    --session-max-turns=*)
      SESSION_MAX_TURNS="${1#*=}"
      shift
      ;;
    --effort)
      require_value "$1" "${2-}"
      EFFORT=$(vendors_lower "$2")
      shift 2
      ;;
    --effort=*)
      EFFORT="${1#*=}"
      EFFORT=$(vendors_lower "$EFFORT")
      shift
      ;;
    --model)
      require_value "$1" "${2-}"
      MODEL_OVERRIDE="$2"
      shift 2
      ;;
    --model=*)
      MODEL_OVERRIDE="${1#*=}"
      shift
      ;;
    --yolo)
      VENDORS_YOLO=1
      shift
      ;;
    --config)
      require_value "$1" "${2-}"
      CONFIG_FILE="$2"
      shift 2
      ;;
    --config=*)
      CONFIG_FILE="${1#*=}"
      shift
      ;;
    --prompt)
      require_value "$1" "${2-}"
      PROMPT_TEXTS+=("$2")
      shift 2
      ;;
    --prompt=*)
      PROMPT_TEXTS+=("${1#*=}")
      shift
      ;;
    --prompt-file)
      require_value "$1" "${2-}"
      PROMPT_FILES+=("$2")
      shift 2
      ;;
    --prompt-file=*)
      PROMPT_FILES+=("${1#*=}")
      shift
      ;;
    --resume-prompt)
      require_value "$1" "${2-}"
      RESUME_PROMPT_TEXTS+=("$2")
      shift 2
      ;;
    --resume-prompt=*)
      RESUME_PROMPT_TEXTS+=("${1#*=}")
      shift
      ;;
    --resume-prompt-file)
      require_value "$1" "${2-}"
      RESUME_PROMPT_FILES+=("$2")
      shift 2
      ;;
    --resume-prompt-file=*)
      RESUME_PROMPT_FILES+=("${1#*=}")
      shift
      ;;
    --system)
      require_value "$1" "${2-}"
      SYSTEM_TEXTS+=("$2")
      shift 2
      ;;
    --system=*)
      SYSTEM_TEXTS+=("${1#*=}")
      shift
      ;;
    --system-file)
      require_value "$1" "${2-}"
      SYSTEM_FILES+=("$2")
      shift 2
      ;;
    --system-file=*)
      SYSTEM_FILES+=("${1#*=}")
      shift
      ;;
    --instruction)
      require_value "$1" "${2-}"
      INSTRUCTION_TEXTS+=("$2")
      shift 2
      ;;
    --instruction=*)
      INSTRUCTION_TEXTS+=("${1#*=}")
      shift
      ;;
    --instruction-file)
      require_value "$1" "${2-}"
      INSTRUCTION_FILES+=("$2")
      shift 2
      ;;
    --instruction-file=*)
      INSTRUCTION_FILES+=("${1#*=}")
      shift
      ;;
    --context-file)
      require_value "$1" "${2-}"
      CONTEXT_FILES+=("$2")
      shift 2
      ;;
    --context-file=*)
      CONTEXT_FILES+=("${1#*=}")
      shift
      ;;
    --cwd)
      require_value "$1" "${2-}"
      VENDORS_CWD="$2"
      shift 2
      ;;
    --cwd=*)
      VENDORS_CWD="${1#*=}"
      shift
      ;;
    --timeout)
      require_value "$1" "${2-}"
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --timeout=*)
      TIMEOUT_SECONDS="${1#*=}"
      shift
      ;;
    --timeout-extend)
      require_value "$1" "${2-}"
      TIMEOUT_EXTEND_SECONDS="$2"
      shift 2
      ;;
    --timeout-extend=*)
      TIMEOUT_EXTEND_SECONDS="${1#*=}"
      shift
      ;;
    --idle-probe-vendor)
      require_value "$1" "${2-}"
      IDLE_PROBE_VENDOR="$2"
      shift 2
      ;;
    --idle-probe-vendor=*)
      IDLE_PROBE_VENDOR="${1#*=}"
      shift
      ;;
    --idle-probe-model)
      require_value "$1" "${2-}"
      IDLE_PROBE_MODEL="$2"
      shift 2
      ;;
    --idle-probe-model=*)
      IDLE_PROBE_MODEL="${1#*=}"
      shift
      ;;
    --idle-probe-effort)
      require_value "$1" "${2-}"
      IDLE_PROBE_EFFORT=$(vendors_lower "$2")
      shift 2
      ;;
    --idle-probe-effort=*)
      IDLE_PROBE_EFFORT="${1#*=}"
      IDLE_PROBE_EFFORT=$(vendors_lower "$IDLE_PROBE_EFFORT")
      shift
      ;;
    --idle-probe-timeout)
      require_value "$1" "${2-}"
      IDLE_PROBE_TIMEOUT="$2"
      shift 2
      ;;
    --idle-probe-timeout=*)
      IDLE_PROBE_TIMEOUT="${1#*=}"
      shift
      ;;
    --idle-probe-max-total)
      require_value "$1" "${2-}"
      IDLE_PROBE_MAX_TOTAL="$2"
      shift 2
      ;;
    --idle-probe-max-total=*)
      IDLE_PROBE_MAX_TOTAL="${1#*=}"
      shift
      ;;
    --speed|--speed=*|--add-dir|--add-dir=*|--tools|--tools=*|--sandbox|--sandbox=*|--output-format|--output-format=*|--json|--schema-json|--schema-json=*)
      die "$1 is not a shared option; pass vendor-specific controls with --native-arg"
      ;;
    --schema-file)
      require_value "$1" "${2-}"
      VENDORS_SCHEMA_FILE="$2"
      shift 2
      ;;
    --schema-file=*)
      VENDORS_SCHEMA_FILE="${1#*=}"
      shift
      ;;
    --native-arg)
      require_value "$1" "${2-}"
      VENDORS_NATIVE_ARGS+=("$2")
      shift 2
      ;;
    --native-arg=*)
      VENDORS_NATIVE_ARGS+=("${1#*=}")
      shift
      ;;
    --env)
      require_value "$1" "${2-}"
      VENDORS_ENV+=("$2")
      shift 2
      ;;
    --env=*)
      VENDORS_ENV+=("${1#*=}")
      shift
      ;;
    --dry-run)
      VENDORS_DRY_RUN=1
      shift
      ;;
    --)
      shift
      if [ "$#" -gt 0 ]; then
        PROMPT_TEXTS+=("$*")
      fi
      break
      ;;
    -*)
      die "unknown option: $1"
      ;;
    *)
      PROMPT_TEXTS+=("$1")
      shift
      ;;
  esac
done

[ "${#VENDOR_RAWS[@]}" -gt 0 ] || die "--vendor is required"

case "$EFFORT" in
  "") ;;
  *)
    EFFORT=$(vendors_normalize_effort "$EFFORT") \
      || die "--effort must be min, low, medium, high, xhigh, or max"
    ;;
esac

# Every digit string below is forced to base 10 once validated: $(( )) reads a
# leading zero as octal, so "0600" would count as 384 and "08" would be an
# error that, under set -e, silently kills the watchdog subshell that does the
# arithmetic (leaving the vendor with no timeout at all).
case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*) die "--timeout must be a non-negative integer" ;;
esac
TIMEOUT_SECONDS=$((10#$TIMEOUT_SECONDS))

case "$TIMEOUT_EXTEND_SECONDS" in
  ''|*[!0-9]*) die "--timeout-extend must be a non-negative integer" ;;
esac
TIMEOUT_EXTEND_SECONDS=$((10#$TIMEOUT_EXTEND_SECONDS))

# 0 would disable the probe's own deadline; a wedged probe call could then
# block the watchdog, and with it the kill, forever.
case "$IDLE_PROBE_TIMEOUT" in
  ''|*[!0-9]*) die "--idle-probe-timeout must be a positive integer" ;;
esac
IDLE_PROBE_TIMEOUT=$((10#$IDLE_PROBE_TIMEOUT))
[ "$IDLE_PROBE_TIMEOUT" -gt 0 ] || die "--idle-probe-timeout must be a positive integer"

case "$IDLE_PROBE_MAX_TOTAL" in
  ''|*[!0-9]*) die "--idle-probe-max-total must be a non-negative integer" ;;
esac
IDLE_PROBE_MAX_TOTAL=$((10#$IDLE_PROBE_MAX_TOTAL))

if [ -n "$IDLE_PROBE_VENDOR" ]; then
  # Validate in a subshell: vendors_normalize_vendor sets the VENDORS_VENDOR_*
  # globals that the per-vendor loop below owns.
  (vendors_normalize_vendor "$IDLE_PROBE_VENDOR" >/dev/null 2>&1) \
    || die "--idle-probe-vendor: unknown vendor: $IDLE_PROBE_VENDOR"
  # The probe only ever runs from the --timeout watchdog; without one it
  # would be accepted and silently do nothing.
  [ "$TIMEOUT_SECONDS" -gt 0 ] || die "--idle-probe-vendor requires --timeout > 0"
  if [ -n "$IDLE_PROBE_EFFORT" ]; then
    IDLE_PROBE_EFFORT=$(vendors_normalize_effort "$IDLE_PROBE_EFFORT") \
      || die "--idle-probe-effort must be min, low, medium, high, xhigh, or max"
  fi
  if [ ! -r "$SCRIPT_DIR/idle-probe.sh" ]; then
    die "--idle-probe-vendor requires $SCRIPT_DIR/idle-probe.sh"
  fi
elif [ -n "$IDLE_PROBE_MODEL" ] || [ -n "$IDLE_PROBE_EFFORT" ]; then
  die "--idle-probe-model and --idle-probe-effort require --idle-probe-vendor"
fi

case "$MIN_SUCCESS" in
  "") ;;
  *[!0-9]*) die "--min-success must be a non-negative integer" ;;
esac

if [ ! -r "$CONFIG_FILE" ]; then
  die "cannot read config: $CONFIG_FILE"
fi

CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"
vendors_read_models "$CONFIG_FILE"

VENDOR_IDS=()
VENDOR_LABELS=()
VENDOR_CLIS=()
OUTPUT_IDS=()

for vendor_raw in "${VENDOR_RAWS[@]}"; do
  vendors_normalize_vendor "$vendor_raw"
  VENDOR_IDS+=("$VENDORS_VENDOR_ID")
  VENDOR_LABELS+=("$vendor_raw")
  VENDOR_CLIS+=("$VENDORS_VENDOR_CLI")
done

if [ "${#CALL_IDS[@]}" -gt 0 ] && [ "${#CALL_IDS[@]}" -ne "${#VENDOR_IDS[@]}" ]; then
  die "when provided, --id must be repeated once per --vendor"
fi

if [ "${#SESSION_KEYS[@]}" -gt 0 ] && [ "${#SESSION_KEYS[@]}" -ne "${#VENDOR_IDS[@]}" ]; then
  die "when provided, --session-key must be repeated once per --vendor"
fi

case "$SESSION_MAX_TURNS" in
  "") ;;
  0|*[!0-9]*) die "--session-max-turns must be a positive integer" ;;
esac
if [ -n "$SESSION_MAX_TURNS" ] && [ "${#SESSION_KEYS[@]}" -eq 0 ]; then
  die "--session-max-turns requires --session-key"
fi

for i in "${!VENDOR_IDS[@]}"; do
  vendor_id="${VENDOR_IDS[$i]}"
  if [ "${#CALL_IDS[@]}" -gt 0 ]; then
    output_id="${CALL_IDS[$i]}"
  else
    count=1
    j=0
    while [ "$j" -lt "$i" ]; do
      if [ "${VENDOR_IDS[$j]}" = "$vendor_id" ]; then
        count=$((count + 1))
      fi
      j=$((j + 1))
    done
    if [ "$count" -eq 1 ]; then
      output_id="$vendor_id"
    else
      output_id="$vendor_id-$count"
    fi
  fi

  case "$output_id" in
    .|..)
      die "--id must name a direct child directory, not $output_id"
      ;;
    ""|*[!A-Za-z0-9_.-]*)
      die "--id may only contain letters, numbers, underscore, dot, and dash"
      ;;
  esac
  for existing in "${OUTPUT_IDS[@]}"; do
    if [ "$existing" = "$output_id" ]; then
      die "duplicate output id selected: $output_id"
    fi
  done
  OUTPUT_IDS+=("$output_id")
done

if [ -z "$MIN_SUCCESS" ]; then
  MIN_SUCCESS="${#VENDOR_IDS[@]}"
fi

if [ "$MIN_SUCCESS" -gt "${#VENDOR_IDS[@]}" ]; then
  die "--min-success cannot exceed selected vendor count"
fi

if [ -n "$VENDORS_CWD" ] && [ ! -d "$VENDORS_CWD" ]; then
  die "--cwd is not a directory: $VENDORS_CWD"
fi
if [ -n "$VENDORS_CWD" ]; then
  VENDORS_CWD=$(cd "$VENDORS_CWD" && pwd -P)
  VENDORS_EFFECTIVE_CWD="$VENDORS_CWD"
else
  # The vendor inherits the invocation directory when --cwd is omitted, so
  # that effective directory must also participate in session identity.
  VENDORS_EFFECTIVE_CWD="$VENDORS_INVOCATION_CWD"
fi

if [ -n "$VENDORS_SCHEMA_FILE" ]; then
  if [ ! -r "$VENDORS_SCHEMA_FILE" ]; then
    die "cannot read --schema-file: $VENDORS_SCHEMA_FILE"
  fi
  VENDORS_SCHEMA_FILE="$(cd "$(dirname "$VENDORS_SCHEMA_FILE")" && pwd)/$(basename "$VENDORS_SCHEMA_FILE")"
  for vid in "${VENDOR_IDS[@]}"; do
    case "$vid" in
      agy|cursor)
        die "--schema-file is not supported on $vid; the $vid CLI has no native schema enforcement"
        ;;
    esac
  done
  export VENDORS_SCHEMA_FILE
fi

for file in "${PROMPT_FILES[@]}" "${RESUME_PROMPT_FILES[@]}" "${SYSTEM_FILES[@]}" "${INSTRUCTION_FILES[@]}" "${CONTEXT_FILES[@]}"; do
  if [ ! -r "$file" ]; then
    die "cannot read file: $file"
  fi
done

for env_pair in "${VENDORS_ENV[@]}"; do
  case "$env_pair" in
    *=*) ;;
    *) die "--env must be NAME=VALUE: $env_pair" ;;
  esac
done

if [ "${#PROMPT_TEXTS[@]}" -eq 0 ] && [ "${#PROMPT_FILES[@]}" -eq 0 ] && [ ! -t 0 ]; then
  stdin_prompt=$(cat)
  if [ -n "$stdin_prompt" ]; then
    PROMPT_TEXTS+=("$stdin_prompt")
  fi
fi

if [ "${#PROMPT_TEXTS[@]}" -eq 0 ] && [ "${#PROMPT_FILES[@]}" -eq 0 ]; then
  die "provide --prompt, --prompt-file, positional prompt text, or stdin"
fi

vendors_session_state_dir() {
  if [ -n "${VENDORS_SESSION_STATE_DIR:-}" ]; then
    printf "%s\n" "$VENDORS_SESSION_STATE_DIR"
  elif [ -n "${XDG_STATE_HOME:-}" ]; then
    printf "%s\n" "$XDG_STATE_HOME/shared-vendors/sessions"
  elif [ -n "${HOME:-}" ]; then
    printf "%s\n" "$HOME/.local/state/shared-vendors/sessions"
  else
    printf "%s\n" "${TMPDIR:-/tmp}/shared-vendors-${UID:-unknown}/sessions"
  fi
}

WORK_DIR=$(mktemp -d /tmp/vendors.XXXXXX)
PIDS=()
OUTPUT_LOCK_DIRS=()
CALL_DIRS=()
CLEANUP_SAFE=1

COORDINATOR_PID=$$

cleanup() {
  local lock_dir=""

  if [ "$CLEANUP_SAFE" != "1" ]; then
    return
  fi
  for lock_dir in "${OUTPUT_LOCK_DIRS[@]}"; do
    rm -f "$lock_dir/owner"
    rmdir "$lock_dir" 2>/dev/null || true
  done
  rm -rf "$WORK_DIR"
}

acquire_output_lock() {
  local output_id="$1"
  local lock_dir="$OUTPUT_DIR/.vendors-call-$output_id.lock"
  local owner=""

  if ! mkdir "$lock_dir" 2>/dev/null; then
    if [ -r "$lock_dir/owner" ]; then
      owner=$(tr '\n' ' ' < "$lock_dir/owner")
    fi
    printf "call.sh: output id is already in use: %s/%s" "$OUTPUT_DIR" "$output_id" >&2
    if [ -n "$owner" ]; then
      printf " (%s)" "$owner" >&2
    fi
    printf "\n" >&2
    printf "call.sh: use a different --output-dir, or verify the recorded process is gone before removing a stale lock\n" >&2
    exit 2
  fi
  OUTPUT_LOCK_DIRS+=("$lock_dir")
  {
    printf "pid=%s\n" "$COORDINATOR_PID"
    printf "started_epoch=%s\n" "$(date +%s)"
  } > "$lock_dir/owner"
}
kill_tree() {
  local pid="$1"
  local signal_name="${2:-TERM}"
  local child=""

  if [ -z "$pid" ]; then
    return
  fi

  while read -r child; do
    [ -n "$child" ] || continue
    kill_tree "$child" "$signal_name"
  done < <(pgrep -P "$pid" 2>/dev/null || true)

  kill "-$signal_name" "$pid" 2>/dev/null || true
}

stream_size() {
  # Size in bytes of the vendor's stream file; 0 if it does not exist yet.
  if [ -f "$VENDORS_STREAM_FILE" ]; then
    wc -c < "$VENDORS_STREAM_FILE" | tr -d '[:space:]'
  else
    printf 0
  fi
}

file_mtime_epoch() {
  # Portable mtime (follows symlinks: the stream file is usually a symlink to
  # the vendor transcript). Selected per host OS like vendors_host_os, never
  # by running one platform's form and sniffing its error output.
  local file="$1"
  case "$(vendors_host_os)" in
    darwin) stat -L -f %m "$file" 2>/dev/null || true ;;
    linux) stat -L -c %Y "$file" 2>/dev/null || true ;;
    *) printf '' ;;
  esac
}

stream_idle_seconds() {
  # Seconds since the vendor's stream output last grew: stream-file mtime when
  # it exists and can be read, otherwise the epoch given as fallback.
  local fallback_epoch="$1"
  local now=""
  local mtime=""

  now=$(date +%s)
  if [ -f "$VENDORS_STREAM_FILE" ]; then
    mtime=$(file_mtime_epoch "$VENDORS_STREAM_FILE")
  fi
  case "$mtime" in
    ''|*[!0-9]*) mtime="$fallback_epoch" ;;
  esac
  if [ "$now" -gt "$mtime" ]; then
    printf '%s' "$((now - mtime))"
  else
    printf 0
  fi
}

collect_process_tree() {
  local pid="$1"
  local child=""

  while read -r child; do
    [ -n "$child" ] || continue
    collect_process_tree "$child"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  SIGNAL_TARGET_PIDS+=("$pid")
}

pid_has_live_work() {
  local pid="$1"
  local stat=""

  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  stat=$(ps -o stat= -p "$pid" 2>/dev/null | awk 'NR == 1 { print $1 }' || true)
  case "$stat" in
    Z*) return 1 ;;
    "") return 0 ;;
    *) return 0 ;;
  esac
}

snapshot_has_live_work() {
  # True when any pid listed in $1 (one per line), other than $2, is alive
  # and not a zombie.
  local targets_file="$1"
  local skip_pid="${2:-}"
  local target=""

  while read -r target; do
    [ -n "$target" ] && [ "$target" != "$skip_pid" ] || continue
    if pid_has_live_work "$target"; then
      return 0
    fi
  done < "$targets_file"
  return 1
}

reap_timed_out_tree() {
  # SIGKILL escalation for a timed-out call, run by run_one_vendor after
  # `wait "$RUN_PID"` returns with the timeout marker present.
  #
  # Why it lives here and not in the watchdog: the watchdog sends TERM
  # leaves-first through kill_tree and ends with RUN_PID, a bash subshell
  # with the default TERM disposition. RUN_PID therefore dies on that first
  # TERM, the parent's wait returns, and the parent tears the watchdog down
  # while it is still in its `sleep 2` -- its KILL pass never ran. A vendor
  # CLI that ignores TERM survived as an orphan: reparented to init, no
  # longer reachable from RUN_PID with `pgrep -P`, and still writing into
  # the call dir after the status block has reported a timeout.
  #
  # So the watchdog records the tree it is about to signal in $1
  # (collect_process_tree, before any TERM), and this walks that snapshot:
  # every listed pid already received TERM, so give it two seconds, KILL
  # whatever still has live work, and wait for the kernel to tear it down.
  # $2 is RUN_PID, already reaped by wait (its pid is free for reuse), so it
  # is skipped. Process groups would be tidier, but bash 3.2 has no setpgid,
  # macOS has no setsid(1), and `set -m` would change job semantics for the
  # whole coordinator; a pid snapshot taken microseconds before TERM is the
  # least invasive reliable handle, and it is the same handle handle_signal
  # already relies on.
  local targets_file="$1"
  local reaped_pid="${2:-}"
  local target=""
  local tries=0

  [ -r "$targets_file" ] || return 0
  tries=0
  while snapshot_has_live_work "$targets_file" "$reaped_pid"; do
    [ "$tries" -lt 2 ] || break
    sleep 1
    tries=$((tries + 1))
  done
  snapshot_has_live_work "$targets_file" "$reaped_pid" || return 0
  while read -r target; do
    [ -n "$target" ] && [ "$target" != "$reaped_pid" ] || continue
    if pid_has_live_work "$target"; then
      kill -KILL "$target" 2>/dev/null || true
    fi
  done < "$targets_file"
  tries=0
  while snapshot_has_live_work "$targets_file" "$reaped_pid"; do
    [ "$tries" -lt 3 ] || break
    sleep 1
    tries=$((tries + 1))
  done
  if snapshot_has_live_work "$targets_file" "$reaped_pid"; then
    printf "timeout: part of the vendor process tree survived SIGKILL escalation; see %s\n" \
      "$targets_file" >&2
  fi
}

handle_signal() {
  local code="$1"
  local pid=""
  local target=""
  local all_stopped=1
  local py=""
  local plan_file=""
  local output_id=""
  local -a interrupt_args=()

  # Ignored dispositions survive exec, so the cleanup helper cannot be struck
  # by a second copy of the signal while it drains the dedicated process group.
  trap '' INT TERM HUP

  if [ "${#SESSION_KEYS[@]}" -gt 0 ]; then
    py=$(vendors_session_python || true)
    if [ -n "$py" ]; then
      interrupt_args=(
        "$SCRIPT_DIR/session-state.py" interrupt
        --process-group "$COORDINATOR_PID"
        --coordinator-pid "$COORDINATOR_PID"
      )
      if [ -n "$OUTPUT_DIR" ]; then
        for output_id in "${OUTPUT_IDS[@]}"; do
          plan_file="$OUTPUT_DIR/$output_id/session-plan.json"
          if [ -r "$plan_file" ]; then
            interrupt_args+=(--plan "$plan_file")
          fi
        done
      fi
      if "$py" "${interrupt_args[@]}" >/dev/null; then
        for pid in "${PIDS[@]}"; do
          [ -n "$pid" ] || continue
          wait "$pid" 2>/dev/null || true
        done
        cleanup
        trap - EXIT
        exit "$code"
      fi
    fi

    # The kernel-scoped verifier failed, so best-effort termination is still
    # appropriate, but leases/output locks must remain fail closed.
    CLEANUP_SAFE=0
    printf "call.sh: could not verify keyed process-group shutdown; retaining session/output locks fail closed\n" >&2
  else
    if ! command -v pgrep >/dev/null 2>&1; then
      all_stopped=0
    fi
  fi

  SIGNAL_TARGET_PIDS=()
  for pid in "${PIDS[@]}"; do
    [ -n "$pid" ] || continue
    collect_process_tree "$pid"
  done
  for target in "${SIGNAL_TARGET_PIDS[@]}"; do
    kill -TERM "$target" 2>/dev/null || true
  done
  sleep 1
  for target in "${SIGNAL_TARGET_PIDS[@]}"; do
    if pid_has_live_work "$target"; then
      kill -KILL "$target" 2>/dev/null || true
    fi
  done
  for pid in "${PIDS[@]}"; do
    [ -n "$pid" ] || continue
    wait "$pid" 2>/dev/null || true
  done
  if [ "${#SESSION_KEYS[@]}" -eq 0 ]; then
    for target in "${SIGNAL_TARGET_PIDS[@]}"; do
      if pid_has_live_work "$target"; then
        all_stopped=0
      fi
    done
    if [ "$all_stopped" != "1" ]; then
      CLEANUP_SAFE=0
      printf "call.sh: signal cleanup could not verify subprocess shutdown; retaining locks and temporary state fail closed\n" >&2
    fi
  fi
  cleanup
  trap - EXIT
  exit "$code"
}
trap cleanup EXIT
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM
trap 'handle_signal 129' HUP

SYSTEM_FILE="$WORK_DIR/system.txt"
BASE_PROMPT_FILE="$WORK_DIR/base-prompt.txt"
BASE_RESUME_PROMPT_FILE="$WORK_DIR/base-resume-prompt.txt"

{
  for file in "${SYSTEM_FILES[@]}"; do
    cat "$file"
    printf "\n"
  done
  for text in "${SYSTEM_TEXTS[@]}"; do
    printf "%s\n" "$text"
  done
} > "$SYSTEM_FILE"

if [ -s "$SYSTEM_FILE" ]; then
  VENDORS_SYSTEM_PROMPT=$(cat "$SYSTEM_FILE")
fi

{
  for file in "${PROMPT_FILES[@]}"; do
    cat "$file"
    printf "\n"
  done
  for text in "${PROMPT_TEXTS[@]}"; do
    printf "%s\n" "$text"
  done

  if [ "${#INSTRUCTION_FILES[@]}" -gt 0 ] || [ "${#INSTRUCTION_TEXTS[@]}" -gt 0 ]; then
    printf "\nAdditional instructions:\n"
    for file in "${INSTRUCTION_FILES[@]}"; do
      cat "$file"
      printf "\n"
    done
    for text in "${INSTRUCTION_TEXTS[@]}"; do
      printf "%s\n" "$text"
    done
  fi

  if [ "${#CONTEXT_FILES[@]}" -gt 0 ]; then
    printf "\n\nAttached context files:\n"
    printf "The following files are inlined by the shared vendor wrapper so every vendor receives identical bytes.\n"
    for file in "${CONTEXT_FILES[@]}"; do
      size_bytes=$(wc -c < "$file" | tr -d ' ')
      sha=$(file_sha256 "$file")
      printf "\n----- BEGIN CONTEXT FILE: %s sha256:%s size_bytes:%s -----\n" "$file" "$sha" "$size_bytes"
      cat "$file"
      printf "\n----- END CONTEXT FILE: %s -----\n" "$file"
    done
  fi
} > "$BASE_PROMPT_FILE"

{
  for file in "${RESUME_PROMPT_FILES[@]}"; do
    cat "$file"
    printf "\n"
  done
  for text in "${RESUME_PROMPT_TEXTS[@]}"; do
    printf "%s\n" "$text"
  done
} > "$BASE_RESUME_PROMPT_FILE"

build_prompt_for_vendor() {
  local vendor_id="$1"
  local output_id="$2"
  local prompt_file="$WORK_DIR/$output_id.prompt.txt"

  if [ "$VENDORS_SESSION_MODE" = "resume" ] && [ -s "$BASE_RESUME_PROMPT_FILE" ]; then
    cat "$BASE_RESUME_PROMPT_FILE" > "$prompt_file"
  elif [ "$vendor_id" != "claude" ] && [ -s "$SYSTEM_FILE" ]; then
    {
      printf "System instructions:\n"
      cat "$SYSTEM_FILE"
      printf "\n"
      cat "$BASE_PROMPT_FILE"
    } > "$prompt_file"
  else
    cat "$BASE_PROMPT_FILE" > "$prompt_file"
  fi

  printf "%s\n" "$prompt_file"
}

vendors_session_missing() {
  local output_file="$1"
  local transcript_file="$2"
  LC_ALL=C grep -Eqi \
    '(session|conversation|thread|rollout).{0,80}(not found|does not exist|missing|unknown|invalid)|no .{0,40}(session|conversation|thread|rollout)|cannot .{0,40}(find|resume)|unable to resume' \
    "$output_file" "$transcript_file" 2>/dev/null
}

vendors_session_arg_conflict() {
  local vendor_id="$1"
  local arg=""

  for arg in "${VENDORS_NATIVE_ARGS[@]}"; do
    case "$vendor_id:$arg" in
      openai:resume|openai:--last|openai:--last=*|openai:--all|openai:--all=*|openai:--ephemeral|openai:--ephemeral=*) ;;
      claude:--resume|claude:--resume=*|claude:-r|claude:-r=*|claude:-r?*|claude:--continue|claude:-c|claude:--session-id|claude:--session-id=*|claude:--fork-session|claude:--fork-session=*|claude:--no-session-persistence|claude:--from-pr|claude:--from-pr=*) ;;
      agy:--conversation|agy:--conversation=*|agy:--continue|agy:-c|agy:--output-format|agy:--output-format=*) ;;
      cursor:resume|cursor:--resume|cursor:--resume=*|cursor:--continue|cursor:--continue=*|cursor:--output-format|cursor:--output-format=*) ;;
      grok:resume|grok:--resume|grok:--resume=*|grok:-r|grok:-r=*|grok:-r?*|grok:--continue|grok:-c|grok:--session-id|grok:--session-id=*|grok:-s|grok:-s=*|grok:-s?*|grok:--fork-session|grok:--fork-session=*) ;;
      *) continue ;;
    esac
    printf "%s\n" "$arg"
    return 0
  done
  return 1
}

run_one_vendor() {
  local label="$1"
  local vendor_id="$2"
  local cli="$3"
  local output_id="$4"
  local output_file="$5"
  local log_file="${6:-}"
  local status_file="${7:-}"
  local session_key="${8:-}"
  local session_max_turns="${9:-}"
  local call_dir=""
  local timeout_marker=""
  local usage_file=""
  local prompt_file=""
  local code=0
  local reason=""
  local session_helper="$SCRIPT_DIR/session-state.py"
  local session_state_dir=""
  local session_plan_file=""
  local session_result_file=""
  local session_lease_sec=28860
  local observed_session_id=""
  local session_invalidated=0
  local session_observe_status=0
  local session_finalize_status=0
  local session_owner_pid=""
  local native_arg=""
  local py=""
  local -a session_plan_args=()
  local idle_probe_verdict_file=""
  local idle_probe_verdicts=0
  local idle_probe_last_verdict=""
  local run_started_epoch=""
  local kill_targets_file=""

  call_dir=$(dirname "$output_file")
  timeout_marker="$call_dir/timed-out"
  usage_file="$call_dir/usage.json"
  session_plan_file="$call_dir/session-plan.json"
  session_result_file="$call_dir/session.json"
  VENDORS_STREAM_FILE="$call_dir/stream"
  rm -f "$VENDORS_STREAM_FILE"
  if [ "$vendor_id" = "openai" ]; then
    VENDORS_TRANSCRIPT_FILE="$call_dir/codex-transcript.txt"
    ln -s "$(basename "$VENDORS_TRANSCRIPT_FILE")" "$VENDORS_STREAM_FILE" 2>/dev/null \
      || VENDORS_STREAM_FILE="$VENDORS_TRANSCRIPT_FILE"
  elif [ "$vendor_id" = "grok" ]; then
    VENDORS_TRANSCRIPT_FILE="$call_dir/grok-transcript.jsonl"
    ln -s "$(basename "$VENDORS_TRANSCRIPT_FILE")" "$VENDORS_STREAM_FILE" 2>/dev/null \
      || VENDORS_STREAM_FILE="$VENDORS_TRANSCRIPT_FILE"
  else
    VENDORS_TRANSCRIPT_FILE="$output_file"
    ln -s "$(basename "$output_file")" "$VENDORS_STREAM_FILE" 2>/dev/null \
      || VENDORS_STREAM_FILE="$output_file"
  fi

  if ! command -v "$cli" >/dev/null 2>&1; then
    printf '{"available":false,"provider":"%s","total_tokens":null,"reason":"CLI not on PATH"}\n' \
      "$vendor_id" > "$usage_file"
    if [ -n "$status_file" ]; then
      {
        printf "vendor=%s\n" "$vendor_id"
        printf "cli=%s\n" "$cli"
        printf "id=%s\n" "$output_id"
        printf "label=%s\n" "$label"
        printf "exit_code=127\n"
        printf "output=%s\n" "$output_file"
        printf "stream=%s\n" "$VENDORS_STREAM_FILE"
        printf "log=%s\n" "$log_file"
        printf "usage=%s\n" "$usage_file"
        printf "reason=CLI not on PATH\n"
      } > "$status_file"
    else
      printf "%s CLI is not on PATH\n" "$cli" >&2
    fi
    return 127
  fi

  VENDORS_VENDOR_ID="$vendor_id"
  VENDORS_VENDOR_CLI="$cli"
  VENDORS_RESOLVED_MODEL=$(vendors_select_model "$vendor_id" "$MODEL_OVERRIDE")
  VENDORS_RESOLVED_EFFORT=$(vendors_map_effort "$vendor_id" "$EFFORT")
  VENDORS_SESSION_KEY="$session_key"
  VENDORS_SESSION_MODE=""
  VENDORS_SESSION_ID=""
  VENDORS_SESSION_PLAN_FILE="$session_plan_file"
  VENDORS_SESSION_RESULT_FILE="$session_result_file"

  if [ -n "$session_key" ]; then
    py=$(vendors_session_python || true)
    if [ -z "$py" ]; then
      printf "session support requires Python 3.10+\n" >&2
      printf '{"available":false,"provider":"%s","total_tokens":null,"reason":"session support requires Python 3.10+"}\n' \
        "$vendor_id" > "$usage_file"
      {
        printf "vendor=%s\n" "$vendor_id"
        printf "cli=%s\n" "$cli"
        printf "id=%s\n" "$output_id"
        printf "exit_code=69\n"
        printf "output=%s\n" "$output_file"
        printf "stream=%s\n" "$VENDORS_STREAM_FILE"
        printf "log=%s\n" "$log_file"
        printf "usage=%s\n" "$usage_file"
        printf "reason=session support requires Python 3.10+\n"
      } > "$status_file"
      return 69
    fi
    session_state_dir=$(vendors_session_state_dir)
    if [ "$TIMEOUT_SECONDS" -gt 0 ]; then
      session_lease_sec=$((TIMEOUT_SECONDS + 60))
    fi
    # Bash 3.2 has no BASHPID and `$$` keeps the top coordinator's pid in an
    # async function. Process substitution launches this sh directly from the
    # current supervisor shell, so its PPID is the lease owner we need.
    read -r session_owner_pid < <(sh -c 'printf "%s\n" "$PPID"')
    session_plan_args=(
      "$session_helper" plan
      --state-dir "$session_state_dir"
      --key "$session_key"
      --vendor "$vendor_id"
      --model "${VENDORS_RESOLVED_MODEL:-}"
      --cwd "$VENDORS_EFFECTIVE_CWD"
      --owner-pid "$session_owner_pid"
      --lease-sec "$session_lease_sec"
      --output "$session_plan_file"
    )
    if [ -n "$session_max_turns" ]; then
      session_plan_args+=(--max-turns "$session_max_turns")
    fi
    for native_arg in "${VENDORS_NATIVE_ARGS[@]}"; do
      session_plan_args+=("--transport-arg=$native_arg")
    done
    if ! "$py" "${session_plan_args[@]}"; then
      printf "failed to acquire session lease\n" >&2
      printf '{"available":false,"provider":"%s","total_tokens":null,"reason":"session lease unavailable"}\n' \
        "$vendor_id" > "$usage_file"
      {
        printf "vendor=%s\n" "$vendor_id"
        printf "cli=%s\n" "$cli"
        printf "id=%s\n" "$output_id"
        printf "exit_code=75\n"
        printf "output=%s\n" "$output_file"
        printf "stream=%s\n" "$VENDORS_STREAM_FILE"
        printf "log=%s\n" "$log_file"
        printf "usage=%s\n" "$usage_file"
        printf "reason=session lease unavailable\n"
      } > "$status_file"
      return 75
    fi
    VENDORS_SESSION_MODE=$("$py" "$session_helper" field --plan "$session_plan_file" --name mode)
    VENDORS_SESSION_ID=$("$py" "$session_helper" field --plan "$session_plan_file" --name session_id)
  fi

  # Session mode determines whether the full initial prompt or the optional
  # smaller continuation prompt is delivered.
  prompt_file=$(build_prompt_for_vendor "$vendor_id" "$output_id")

  run_status=0
  if [ "$TIMEOUT_SECONDS" -gt 0 ] && [ "$VENDORS_DRY_RUN" != "1" ]; then
    # The watchdog subshell cannot set this function's variables, so it
    # appends every idle-probe verdict to a work file that the status block
    # reads back after the run, and writes the pids it is about to TERM to
    # a file the parent escalates from (reap_timed_out_tree).
    idle_probe_verdict_file="$WORK_DIR/$output_id.idle-probe-verdicts"
    kill_targets_file="$WORK_DIR/$output_id.kill-targets"
    : > "$idle_probe_verdict_file"
    rm -f "$kill_targets_file"
    run_started_epoch=$(date +%s)
    vendors_run "$vendor_id" "$prompt_file" "$output_file" &
    RUN_PID=$!
    (
      sleep "$TIMEOUT_SECONDS"
      # With --timeout-extend, a deadline reached while output is still
      # flowing is not a stall: grant another window whenever the stream
      # file grew since the previous check, and kill only after a full
      # window passes with no new output.
      prev_size=0
      probe_count=0
      probe_granted=0
      while :; do
        while [ "$TIMEOUT_EXTEND_SECONDS" -gt 0 ]; do
          cur_size=$(stream_size)
          if [ "$cur_size" -le "$prev_size" ]; then
            break
          fi
          prev_size="$cur_size"
          sleep "$TIMEOUT_EXTEND_SECONDS"
        done
        # The mechanical watchdog would kill here. With --idle-probe-vendor,
        # ask a cheap model first: `extend N` sleeps N seconds (bounded by
        # the remaining --idle-probe-max-total budget) and returns to the
        # extend-window check; `kill`, an exhausted budget, or any probe
        # failure falls through to the kill below. The probe's own call.sh
        # gets no idle-probe flags (no recursion) and no --session-key, but
        # does get this call's --config so it resolves models from the same
        # mapping.
        [ -n "$IDLE_PROBE_VENDOR" ] || break
        probe_remaining=$((IDLE_PROBE_MAX_TOTAL - probe_granted))
        if [ "$probe_remaining" -le 0 ]; then
          printf "idle-probe: extension budget exhausted (%ss granted of %ss); killing\n" \
            "$probe_granted" "$IDLE_PROBE_MAX_TOTAL"
          break
        fi
        probe_count=$((probe_count + 1))
        probe_idle_sec=$(stream_idle_seconds "$run_started_epoch")
        probe_out_dir="$call_dir/idle-probe/$probe_count"
        probe_verdict_file="$probe_out_dir/verdict"
        probe_deadline_marker="$probe_out_dir/hard-deadline"
        probe_hard_deadline=$((IDLE_PROBE_TIMEOUT + 30))
        mkdir -p "$probe_out_dir"
        probe_args=(
          "$SCRIPT_DIR/idle-probe.sh"
          --pid "$RUN_PID"
          --idle-sec "$probe_idle_sec"
          --idle-cap-sec "$TIMEOUT_SECONDS"
          --label "$output_id"
          --stream "$VENDORS_STREAM_FILE"
          --stdout "$output_file"
          --stderr "$log_file"
          --probe-vendor "$IDLE_PROBE_VENDOR"
          --probe-timeout "$IDLE_PROBE_TIMEOUT"
          --output-dir "$probe_out_dir"
          --config "$CONFIG_FILE"
        )
        if [ -n "$IDLE_PROBE_MODEL" ]; then
          probe_args+=(--probe-model "$IDLE_PROBE_MODEL")
        fi
        if [ -n "$IDLE_PROBE_EFFORT" ]; then
          probe_args+=(--probe-effort "$IDLE_PROBE_EFFORT")
        fi
        printf "idle-probe: probe %s after %ss idle (cap %ss) via %s\n" \
          "$probe_count" "$probe_idle_sec" "$TIMEOUT_SECONDS" "$IDLE_PROBE_VENDOR"
        # Evidence snapshot: a `kill` verdict is trusted only if the stream
        # stayed still while the probe ran (stale-verdict check below).
        probe_pre_size=$(stream_size)
        probe_pre_mtime=$(file_mtime_epoch "$VENDORS_STREAM_FILE")
        # idle-probe.sh enforces its own --probe-timeout, but a probe wedged
        # outside that window (its call.sh stuck before the vendor call, a
        # hung PTY) would otherwise block this watchdog, and with it the
        # kill, forever. Run it under a hard deadline of its own.
        probe_status=0
        bash "${probe_args[@]}" > "$probe_verdict_file" &
        probe_pid=$!
        (
          sleep "$probe_hard_deadline"
          : > "$probe_deadline_marker"
          kill_tree "$probe_pid" TERM
          sleep 2
          kill_tree "$probe_pid" KILL
        ) &
        probe_guard_pid=$!
        wait "$probe_pid" 2>/dev/null || probe_status=$?
        kill_tree "$probe_guard_pid" TERM
        wait "$probe_guard_pid" 2>/dev/null || true
        if [ -e "$probe_deadline_marker" ]; then
          printf "idle-probe: probe %s exceeded its hard deadline (%ss); treating as kill\n" \
            "$probe_count" "$probe_hard_deadline"
          probe_verdict="kill"
          probe_rationale=""
        elif [ "$probe_status" -ne 0 ]; then
          probe_verdict="kill"
          probe_rationale="probe exited $probe_status"
        else
          probe_verdict=$(head -n 1 "$probe_verdict_file" 2>/dev/null || true)
          probe_rationale=$(sed -n '2p' "$probe_verdict_file" 2>/dev/null || true)
        fi
        [ -n "$probe_verdict" ] || probe_verdict="kill"
        printf "%s\n" "$probe_verdict" >> "$idle_probe_verdict_file"
        printf "idle-probe: verdict %s: %s%s\n" "$probe_count" "$probe_verdict" \
          "${probe_rationale:+ ($probe_rationale)}"
        case "$probe_verdict" in
          "extend "*)
            probe_grant="${probe_verdict#extend }"
            case "$probe_grant" in
              ''|*[!0-9]*) probe_grant=0 ;;
            esac
            # Base 10 (see the --timeout validation); more than nine digits
            # is "huge", not an overflow.
            if [ "${#probe_grant}" -gt 9 ]; then
              probe_grant="$probe_remaining"
            else
              probe_grant=$((10#$probe_grant))
            fi
            if [ "$probe_grant" -gt "$probe_remaining" ]; then
              probe_grant="$probe_remaining"
            fi
            if [ "$probe_grant" -le 0 ]; then
              break
            fi
            probe_granted=$((probe_granted + probe_grant))
            printf "idle-probe: extending %ss (%ss of %ss budget used)\n" \
              "$probe_grant" "$probe_granted" "$IDLE_PROBE_MAX_TOTAL"
            sleep "$probe_grant"
            ;;
          *)
            # A kill verdict judged evidence gathered before the probe ran.
            # If the vendor exited or its stream grew in the meantime, that
            # evidence is stale: discard the verdict rather than kill a call
            # that just resumed.
            if ! pid_has_live_work "$RUN_PID"; then
              printf "idle-probe: vendor exited while probe %s ran; discarding stale kill verdict\n" \
                "$probe_count"
              exit 0
            fi
            probe_post_size=$(stream_size)
            probe_post_mtime=$(file_mtime_epoch "$VENDORS_STREAM_FILE")
            if [ "$probe_post_size" -gt "$probe_pre_size" ] \
                || [ "$probe_post_mtime" != "$probe_pre_mtime" ]; then
              printf "idle-probe: stream grew while probe %s ran (%s -> %s bytes); discarding stale kill verdict\n" \
                "$probe_count" "$probe_pre_size" "$probe_post_size"
              # Back to the extend-window check, which sees the growth.
              prev_size="$probe_pre_size"
              if [ "$TIMEOUT_EXTEND_SECONDS" -le 0 ]; then
                # No extend windows configured: wait one base window for
                # the resumed stream, charged to the probe budget so a
                # trickle of output cannot re-probe forever.
                probe_grant="$TIMEOUT_SECONDS"
                if [ "$probe_grant" -gt "$probe_remaining" ]; then
                  probe_grant="$probe_remaining"
                fi
                probe_granted=$((probe_granted + probe_grant))
                printf "idle-probe: waiting %ss for the resumed stream (%ss of %ss budget used)\n" \
                  "$probe_grant" "$probe_granted" "$IDLE_PROBE_MAX_TOTAL"
                sleep "$probe_grant"
              fi
              continue
            fi
            break
            ;;
        esac
      done
      # Nothing to kill if the vendor finished while the watchdog deliberated
      # (and no timeout to report: the run's own exit status stands).
      if ! pid_has_live_work "$RUN_PID"; then
        exit 0
      fi
      # Snapshot the tree BEFORE signalling: RUN_PID dies on the TERM below,
      # after which its surviving descendants are orphans that pgrep -P can
      # no longer find. The parent escalates from this file
      # (reap_timed_out_tree); the sleep/KILL tail here only matters if
      # RUN_PID itself ignores TERM (call.sh started with SIGTERM ignored),
      # so it works from the same snapshot.
      SIGNAL_TARGET_PIDS=()
      collect_process_tree "$RUN_PID"
      printf '%s\n' "${SIGNAL_TARGET_PIDS[@]}" > "$kill_targets_file"
      : > "$timeout_marker"
      kill_tree "$RUN_PID" TERM
      sleep 2
      while read -r target; do
        [ -n "$target" ] || continue
        if pid_has_live_work "$target"; then
          kill -KILL "$target" 2>/dev/null || true
        fi
      done < "$kill_targets_file"
    ) &
    TIMER_PID=$!
    wait "$RUN_PID" || run_status=$?
    # TIMER_PID is a subshell whose child is the long-lived sleep. Killing
    # only the subshell reparents that sleep to PID 1, leaving one watchdog
    # behind after every successful vendor call. Terminate the whole timer
    # tree and reap the subshell before returning.
    kill_tree "$TIMER_PID" TERM
    wait "$TIMER_PID" 2>/dev/null || true
    if [ -e "$timeout_marker" ]; then
      run_status=124
      # The watchdog's TERM already took RUN_PID down (that is why wait
      # returned); finish the escalation for TERM-resistant descendants
      # before the status block reports, so nothing keeps writing into the
      # call dir afterwards.
      reap_timed_out_tree "$kill_targets_file" "$RUN_PID"
    fi

    if [ -s "$idle_probe_verdict_file" ]; then
      idle_probe_verdicts=$(wc -l < "$idle_probe_verdict_file" | tr -d '[:space:]')
      idle_probe_last_verdict=$(tail -n 1 "$idle_probe_verdict_file")
    fi
  else
    vendors_run "$vendor_id" "$prompt_file" "$output_file" || run_status=$?
  fi
  code="$run_status"

  if [ "$VENDORS_DRY_RUN" != "1" ]; then
    # Capture provider-native session metadata before usage normalization.
    # Claude and Cursor intentionally replace their raw protocol envelope in
    # `out` with assistant prose, so observing afterward would discard the
    # only copy of a newly allocated session id.
    if [ -n "$session_key" ]; then
      "$py" "$session_helper" observe \
        --vendor "$vendor_id" \
        --output-file "$output_file" \
        --transcript-file "$VENDORS_TRANSCRIPT_FILE" \
        --requested-session-id "$VENDORS_SESSION_ID" \
        --mode "$VENDORS_SESSION_MODE" \
        --exit-code "$run_status" \
        --output "$session_result_file" || session_observe_status=$?
      if [ "$session_observe_status" = "0" ]; then
        observed_session_id=$(
          "$py" "$session_helper" field \
            --plan "$session_result_file" --name session_id 2>/dev/null
        ) || session_observe_status=$?
      fi
      if [ "$VENDORS_SESSION_MODE" = "resume" ] \
          && [ "$run_status" != "0" ] \
          && vendors_session_missing "$output_file" "$VENDORS_TRANSCRIPT_FILE"; then
        session_invalidated=1
      fi
    fi

    vendors_collect_usage "$vendor_id" "$output_file" "$VENDORS_TRANSCRIPT_FILE" "$usage_file"
    if [ "$code" = "0" ]; then
      reason=$(vendors_output_error_reason "$vendor_id" "$output_file" || true)
      if [ -n "$reason" ]; then
        code=1
      fi
    fi
    if [ -n "$session_key" ]; then
      if [ "$code" = "0" ] && [ "$session_observe_status" != "0" ]; then
        code=70
        reason="session metadata observation failed"
      elif [ "$code" = "0" ] && [ -z "$observed_session_id" ]; then
        code=70
        reason="session protocol did not yield a native session id"
      fi
      if [ "$VENDORS_SESSION_MODE" = "resume" ] \
          && [ "$code" != "0" ] \
          && [ "$session_invalidated" != "1" ] \
          && vendors_session_missing "$output_file" "$VENDORS_TRANSCRIPT_FILE"; then
        session_invalidated=1
      fi
      if [ "$code" = "0" ]; then
        "$py" "$session_helper" finalize \
          --plan "$session_plan_file" \
          --success \
          --observed-session-id "$observed_session_id" \
          || session_finalize_status=$?
      elif [ "$session_invalidated" = "1" ]; then
        "$py" "$session_helper" finalize \
          --plan "$session_plan_file" --invalidate \
          || session_finalize_status=$?
      else
        "$py" "$session_helper" finalize \
          --plan "$session_plan_file" \
          || session_finalize_status=$?
      fi
      if [ "$session_finalize_status" != "0" ]; then
        if [ "$code" = "0" ]; then
          code=70
        fi
        if [ -z "$reason" ]; then
          reason="session state finalization failed"
        fi
      fi
    fi
  elif [ -n "$session_key" ]; then
    # Dry-run must never leave a live lease or establish a provider session.
    "$py" "$session_helper" finalize --plan "$session_plan_file" \
      || session_finalize_status=$?
    if [ "$session_finalize_status" != "0" ]; then
      code=70
      reason="session state finalization failed"
    fi
  fi

  if [ -n "$status_file" ]; then
    {
      printf "vendor=%s\n" "$vendor_id"
      printf "cli=%s\n" "$cli"
      printf "model=%s\n" "${VENDORS_RESOLVED_MODEL:-}"
      printf "effort=%s\n" "${VENDORS_RESOLVED_EFFORT:-}"
      printf "yolo=%s\n" "$VENDORS_YOLO"
      printf "id=%s\n" "$output_id"
      printf "label=%s\n" "$label"
      printf "exit_code=%s\n" "$code"
      printf "output=%s\n" "$output_file"
      printf "stream=%s\n" "$VENDORS_STREAM_FILE"
      printf "log=%s\n" "$log_file"
      printf "usage=%s\n" "$usage_file"
      if [ -n "$session_key" ]; then
        printf "session_key_hash=%s\n" "$("$py" "$session_helper" field --plan "$session_plan_file" --name identity_hash 2>/dev/null || true)"
        printf "session_mode=%s\n" "$VENDORS_SESSION_MODE"
        printf "session_id=%s\n" "$observed_session_id"
        printf "session=%s\n" "$session_result_file"
        printf "session_invalidated=%s\n" "$session_invalidated"
        printf "session_turn=%s\n" "$("$py" "$session_helper" field --plan "$session_plan_file" --name turn_number 2>/dev/null || true)"
        printf "session_max_turns=%s\n" "$("$py" "$session_helper" field --plan "$session_plan_file" --name max_turns 2>/dev/null || true)"
        printf "session_auto_reset=%s\n" "$("$py" "$session_helper" field --plan "$session_plan_file" --name auto_reset 2>/dev/null || true)"
      fi
      if [ -n "$IDLE_PROBE_VENDOR" ]; then
        printf "idle_probe_verdicts=%s\n" "$idle_probe_verdicts"
        printf "idle_probe_last_verdict=%s\n" "$idle_probe_last_verdict"
      fi
      if [ "$code" = "124" ]; then
        printf "reason=timeout\n"
      elif [ -n "$reason" ]; then
        printf "reason=%s\n" "$reason"
      fi
    } > "$status_file"
  fi

  return "$code"
}

if [ "${#SESSION_KEYS[@]}" -gt 0 ]; then
  for vendor_id in "${VENDOR_IDS[@]}"; do
    conflict=$(vendors_session_arg_conflict "$vendor_id" || true)
    if [ -n "$conflict" ]; then
      die "--session-key owns native session transport for $vendor_id; remove conflicting --native-arg $conflict"
    fi
  done
fi

if [ -z "$OUTPUT_DIR" ]; then
  OUTPUT_DIR=$(mktemp -d /tmp/vendors-output.XXXXXX)
else
  mkdir -p "$OUTPUT_DIR"
fi
OUTPUT_DIR=$(cd "$OUTPUT_DIR" && pwd -P)

# Resolve every existing call directory before locking. A call directory must
# be a real direct child of the canonical output root; traversal aliases and
# symlinked ids could otherwise give two syntactic locks one physical target.
for output_id in "${OUTPUT_IDS[@]}"; do
  call_dir="$OUTPUT_DIR/$output_id"
  if [ -L "$call_dir" ]; then
    die "output call directory must not be a symlink: $call_dir"
  fi
  if [ -e "$call_dir" ] && [ ! -d "$call_dir" ]; then
    die "output path exists and is not a directory: $call_dir"
  fi
  if [ -d "$call_dir" ]; then
    canonical_call_dir=$(cd "$call_dir" && pwd -P)
    if [ "$(dirname "$canonical_call_dir")" != "$OUTPUT_DIR" ] \
        || [ "$(basename "$canonical_call_dir")" != "$output_id" ]; then
      die "output call directory must resolve to a direct child of $OUTPUT_DIR: $call_dir"
    fi
    call_dir="$canonical_call_dir"
  fi
  CALL_DIRS+=("$call_dir")
done

# Reserve every selected output id before creating or truncating any call
# artifact. This makes fan-out startup atomic and prevents two coordinators
# from cross-writing session plans, transcripts, or status files.
for output_id in "${OUTPUT_IDS[@]}"; do
  acquire_output_lock "$output_id"
done

for i in "${!VENDOR_IDS[@]}"; do
  vendor_id="${VENDOR_IDS[$i]}"
  output_id="${OUTPUT_IDS[$i]}"
  call_dir="${CALL_DIRS[$i]}"
  mkdir -p "$call_dir"
  if [ -L "$call_dir" ]; then
    die "output call directory became a symlink after locking: $call_dir"
  fi
  canonical_call_dir=$(cd "$call_dir" && pwd -P)
  if [ "$(dirname "$canonical_call_dir")" != "$OUTPUT_DIR" ] \
      || [ "$(basename "$canonical_call_dir")" != "$output_id" ]; then
    die "output call directory escaped its canonical output root: $call_dir"
  fi
  call_dir="$canonical_call_dir"
  session_key=""
  if [ "${#SESSION_KEYS[@]}" -gt 0 ]; then
    session_key="${SESSION_KEYS[$i]}"
  fi

  run_one_vendor \
    "${VENDOR_LABELS[$i]}" \
    "$vendor_id" \
    "${VENDOR_CLIS[$i]}" \
    "$output_id" \
    "$call_dir/out" \
    "$call_dir/log" \
    "$call_dir/status" \
    "$session_key" \
    "$SESSION_MAX_TURNS" \
    > "$call_dir/log" 2>&1 &
  PIDS+=("$!")
done

for i in "${!PIDS[@]}"; do
  pid="${PIDS[$i]}"
  wait "$pid" 2>/dev/null || true
  # Never retain an already-reaped numeric PID: it can be reused by an
  # unrelated process before a later handled signal in a long fan-out call.
  PIDS[$i]=""
done

SUCCESS=0
FAILED=0

printf "output_dir=%s\n" "$OUTPUT_DIR"
printf "min_success=%s\n" "$MIN_SUCCESS"
printf "\n"

for i in "${!VENDOR_IDS[@]}"; do
  vendor_id="${VENDOR_IDS[$i]}"
  output_id="${OUTPUT_IDS[$i]}"
  status_file="$OUTPUT_DIR/$output_id/status"
  code=$(awk -F= '$1 == "exit_code" { print $2; exit }' "$status_file" 2>/dev/null || printf "missing")
  out_file="$OUTPUT_DIR/$output_id/out"
  log_file="$OUTPUT_DIR/$output_id/log"

  if [ "$code" = "0" ]; then
    printf "[OK] %s vendor=%s output=%s log=%s\n" "$output_id" "$vendor_id" "$out_file" "$log_file"
    SUCCESS=$((SUCCESS + 1))
  else
    printf "[--] %s vendor=%s exit=%s output=%s log=%s\n" "$output_id" "$vendor_id" "$code" "$out_file" "$log_file"
    FAILED=$((FAILED + 1))
  fi

  if [ "$VENDORS_DRY_RUN" = "1" ] && [ -s "$log_file" ]; then
    sed 's/^/  /' "$log_file"
  fi
done

printf "\n"
printf "success=%s failed=%s selected=%s\n" "$SUCCESS" "$FAILED" "${#VENDOR_IDS[@]}"

if [ "$SUCCESS" -lt "$MIN_SUCCESS" ]; then
  printf "Troubleshooting notes: %s\n" "$(cd "$SCRIPT_DIR/.." && pwd)/TROUBLESHOOTING.md" >&2
  exit 1
fi
