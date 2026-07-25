#!/usr/bin/env bash
# Shared vendor launch helpers for the vendors skill.
#
# Source this file from scripts/call.sh; do not execute it directly.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  printf "vendor-launch.sh is a library; source it from call.sh.\n" >&2
  exit 2
fi

vendors_conf_get() {
  local config_file="$1"
  local key="$2"

  awk -F= -v key="$key" '
    $0 ~ /^[[:space:]]*(#|$)/ { next }
    $1 == key {
      sub(/^[^=]*=/, "")
      print
      exit
    }
  ' "$config_file"
}

vendors_lower() {
  printf "%s" "$1" | tr '[:upper:]' '[:lower:]'
}

vendors_read_models() {
  local config_file="$1"

  VENDORS_OPENAI_MODEL=$(vendors_conf_get "$config_file" "openai.model")
  VENDORS_CLAUDE_MODEL=$(vendors_conf_get "$config_file" "claude.model")
  VENDORS_AGY_MODEL=$(vendors_conf_get "$config_file" "agy.model")
  VENDORS_CURSOR_MODEL=$(vendors_conf_get "$config_file" "cursor.model")
  VENDORS_GROK_MODEL=$(vendors_conf_get "$config_file" "grok.model")

  # Backward-compatible fallback for configs created before speed was removed.
  [ -n "$VENDORS_OPENAI_MODEL" ] || VENDORS_OPENAI_MODEL=$(vendors_conf_get "$config_file" "openai.normal")
  [ -n "$VENDORS_CLAUDE_MODEL" ] || VENDORS_CLAUDE_MODEL=$(vendors_conf_get "$config_file" "claude.normal")
  [ -n "$VENDORS_AGY_MODEL" ] || VENDORS_AGY_MODEL=$(vendors_conf_get "$config_file" "agy.normal")
  [ -n "$VENDORS_CURSOR_MODEL" ] || VENDORS_CURSOR_MODEL=$(vendors_conf_get "$config_file" "cursor.normal")
  [ -n "$VENDORS_GROK_MODEL" ] || VENDORS_GROK_MODEL=$(vendors_conf_get "$config_file" "grok.normal")
}

vendors_normalize_vendor() {
  local raw

  raw=$(vendors_lower "$1")

  case "$raw" in
    openai|codex|gpt)
      VENDORS_VENDOR_ID="openai"
      VENDORS_VENDOR_CLI="codex"
      ;;
    claude|anthropic)
      VENDORS_VENDOR_ID="claude"
      VENDORS_VENDOR_CLI="claude"
      ;;
    agy|antigravity)
      VENDORS_VENDOR_ID="agy"
      VENDORS_VENDOR_CLI="agy"
      ;;
    cursor|cursor-agent|anysphere)
      VENDORS_VENDOR_ID="cursor"
      VENDORS_VENDOR_CLI="cursor-agent"
      ;;
    grok|xai)
      VENDORS_VENDOR_ID="grok"
      VENDORS_VENDOR_CLI="grok"
      ;;
    *)
      printf "unknown vendor: %s (expected openai, claude, agy, cursor, or grok)\n" "$1" >&2
      return 2
      ;;
  esac
}

vendors_select_model() {
  local vendor="$1"
  local override="$2"
  local selected=""

  if [ -n "$override" ]; then
    printf "%s\n" "$override"
    return
  fi

  case "$vendor" in
    openai)
      selected="$VENDORS_OPENAI_MODEL"
      ;;
    claude)
      selected="$VENDORS_CLAUDE_MODEL"
      ;;
    agy)
      selected="$VENDORS_AGY_MODEL"
      ;;
    cursor)
      selected="$VENDORS_CURSOR_MODEL"
      ;;
    grok)
      selected="$VENDORS_GROK_MODEL"
      ;;
    *)
      printf "unknown normalized vendor: %s\n" "$vendor" >&2
      return 2
      ;;
  esac

  printf "%s\n" "$selected"
}

