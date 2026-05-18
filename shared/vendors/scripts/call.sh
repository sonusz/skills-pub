#!/usr/bin/env bash
# Usage: call.sh --vendor openai|claude|gemini|cursor [--vendor ...] [options] [prompt]
#
# Unified vendor interface. A single --vendor behaves like a normal CLI call;
# repeated --vendor values fan the same prompt out in parallel.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=vendor-launch.sh
. "$SCRIPT_DIR/vendor-launch.sh"

usage() {
  cat <<'USAGE'
Usage:
  scripts/call.sh --vendor openai|claude|gemini|cursor [--vendor NAME...] [options] [prompt]

Vendor selection:
  --vendor NAME                  openai, claude, gemini, or cursor; repeatable
  --min-success N                Required successful calls. Default: all selected

Output:
  --output-dir DIR               Output root for <id>/out, <id>/status, <id>/log
                                 If omitted, a temp dir is kept and printed
  --id ID                        Output directory id; repeatable with --vendor
                                 Default: normalized vendor id, suffixed on duplicates

Selection hints:
  --effort min|low|medium|high|xhigh|max
                                 Best-effort reasoning hint
  --model MODEL                  Override vendors.conf model selection
  --yolo                         Map to each vendor's no-approval mode
  --config FILE                  Model mapping config (default: ../vendors.conf)
  --schema-file FILE             JSON Schema constraining the response shape.
                                 Output lands at <output-dir>/<id>/out as
                                 {"structured_output": <conforming-object>}.
                                 Supported on claude and openai (codex).
                                 gemini and cursor do not enforce schemas natively.

Prompt and instruction input:
  --prompt TEXT                  Prompt text; repeatable
  --prompt-file FILE             Prompt file; repeatable
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
MIN_SUCCESS=""
TIMEOUT_SECONDS=0

VENDORS_CWD=""
VENDORS_DRY_RUN=0
VENDORS_NATIVE_ARGS=()
VENDORS_ENV=()
VENDORS_SYSTEM_PROMPT=""
VENDORS_TRANSCRIPT_FILE=""
VENDORS_SCHEMA_FILE=""

PROMPT_TEXTS=()
PROMPT_FILES=()
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

case "$TIMEOUT_SECONDS" in
  ''|*[!0-9]*) die "--timeout must be a non-negative integer" ;;
esac

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

if [ -n "$VENDORS_SCHEMA_FILE" ]; then
  if [ ! -r "$VENDORS_SCHEMA_FILE" ]; then
    die "cannot read --schema-file: $VENDORS_SCHEMA_FILE"
  fi
  VENDORS_SCHEMA_FILE="$(cd "$(dirname "$VENDORS_SCHEMA_FILE")" && pwd)/$(basename "$VENDORS_SCHEMA_FILE")"
  for vid in "${VENDOR_IDS[@]}"; do
    case "$vid" in
      gemini|cursor)
        die "--schema-file is not supported on $vid; the $vid CLI has no native schema enforcement"
        ;;
    esac
  done
  export VENDORS_SCHEMA_FILE
fi

for file in "${PROMPT_FILES[@]}" "${SYSTEM_FILES[@]}" "${INSTRUCTION_FILES[@]}" "${CONTEXT_FILES[@]}"; do
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

WORK_DIR=$(mktemp -d /tmp/vendors.XXXXXX)
PIDS=()