vendors_normalize_effort() {
  local raw

  raw=$(vendors_lower "$1")

  raw="${raw//_/-}"
  raw="${raw// /-}"
  case "$raw" in
    min|minimal|minimum) printf "min\n" ;;
    low) printf "low\n" ;;
    med|mid|medium) printf "medium\n" ;;
    hi|high) printf "high\n" ;;
    xhigh|x-high|extra-high|extrahigh) printf "xhigh\n" ;;
    max|maximum) printf "max\n" ;;
    *) return 1 ;;
  esac
}

vendors_effort_rank() {
  case "$1" in
    min) printf "0\n" ;;
    low) printf "1\n" ;;
    medium) printf "2\n" ;;
    high) printf "3\n" ;;
    xhigh) printf "4\n" ;;
    max) printf "5\n" ;;
    *) return 1 ;;
  esac
}

vendors_supported_efforts() {
  case "$1" in
    openai) printf "low medium high xhigh\n" ;;
    claude) printf "low medium high xhigh max\n" ;;
    agy) printf "\n" ;;
    cursor) printf "\n" ;;
    grok) printf "low medium high\n" ;;
    *)
      printf "unknown vendor %s\n" "$1" >&2
      return 2
      ;;
  esac
}

vendors_map_effort() {
  local vendor="$1"
  local effort="$2"
  local normalized=""
  local desired_rank=""
  local supported_text=""
  local best_up=""
  local best_down=""
  local best_up_rank=999
  local best_down_rank=-1
  local candidate=""
  local candidate_rank=0
  local -a supported=()

  if [ -z "$effort" ]; then
    printf "\n"
    return
  fi

  normalized=$(vendors_normalize_effort "$effort") || {
    printf "unsupported effort %s; expected min, low, medium, high, xhigh, or max\n" \
      "$effort" >&2
    return 2
  }
  desired_rank=$(vendors_effort_rank "$normalized")
  supported_text=$(vendors_supported_efforts "$vendor") || return 2

  # Vendor has no native effort knob; accept the shared hint and use default.
  if [ -z "$supported_text" ]; then
    printf "\n"
    return
  fi

  read -r -a supported <<< "$supported_text"
  for candidate in "${supported[@]}"; do
    candidate_rank=$(vendors_effort_rank "$candidate")
    if [ "$candidate_rank" -ge "$desired_rank" ] \
        && [ "$candidate_rank" -lt "$best_up_rank" ]; then
      best_up="$candidate"
      best_up_rank="$candidate_rank"
    fi
    if [ "$candidate_rank" -le "$desired_rank" ] \
        && [ "$candidate_rank" -gt "$best_down_rank" ]; then
      best_down="$candidate"
      best_down_rank="$candidate_rank"
    fi
  done

  if [ -n "$best_up" ]; then
    printf "%s\n" "$best_up"
    return
  fi
  if [ -n "$best_down" ]; then
    printf "%s\n" "$best_down"
    return
  fi

  printf "vendor %s has no effort mapping for %s\n" "$vendor" "$effort" >&2
  return 2
}

vendors_native_arg_present() {
  local needle="$1"
  local arg=""

  for arg in "${VENDORS_NATIVE_ARGS[@]}"; do
    if [ "$arg" = "$needle" ] || [[ "$arg" == "$needle="* ]]; then
      return 0
    fi
  done
  return 1
}

vendors_print_command() {
  local prompt_file="$1"
  local output_file="$2"
  shift 2
  local -a command=("$@")

  printf "vendor=%s\n" "$VENDORS_VENDOR_ID"
  printf "cli=%s\n" "$VENDORS_VENDOR_CLI"
  printf "model=%s\n" "${VENDORS_RESOLVED_MODEL:-<default>}"
  printf "effort=%s\n" "${VENDORS_RESOLVED_EFFORT:-<default>}"
  printf "yolo=%s\n" "${VENDORS_YOLO:-0}"
  printf "prompt_file=%s\n" "$prompt_file"
  printf "output_file=%s\n" "$output_file"
  printf "command="
  printf " %q" "${command[@]}"
  printf "\n"
}

vendors_python() {
  command -v python3 || command -v python || true
}

vendors_collect_usage() {
  local vendor="$1"
  local output_file="$2"
  local transcript_file="$3"
  local usage_file="$4"
  local py

  py=$(vendors_python)
  if [ -z "$py" ]; then
    if [ "$vendor" = "grok" ]; then
      mkdir -p "$(dirname "$usage_file")"
      printf '%s\n' \
        '{"available":false,"provider":"grok","total_tokens":null,"reason":"python3 or python is required for Grok streaming JSON normalization"}' \
        > "$usage_file"
    fi
    return 0
  fi

  "$py" - "$vendor" "$output_file" "$transcript_file" "$usage_file" \
    "${VENDORS_RESOLVED_MODEL:-}" <<'PY' || true
import json
import os
import re
import sys
from pathlib import Path

vendor, output_path, transcript_path, usage_path, model = sys.argv[1:6]
output = Path(output_path)
transcript = Path(transcript_path)
usage_file = Path(usage_path)
schema_file = os.environ.get("VENDORS_SCHEMA_FILE", "")


def parse_int(value):
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^0-9]", "", value)
        return int(digits) if digits else 0
    return 0


def usage_total(raw):
    if not isinstance(raw, dict):
        return None
    for key in ("total_tokens", "totalTokens", "totalTokenCount", "total"):
        value = parse_int(raw.get(key))
        if value > 0:
            return value
    parts = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    )
    total = sum(parse_int(raw.get(key)) for key in parts)
    return total or None


def write_usage(payload):
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    usage_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


payload = {
    "provider": vendor,
    "model": model or None,
    "total_tokens": None,
    "available": False,
}

try:
    raw_out = output.read_text(errors="replace") if output.exists() else ""
except OSError:
    raw_out = ""

if vendor == "claude":
    try:
        data = json.loads(raw_out)
    except Exception:
        data = None
    if not isinstance(data, dict):
        last_json_event = None
        last_result_event = None
        for line in raw_out.splitlines():
            candidate = line.strip()
            if not (candidate.startswith("{") and candidate.endswith("}")):
                continue
            try:
                event = json.loads(candidate)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            last_json_event = event
            if event.get("type") == "result":
                last_result_event = event
        data = last_result_event or last_json_event
    if isinstance(data, dict):
        # When Claude is invoked with `--json-schema`, the schema-conforming
        # JSON lands under `structured_output` and `result` is empty. When
        # invoked without a schema, `result` carries the LLM's text output.
        # Preserve a stable contract for downstream consumers: if a
        # structured payload exists, write a minimal envelope they can parse;
        # otherwise unwrap the text `result` as before.
        if data.get("structured_output") is not None:
            output.write_text(
                json.dumps({"structured_output": data["structured_output"]})
            )
        elif "result" in data:
            result = data.get("result", "")
            structured = None
            if schema_file and isinstance(result, str) and result.strip():
                try:
                    structured = json.loads(result)
                except Exception:
                    structured = None
            if structured is not None:
                output.write_text(json.dumps({"structured_output": structured}))
            else:
                output.write_text(str(result))
        raw_usage = data.get("usage")
        total = usage_total(raw_usage)
        if isinstance(raw_usage, dict) and total is not None:
            payload.update({
                "available": True,
                "source": "claude_json",
                "total_tokens": total,
                "input_tokens": parse_int(raw_usage.get("input_tokens")),
                "cache_creation_input_tokens": parse_int(raw_usage.get("cache_creation_input_tokens")),
                "cache_read_input_tokens": parse_int(raw_usage.get("cache_read_input_tokens")),
                "output_tokens": parse_int(raw_usage.get("output_tokens")),
                "raw": raw_usage,
            })