cleanup() {
  rm -rf "$WORK_DIR"
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
handle_signal() {
  local code="$1"
  local pid=""

  for pid in "${PIDS[@]}"; do
    kill_tree "$pid" TERM
  done
  sleep 1
  for pid in "${PIDS[@]}"; do
    kill_tree "$pid" KILL
  done
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

build_prompt_for_vendor() {
  local vendor_id="$1"
  local output_id="$2"
  local prompt_file="$WORK_DIR/$output_id.prompt.txt"

  if [ "$vendor_id" != "claude" ] && [ -s "$SYSTEM_FILE" ]; then
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

run_one_vendor() {
  local label="$1"
  local vendor_id="$2"
  local cli="$3"
  local output_id="$4"
  local output_file="$5"
  local log_file="${6:-}"
  local status_file="${7:-}"
  local call_dir=""
  local timeout_marker=""
  local usage_file=""
  local prompt_file=""
  local code=0
  local reason=""

  call_dir=$(dirname "$output_file")
  timeout_marker="$call_dir/timed-out"
  usage_file="$call_dir/usage.json"

  if ! command -v "$cli" >/dev/null 2>&1; then
    printf '{"available":false,"provider":"%s","total_tokens":null,"reason":"CLI not on PATH"}\n' \
      "$vendor_id" > "$usage_file"
    if [ -n "$status_file" ]; then
      {
        printf "vendor=%s\n" "$vendor_id"
        printf "id=%s\n" "$output_id"
        printf "label=%s\n" "$label"
        printf "exit_code=127\n"
        printf "output=%s\n" "$output_file"
        printf "log=%s\n" "$log_file"
        printf "usage=%s\n" "$usage_file"
        printf "reason=CLI not on PATH\n"
      } > "$status_file"
    else
      printf "%s CLI is not on PATH\n" "$cli" >&2
    fi
    return 127
  fi

  prompt_file=$(build_prompt_for_vendor "$vendor_id" "$output_id")

  VENDORS_VENDOR_ID="$vendor_id"
  VENDORS_VENDOR_CLI="$cli"
  VENDORS_RESOLVED_MODEL=$(vendors_select_model "$vendor_id" "$MODEL_OVERRIDE")
  VENDORS_RESOLVED_EFFORT=$(vendors_map_effort "$vendor_id" "$EFFORT")
  VENDORS_TRANSCRIPT_FILE="$call_dir/codex-transcript.txt"

  run_status=0
  if [ "$TIMEOUT_SECONDS" -gt 0 ] && [ "$VENDORS_DRY_RUN" != "1" ]; then
    vendors_run "$vendor_id" "$prompt_file" "$output_file" &
    RUN_PID=$!
    (
      sleep "$TIMEOUT_SECONDS"
      : > "$timeout_marker"
      kill_tree "$RUN_PID" TERM
      sleep 2
      kill_tree "$RUN_PID" KILL
    ) &
    TIMER_PID=$!
    wait "$RUN_PID" || run_status=$?
    kill "$TIMER_PID" 2>/dev/null || true
    if [ -e "$timeout_marker" ]; then
      run_status=124
    fi
  else
    vendors_run "$vendor_id" "$prompt_file" "$output_file" || run_status=$?
  fi
  code="$run_status"

  if [ "$VENDORS_DRY_RUN" != "1" ]; then
    vendors_collect_usage "$vendor_id" "$output_file" "$VENDORS_TRANSCRIPT_FILE" "$usage_file"
    if [ "$code" = "0" ]; then
      reason=$(vendors_output_error_reason "$vendor_id" "$output_file" || true)
      if [ -n "$reason" ]; then
        code=1
      fi
    fi
  fi

  if [ -n "$status_file" ]; then
    {
      printf "vendor=%s\n" "$vendor_id"
      printf "id=%s\n" "$output_id"
      printf "label=%s\n" "$label"
      printf "exit_code=%s\n" "$code"
      printf "output=%s\n" "$output_file"
      printf "log=%s\n" "$log_file"
      printf "usage=%s\n" "$usage_file"
      if [ "$code" = "124" ]; then
        printf "reason=timeout\n"
      elif [ -n "$reason" ]; then
        printf "reason=%s\n" "$reason"
      fi
    } > "$status_file"
  fi

  return "$code"
}

if [ -z "$OUTPUT_DIR" ]; then
  OUTPUT_DIR=$(mktemp -d /tmp/vendors-output.XXXXXX)
else
  mkdir -p "$OUTPUT_DIR"
fi

for i in "${!VENDOR_IDS[@]}"; do
  vendor_id="${VENDOR_IDS[$i]}"
  output_id="${OUTPUT_IDS[$i]}"
  call_dir="$OUTPUT_DIR/$output_id"
  if [ -e "$call_dir" ] && [ ! -d "$call_dir" ]; then
    die "output path exists and is not a directory: $call_dir"
  fi
  mkdir -p "$call_dir"

  run_one_vendor \
    "${VENDOR_LABELS[$i]}" \
    "$vendor_id" \
    "${VENDOR_CLIS[$i]}" \
    "$output_id" \
    "$call_dir/out" \
    "$call_dir/log" \
    "$call_dir/status" \
    > "$call_dir/log" 2>&1 &
  PIDS+=("$!")
done

for pid in "${PIDS[@]}"; do
  wait "$pid" 2>/dev/null || true
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