elif vendor == "grok":
    # Grok Build --output-format streaming-json emits NDJSON `text`,
    # `thought`, and terminal `end` events. Keep that protocol transcript in
    # `stream`, while exposing only concatenated final text (or the shared
    # structured-output envelope) through `out`.
    try:
        lines = transcript.read_text(errors="replace").splitlines() if transcript.exists() else []
    except OSError:
        lines = []
    text_parts = []
    end_event = None
    for line in lines:
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "text" and isinstance(event.get("data"), str):
            text_parts.append(event["data"])
        elif event.get("type") == "end":
            end_event = event
    if schema_file and isinstance(end_event, dict) and "structuredOutput" in end_event:
        output.write_text(json.dumps({
            "structured_output": end_event.get("structuredOutput")
        }))
    elif text_parts:
        output.write_text("".join(text_parts))
    if isinstance(end_event, dict):
        raw_usage = end_event.get("usage")
        if isinstance(raw_usage, dict):
            input_tokens = parse_int(raw_usage.get("input_tokens"))
            cache_read_tokens = parse_int(raw_usage.get("cache_read_input_tokens"))
            output_tokens = parse_int(raw_usage.get("output_tokens"))
            total = parse_int(raw_usage.get("total_tokens"))
            if total <= 0:
                total = input_tokens + cache_read_tokens + output_tokens
            payload.update({
                "available": total > 0,
                "source": "grok_jsonl",
                "total_tokens": total or None,
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": parse_int(raw_usage.get("reasoning_tokens")),
                "raw": raw_usage,
            })
elif vendor == "cursor":
    # cursor-agent --output-format stream-json emits one JSON event per line.
    # The terminal "result" event carries both the assistant text (`result`)
    # and a `usage` object with cursor's camelCase token fields:
    #   {"type":"result", ..., "result":"...", "usage":{
    #      "inputTokens":3,"outputTokens":5,
    #      "cacheReadTokens":15157,"cacheWriteTokens":5081}}
    # `--output-format json` prints the same envelope as a single line.
    last_result_event = None
    last_assistant_text = None
    for line in raw_out.splitlines():
        candidate = line.strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            event = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            last_result_event = event
            continue
        if event.get("type") == "assistant":
            message = event.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text")
                            if isinstance(text, str):
                                parts.append(text)
                    if parts:
                        last_assistant_text = "".join(parts)
    if isinstance(last_result_event, dict) and "result" in last_result_event:
        output.write_text(str(last_result_event.get("result", "")))
    elif last_assistant_text is not None:
        output.write_text(last_assistant_text)
    if isinstance(last_result_event, dict):
        raw_usage = last_result_event.get("usage")
        if isinstance(raw_usage, dict):
            input_tokens = parse_int(raw_usage.get("inputTokens"))
            output_tokens = parse_int(raw_usage.get("outputTokens"))
            cache_read_tokens = parse_int(raw_usage.get("cacheReadTokens"))
            cache_write_tokens = parse_int(raw_usage.get("cacheWriteTokens"))
            total = input_tokens + output_tokens
            payload.update({
                "available": total > 0,
                "source": "cursor_agent_jsonl",
                "total_tokens": total or None,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "cache_creation_input_tokens": cache_write_tokens,
                "raw": raw_usage,
            })
elif vendor == "openai":
    # When a schema was passed, codex writes the schema-conforming JSON to
    # output_file via --output-last-message. Wrap it in the same
    # {"structured_output": ...} envelope claude produces so callers see
    # one shape across vendors.
    if schema_file and raw_out.strip():
        try:
            obj = json.loads(raw_out)
        except Exception:
            obj = None
        if obj is None:
            for line in reversed(raw_out.splitlines()):
                candidate = line.strip()
                if not (candidate.startswith("{") and candidate.endswith("}")):
                    continue
                try:
                    obj = json.loads(candidate)
                    break
                except Exception:
                    continue
        if obj is not None:
            output.write_text(json.dumps({"structured_output": obj}))
    try:
        lines = transcript.read_text(errors="replace").splitlines() if transcript.exists() else []
    except OSError:
        lines = []
    last_usage = None
    for line in lines:
        try:
            event = json.loads(line)
        except Exception:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                last_usage = usage
    if last_usage:
        input_tokens = parse_int(last_usage.get("input_tokens"))
        output_tokens = parse_int(last_usage.get("output_tokens"))
        total = input_tokens + output_tokens
        payload.update({
            "available": total > 0,
            "source": "codex_jsonl",
            "total_tokens": total or None,
            "input_tokens": input_tokens,
            "cached_input_tokens": parse_int(last_usage.get("cached_input_tokens")),
            "output_tokens": output_tokens,
            "reasoning_output_tokens": parse_int(last_usage.get("reasoning_output_tokens")),
            "raw": last_usage,
        })
    else:
        combined = raw_out + "\n" + "\n".join(lines)
        match = re.search(r"(?:tokens used|total tokens|total_tokens)\D+([0-9][0-9,]*)", combined, re.I)
        if match:
            payload.update({
                "available": True,
                "source": "codex_text_fallback",
                "total_tokens": parse_int(match.group(1)),
                "raw": {"matched": match.group(0)},
            })

write_usage(payload)
PY
}

vendors_output_error_reason() {
  local vendor="$1"
  local output_file="$2"
  local transcript_file="${VENDORS_TRANSCRIPT_FILE:-$output_file}"
  local py

  [ "$vendor" = "grok" ] || return 1
  py=$(vendors_python)
  [ -n "$py" ] || return 1

  "$py" - "$transcript_file" "${VENDORS_SCHEMA_FILE:-}" <<'PY'
import json
import sys
from pathlib import Path

transcript_path, schema_file = sys.argv[1:3]
try:
    lines = Path(transcript_path).read_text(errors="replace").splitlines()
except OSError:
    lines = []
events = []
for line in lines:
    try:
        event = json.loads(line)
    except Exception:
        continue
    if isinstance(event, dict):
        events.append(event)
for event in events:
    if event.get("type") == "error":
        print("Grok error: " + str(event.get("message") or "unspecified error"))
        raise SystemExit(0)
end = next((event for event in reversed(events) if event.get("type") == "end"), None)
if end is None:
    print("Grok streaming JSON ended without an end event")
    raise SystemExit(0)
if schema_file:
    error = end.get("structuredOutputError")
    if error:
        print("Grok structured output failed: " + str(error))
        raise SystemExit(0)
    if "structuredOutput" not in end:
        print("Grok structured output failed: end event omitted structuredOutput")
        raise SystemExit(0)
elif not any(
    event.get("type") == "text"
    and isinstance(event.get("data"), str)
    and event.get("data")
    for event in events
):
    print("Grok streaming JSON ended without response text")
    raise SystemExit(0)
raise SystemExit(1)
PY
}

vendors_run_with_redirect() {
  local prompt_file="$1"
  local output_file="$2"
  shift 2
  local -a command=("$@")
  local -a env_command=()

  if [ "${#VENDORS_ENV[@]}" -gt 0 ]; then
    env_command=(env "${VENDORS_ENV[@]}" "${command[@]}")
  else
    env_command=("${command[@]}")
  fi

  if [ "${VENDORS_DRY_RUN:-0}" = "1" ]; then
    vendors_print_command "$prompt_file" "$output_file" "${env_command[@]}"
    return 0
  fi

  if [ -n "${VENDORS_CWD:-}" ]; then
    ( cd "$VENDORS_CWD" && "${env_command[@]}" ) > "$output_file" 2>&1
  else
    "${env_command[@]}" > "$output_file" 2>&1
  fi
}

vendors_write_stdin_runner() {
  local runner_file="$1"
  local prompt_file="$2"
  shift 2
  local -a command=("$@")

  {
    printf '#!/usr/bin/env bash\n'
    printf 'set -euo pipefail\n'
    if [ -n "${VENDORS_CWD:-}" ]; then
      printf 'cd %q\n' "$VENDORS_CWD"
    fi
    printf 'exec'
    printf ' %q' "${command[@]}"
    printf ' < %q\n' "$prompt_file"
  } > "$runner_file"
  chmod +x "$runner_file"
}

vendors_run_stdin_with_pty() {
  local prompt_file="$1"
  local output_file="$2"
  shift 2
  local -a command=("$@")
  local runner_file
  local status=0

  runner_file="$(dirname "$output_file")/$(basename "$output_file").runner.sh"
  vendors_write_stdin_runner "$runner_file" "$prompt_file" "${command[@]}"
  script -q /dev/null "$runner_file" > "$output_file" 2>&1 || status=$?
  if [ "$status" -ne 0 ] \
      && grep -Eqi 'illegal option|invalid option|unrecognized option|usage: script|unexpected number of arguments' "$output_file"; then
    status=0
    script -qefE never -c "$runner_file" /dev/null > "$output_file" 2>&1 \
      || status=$?
  fi
  rm -f "$runner_file"
  if command -v perl >/dev/null 2>&1; then
    perl -0pi -e 's/\r//g; s/\^D//g; s/\x04\x08\x08//g; s/\x04//g; s/\x08//g; s/\e\[[0-?]*[ -\/]*[@-~]//g; s/\e\][^\a]*(?:\a|\e\\)//g; s/\e[78]//g' "$output_file"
  else
    tr -d '\r\004\010' < "$output_file" > "$output_file.clean"
    mv "$output_file.clean" "$output_file"
  fi
  return "$status"
}

vendors_run_codex() {
  local prompt_file="$1"
  local output_file="$2"
  local transcript_file="${VENDORS_TRANSCRIPT_FILE:-$output_file.codex-stdout}"
  local -a command=(codex exec --skip-git-repo-check --json --output-last-message "$output_file")
  local -a env_command=()

  if [ -n "${VENDORS_RESOLVED_MODEL:-}" ]; then
    command+=(--model "$VENDORS_RESOLVED_MODEL")
  fi
  if [ -n "${VENDORS_RESOLVED_EFFORT:-}" ]; then
    command+=(-c "model_reasoning_effort=\"$VENDORS_RESOLVED_EFFORT\"")
  fi
  if [ -n "${VENDORS_CWD:-}" ]; then
    command+=(--cd "$VENDORS_CWD")
  fi
  if [ "${VENDORS_YOLO:-0}" = "1" ]; then
    command+=(--dangerously-bypass-approvals-and-sandbox)
  fi
  if [ -n "${VENDORS_SCHEMA_FILE:-}" ] \
      && ! vendors_native_arg_present "--output-schema"; then
    command+=(--output-schema "$VENDORS_SCHEMA_FILE")
  fi

  command+=("${VENDORS_NATIVE_ARGS[@]}" -)

  if [ "${#VENDORS_ENV[@]}" -gt 0 ]; then
    env_command=(env "${VENDORS_ENV[@]}" "${command[@]}")
  else
    env_command=("${command[@]}")
  fi

  if [ "${VENDORS_DRY_RUN:-0}" = "1" ]; then
    vendors_print_command "$prompt_file" "$output_file" "${env_command[@]}"
    printf "stdin=%s\n" "$prompt_file"
    printf "transcript_file=%s\n" "$transcript_file"
    return 0
  fi

  if [ -n "${VENDORS_CWD:-}" ]; then
    ( cd "$VENDORS_CWD" && "${env_command[@]}" < "$prompt_file" ) > "$transcript_file" 2>&1
  else
    "${env_command[@]}" < "$prompt_file" > "$transcript_file" 2>&1
  fi

  if [ ! -s "$output_file" ] && [ -s "$transcript_file" ]; then
    {
      printf '[FALLBACK: codex did not write an output-last-message file; this is the CLI transcript.]\n\n'
      cat "$transcript_file"
    } > "$output_file"
  fi
}

vendors_run_claude() {
  local prompt_file="$1"
  local output_file="$2"
  local -a command=(claude -p --no-session-persistence)
  local -a env_command=()

  if [ -n "${VENDORS_RESOLVED_MODEL:-}" ]; then
    command+=(--model "$VENDORS_RESOLVED_MODEL")
  fi
  if [ -n "${VENDORS_RESOLVED_EFFORT:-}" ]; then
    command+=(--effort "$VENDORS_RESOLVED_EFFORT")
  fi
  if [ -n "${VENDORS_SYSTEM_PROMPT:-}" ]; then
    command+=(--system-prompt "$VENDORS_SYSTEM_PROMPT")
  fi
  if ! vendors_native_arg_present "--output-format"; then
    command+=(--output-format stream-json --include-partial-messages --verbose)
  fi
  if [ -n "${VENDORS_SCHEMA_FILE:-}" ] \
      && ! vendors_native_arg_present "--json-schema"; then
    command+=(--json-schema "$(cat "$VENDORS_SCHEMA_FILE")")
  fi
  if [ "${VENDORS_YOLO:-0}" = "1" ]; then
    command+=(--permission-mode bypassPermissions)
  fi

  command+=("${VENDORS_NATIVE_ARGS[@]}")

  if [ "${#VENDORS_ENV[@]}" -gt 0 ]; then
    env_command=(env "${VENDORS_ENV[@]}" "${command[@]}")
  else
    env_command=("${command[@]}")
  fi

  if [ "${VENDORS_DRY_RUN:-0}" = "1" ]; then
    vendors_print_command "$prompt_file" "$output_file" "${env_command[@]}"
    printf "stdin=%s\n" "$prompt_file"
    return 0
  fi

  if command -v script >/dev/null 2>&1; then
    vendors_run_stdin_with_pty "$prompt_file" "$output_file" "${env_command[@]}"
  elif [ -n "${VENDORS_CWD:-}" ]; then
    ( cd "$VENDORS_CWD" && "${env_command[@]}" < "$prompt_file" ) > "$output_file" 2>&1
  else
    "${env_command[@]}" < "$prompt_file" > "$output_file" 2>&1
  fi
}

vendors_run_agy() {
  local prompt_file="$1"
  local output_file="$2"
  local prompt
  local -a command=(agy)

  prompt=$(cat "$prompt_file")

  if [ -n "${VENDORS_RESOLVED_MODEL:-}" ]; then
    command+=(--model "$VENDORS_RESOLVED_MODEL")
  fi
  if [ "${VENDORS_YOLO:-0}" = "1" ]; then
    command+=(--dangerously-skip-permissions)
  fi

  command+=("${VENDORS_NATIVE_ARGS[@]}" --print "$prompt")

  if [ "${VENDORS_DRY_RUN:-0}" = "1" ]; then
    vendors_run_with_redirect "$prompt_file" "$output_file" "${command[@]}"
    return 0
  fi

  if [ "${#VENDORS_ENV[@]}" -gt 0 ]; then
    if [ -n "${VENDORS_CWD:-}" ]; then
      ( cd "$VENDORS_CWD" && env "${VENDORS_ENV[@]}" "${command[@]}" < /dev/null ) > "$output_file" 2>&1
    else
      env "${VENDORS_ENV[@]}" "${command[@]}" < /dev/null > "$output_file" 2>&1
    fi
  else
    if [ -n "${VENDORS_CWD:-}" ]; then
      ( cd "$VENDORS_CWD" && "${command[@]}" < /dev/null ) > "$output_file" 2>&1
    else
      "${command[@]}" < /dev/null > "$output_file" 2>&1
    fi
  fi
}

vendors_run_cursor() {
  local prompt_file="$1"
  local output_file="$2"
  local prompt
  # cursor-agent gates every fresh workspace on a "Workspace Trust Required"
  # prompt that breaks headless runs. `--trust` is the headless-mode opt-in
  # that the IDE's "I trust this folder" click maps to; it does not grant any
  # tool permission beyond what the caller already implicitly grants by
  # invoking the wrapper from their cwd. The other vendors have no
  # workspace-trust concept in their headless paths, so always passing this
  # keeps cursor's baseline aligned with them.
  local -a command=(cursor-agent -p --trust)

  prompt=$(cat "$prompt_file")

  if ! vendors_native_arg_present "--output-format"; then
    command+=(--output-format stream-json)
  fi
  if [ -n "${VENDORS_RESOLVED_MODEL:-}" ]; then
    command+=(--model "$VENDORS_RESOLVED_MODEL")
  fi
  if [ "${VENDORS_YOLO:-0}" = "1" ]; then
    command+=(--yolo)
  fi

  command+=("${VENDORS_NATIVE_ARGS[@]}" -- "$prompt")

  if [ "${VENDORS_DRY_RUN:-0}" = "1" ]; then
    vendors_run_with_redirect "$prompt_file" "$output_file" "${command[@]}"
    return 0
  fi

  if [ "${#VENDORS_ENV[@]}" -gt 0 ]; then
    if [ -n "${VENDORS_CWD:-}" ]; then
      ( cd "$VENDORS_CWD" && env "${VENDORS_ENV[@]}" "${command[@]}" < /dev/null ) > "$output_file" 2>&1
    else
      env "${VENDORS_ENV[@]}" "${command[@]}" < /dev/null > "$output_file" 2>&1
    fi
  else
    if [ -n "${VENDORS_CWD:-}" ]; then
      ( cd "$VENDORS_CWD" && "${command[@]}" < /dev/null ) > "$output_file" 2>&1
    else
      "${command[@]}" < /dev/null > "$output_file" 2>&1
    fi
  fi
}

vendors_run_grok() {
  local prompt_file="$1"
  local output_file="$2"
  local transcript_file="${VENDORS_TRANSCRIPT_FILE:-$output_file.grok-jsonl}"
  local py
  local -a command=(grok --output-format streaming-json)

  if [ -n "${VENDORS_RESOLVED_MODEL:-}" ]; then
    command+=(--model "$VENDORS_RESOLVED_MODEL")
  fi
  if [ -n "${VENDORS_RESOLVED_EFFORT:-}" ]; then
    command+=(--reasoning-effort "$VENDORS_RESOLVED_EFFORT")
  fi
  if [ -n "${VENDORS_CWD:-}" ]; then
    command+=(--cwd "$VENDORS_CWD")
  fi
  if [ "${VENDORS_YOLO:-0}" = "1" ]; then
    command+=(--yolo)
  fi
  if [ -n "${VENDORS_SCHEMA_FILE:-}" ] \
      && ! vendors_native_arg_present "--json-schema"; then
    command+=(--json-schema "$(cat "$VENDORS_SCHEMA_FILE")")
  fi

  command+=("${VENDORS_NATIVE_ARGS[@]}" --prompt-file "$prompt_file")

  if [ "${VENDORS_DRY_RUN:-0}" = "1" ]; then
    vendors_run_with_redirect "$prompt_file" "$output_file" "${command[@]}"
    printf "transcript_file=%s\n" "$transcript_file"
    return 0
  fi

  py=$(vendors_python)
  if [ -z "$py" ]; then
    printf "Grok vendor requires python3 or python for streaming JSON normalization; refusing native invocation\n" >&2
    return 69
  fi

  if [ "${#VENDORS_ENV[@]}" -gt 0 ]; then
    if [ -n "${VENDORS_CWD:-}" ]; then
      ( cd "$VENDORS_CWD" && env "${VENDORS_ENV[@]}" "${command[@]}" < /dev/null ) > "$transcript_file" 2>&1
    else
      env "${VENDORS_ENV[@]}" "${command[@]}" < /dev/null > "$transcript_file" 2>&1
    fi
  else
    if [ -n "${VENDORS_CWD:-}" ]; then
      ( cd "$VENDORS_CWD" && "${command[@]}" < /dev/null ) > "$transcript_file" 2>&1
    else
      "${command[@]}" < /dev/null > "$transcript_file" 2>&1
    fi
  fi
}

vendors_run() {
  local vendor="$1"
  local prompt_file="$2"
  local output_file="$3"

  case "$vendor" in
    openai) vendors_run_codex "$prompt_file" "$output_file" ;;
    claude) vendors_run_claude "$prompt_file" "$output_file" ;;
    agy) vendors_run_agy "$prompt_file" "$output_file" ;;
    cursor) vendors_run_cursor "$prompt_file" "$output_file" ;;
    grok) vendors_run_grok "$prompt_file" "$output_file" ;;
    *)
      printf "unknown normalized vendor: %s\n" "$vendor" >&2
      return 2
      ;;
  esac
}
