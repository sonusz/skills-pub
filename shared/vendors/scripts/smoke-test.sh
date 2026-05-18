#!/usr/bin/env bash
# Smoke test for the shared vendors module without real model calls.
#
# Exercises the cross-vendor fan-out contract: four separate callers each make
# one call to all four configured vendors, for four total calls and sixteen
# vendor outputs.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORK=$(mktemp -d /tmp/vendors-smoke.XXXXXX)
cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT

BIN_DIR="$WORK/bin"
RUN_ROOT="$WORK/runs"
mkdir -p "$BIN_DIR" "$RUN_ROOT"

cat > "$BIN_DIR/codex" <<'FAKE_CODEX'
#!/usr/bin/env bash
out=""
json=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-last-message)
      out="$2"
      shift 2
      ;;
    --json)
      json=1
      shift
      ;;
    *)
      shift
      ;;
  esac
done
prompt=$(cat)
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  response="READY"
else
  response="openai received: $prompt"
fi
[ -n "$out" ] && printf "%s\n" "$response" > "$out"
if [ "$json" = "1" ]; then
  printf '{"type":"turn.completed","usage":{"input_tokens":101,"cached_input_tokens":11,"output_tokens":7,"reasoning_output_tokens":3}}\n'
else
  printf "fake codex transcript\n"
fi
FAKE_CODEX

cat > "$BIN_DIR/claude" <<'FAKE_CLAUDE'
#!/usr/bin/env bash
format=""
verbose=0
include_partials=0
schema=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-format)
      format="${2-}"
      shift 2
      ;;
    --verbose)
      verbose=1
      shift
      ;;
    --include-partial-messages)
      include_partials=1
      shift
      ;;
    --json-schema)
      schema=1
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
prompt=$(cat)
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  response="READY"
else
  response="claude received: $prompt"
fi
if [ "$schema" = "1" ]; then
  response='{"answer":"READY"}'
fi
if [ "$format" = "stream-json" ]; then
  if [ "$verbose" != "1" ]; then
    printf "stream-json requires --verbose\n" >&2
    exit 2
  fi
  python3 - "$response" "$include_partials" <<'PY'
import json
import sys

response = sys.argv[1]
include_partials = sys.argv[2] == "1"
print(json.dumps({"type": "system", "subtype": "init"}))
if include_partials:
    print(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": response[:7]}]},
    }))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "result": response,
    "usage": {
        "input_tokens": 103,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 13,
        "output_tokens": 9,
    },
}))
PY
elif [ "$format" = "json" ]; then
  python3 - "$response" <<'PY'
import json
import sys
print(json.dumps({
    "result": sys.argv[1],
    "usage": {
        "input_tokens": 103,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 13,
        "output_tokens": 9,
    },
}))
PY
else
  printf "%s\n" "$response"
fi
FAKE_CLAUDE

cat > "$BIN_DIR/gemini" <<'FAKE_GEMINI'
#!/usr/bin/env bash
prompt=""
format=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt|-p)
      prompt="$2"
      shift 2
      ;;
    --output-format)
      format="${2-}"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  response="READY"
elif printf "%s" "$prompt" | grep -qi 'semantic gemini error'; then
  cat <<'OUT'
Attempt 1 failed with status 429. Retrying with backoff... _GaxiosError: [{
  "error": {
    "code": 429,
    "message": "No capacity available for model fake-gemini on the server",
    "status": "RESOURCE_EXHAUSTED"
  }
}]
OUT
  exit 0
else
  response="gemini received: $prompt"
fi
if [ "$format" = "stream-json" ]; then
  python3 - "$response" <<'PY'
import json
import sys

response = sys.argv[1]
print(json.dumps({"type": "init", "model": "fake-gemini"}))
print(json.dumps({
    "type": "message",
    "role": "assistant",
    "content": response,
    "delta": True,
}))
print(json.dumps({
    "type": "result",
    "status": "success",
    "stats": {
        "total_tokens": 127,
        "models": {
            "fake-gemini": {
                "total_tokens": 127,
                "input_tokens": 111,
                "output_tokens": 16,
            }
        },
    },
}))
PY
elif [ "$format" = "json" ]; then
  python3 - "$response" <<'PY'
import json
import sys
print(json.dumps({
    "response": sys.argv[1],
    "stats": {
        "models": {
            "fake-gemini": {
                "tokens": {"total": 127}
            }
        }
    },
}))
PY
else
  printf "%s\n" "$response"
fi
FAKE_GEMINI

cat > "$BIN_DIR/cursor-agent" <<'FAKE_CURSOR'
#!/usr/bin/env bash
print=0
format=""
prompt=""
seen_dashdash=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p|--print)
      print=1
      shift
      ;;
    --output-format)
      format="${2-}"
      shift 2
      ;;
    --output-format=*)
      format="${1#*=}"
      shift
      ;;
    --model|--api-key|-H|--header|--mode|--resume|--sandbox|--workspace|-w|--worktree|--worktree-base)
      shift 2
      ;;
    --yolo|-f|--force|--continue|--plan|--list-models|--approve-mcps|--trust|--skip-worktree-setup|--stream-partial-output|-v|--version|-h|--help)
      shift
      ;;
    --)
      seen_dashdash=1
      shift
      prompt="$*"
      break
      ;;
    *)
      if [ "$seen_dashdash" = "0" ]; then
        prompt="$1"
      fi
      shift
      ;;
  esac
done
if [ -z "$prompt" ] && [ ! -t 0 ]; then
  prompt=$(cat)
fi
if [ "$print" != "1" ]; then
  printf "fake cursor-agent requires --print in headless smoke mode\n" >&2
  exit 2
fi
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  response="READY"
else
  response="cursor received: $prompt"
fi
if [ "$format" = "stream-json" ]; then
  python3 - "$response" <<'PY'
import json
import sys

response = sys.argv[1]
print(json.dumps({"type": "system", "subtype": "init", "model": "fake-cursor"}))
print(json.dumps({
    "type": "user",
    "message": {"role": "user", "content": [{"type": "text", "text": "fake-prompt"}]},
}))
print(json.dumps({
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": response}]},
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": response,
    "usage": {
        "inputTokens": 117,
        "outputTokens": 23,
        "cacheReadTokens": 5081,
        "cacheWriteTokens": 1024,
    },
}))
PY
elif [ "$format" = "json" ]; then
  python3 - "$response" <<'PY'
import json
import sys
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": sys.argv[1],
    "usage": {
        "inputTokens": 117,
        "outputTokens": 23,
        "cacheReadTokens": 5081,
        "cacheWriteTokens": 1024,
    },
}))
PY
else
  printf "%s\n" "$response"
fi
FAKE_CURSOR

chmod +x "$BIN_DIR/codex" "$BIN_DIR/claude" "$BIN_DIR/gemini" "$BIN_DIR/cursor-agent"

for caller in openai claude gemini cursor; do
  call_dir="$RUN_ROOT/$caller"
  mkdir -p "$call_dir"

  PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor OpenAI \
    --vendor Claude \
    --vendor Gemini \
    --vendor Cursor \
    --prompt "caller=$caller fan out to openai, claude, gemini, cursor" \
    --output-dir "$call_dir" \
    --min-success 4 >/dev/null

  for vendor in openai claude gemini cursor; do
    for suffix in out status usage.json; do
      file="$call_dir/$vendor/$suffix"
      if [ ! -s "$file" ]; then
        printf "FAIL: expected non-empty %s\n" "$file" >&2
        exit 1
      fi
    done
    if [ ! -e "$call_dir/$vendor/log" ]; then
      printf "FAIL: expected log file %s\n" "$call_dir/$vendor/log" >&2
      exit 1
    fi

    if ! grep -q '^exit_code=0$' "$call_dir/$vendor/status"; then
      printf "FAIL: expected successful status for %s in caller %s\n" "$vendor" "$caller" >&2
      cat "$call_dir/$vendor/status" >&2
      exit 1
    fi
    if ! grep -q '^usage=' "$call_dir/$vendor/status"; then
      printf "FAIL: expected usage path in status for %s in caller %s\n" "$vendor" "$caller" >&2
      cat "$call_dir/$vendor/status" >&2
      exit 1
    fi
    if ! grep -q '"available": true' "$call_dir/$vendor/usage.json" \
        || ! grep -q '"total_tokens":' "$call_dir/$vendor/usage.json"; then
      printf "FAIL: expected available token usage for %s in caller %s\n" "$vendor" "$caller" >&2
      cat "$call_dir/$vendor/usage.json" >&2
      exit 1
    fi

    if ! grep -q "caller=$caller" "$call_dir/$vendor/out"; then
      printf "FAIL: expected %s output to include caller marker %s\n" "$vendor" "$caller" >&2
      cat "$call_dir/$vendor/out" >&2
      exit 1
    fi
  done
done

dry_dir="$RUN_ROOT/dry-run"
mkdir -p "$dry_dir"
PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor OpenAI \
  --prompt "dry run openai yolo mapping" \
  --output-dir "$dry_dir" \
  --id openai-yolo \
  --yolo \
  --dry-run >/dev/null

if ! grep -q -- '--dangerously-bypass-approvals-and-sandbox' "$dry_dir/openai-yolo/log"; then
  printf "FAIL: expected OpenAI yolo dry-run to use Codex approval bypass\n" >&2
  cat "$dry_dir/openai-yolo/log" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Claude \
  --prompt "dry run claude yolo mapping" \
  --output-dir "$dry_dir" \
  --id claude-yolo \
  --yolo \
  --dry-run >/dev/null

if ! grep -q -- '--permission-mode bypassPermissions' "$dry_dir/claude-yolo/log"; then
  printf "FAIL: expected Claude yolo dry-run to use bypassPermissions\n" >&2
  cat "$dry_dir/claude-yolo/log" >&2
  exit 1
fi
if ! grep -q -- '--output-format stream-json' "$dry_dir/claude-yolo/log" \
    || ! grep -q -- '--include-partial-messages' "$dry_dir/claude-yolo/log" \
    || ! grep -q -- '--verbose' "$dry_dir/claude-yolo/log"; then
  printf "FAIL: expected Claude default dry-run to use stream-json with partials and verbose\n" >&2
  cat "$dry_dir/claude-yolo/log" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Claude \
  --prompt "dry run claude explicit json output" \
  --output-dir "$dry_dir" \
  --id claude-json-output \
  --native-arg --output-format \
  --native-arg json \
  --dry-run >/dev/null

if ! grep -q -- '--output-format json' "$dry_dir/claude-json-output/log" \
    || grep -q -- '--output-format stream-json' "$dry_dir/claude-json-output/log" \
    || grep -q -- '--include-partial-messages' "$dry_dir/claude-json-output/log"; then
  printf "FAIL: expected explicit Claude output-format json to suppress stream-json defaults\n" >&2
  cat "$dry_dir/claude-json-output/log" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Gemini \
  --prompt "dry run gemini yolo mapping" \
  --output-dir "$dry_dir" \
  --id gemini-yolo \
  --yolo \
  --dry-run >/dev/null

if ! grep -q -- '--yolo' "$dry_dir/gemini-yolo/log"; then
  printf "FAIL: expected Gemini yolo dry-run to use --yolo\n" >&2
  cat "$dry_dir/gemini-yolo/log" >&2
  exit 1
fi
if ! grep -q -- '--output-format stream-json' "$dry_dir/gemini-yolo/log"; then
  printf "FAIL: expected Gemini default dry-run to use stream-json\n" >&2
  cat "$dry_dir/gemini-yolo/log" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Gemini \
  --prompt "dry run gemini explicit json output" \
  --output-dir "$dry_dir" \
  --id gemini-json-output \
  --native-arg --output-format \
  --native-arg json \
  --dry-run >/dev/null

if ! grep -q -- '--output-format json' "$dry_dir/gemini-json-output/log" \
    || grep -q -- '--output-format stream-json' "$dry_dir/gemini-json-output/log"; then
  printf "FAIL: expected explicit Gemini output-format json to suppress stream-json defaults\n" >&2
  cat "$dry_dir/gemini-json-output/log" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Cursor \
  --prompt "dry run cursor yolo mapping" \
  --output-dir "$dry_dir" \
  --id cursor-yolo \
  --yolo \
  --dry-run >/dev/null

if ! grep -q -- '--yolo' "$dry_dir/cursor-yolo/log"; then
  printf "FAIL: expected Cursor yolo dry-run to use --yolo\n" >&2
  cat "$dry_dir/cursor-yolo/log" >&2
  exit 1
fi
if ! grep -q -- '--output-format stream-json' "$dry_dir/cursor-yolo/log" \
    || ! grep -q -- '-p ' "$dry_dir/cursor-yolo/log" \
    || ! grep -q -- '--trust' "$dry_dir/cursor-yolo/log"; then
  printf "FAIL: expected Cursor default dry-run to use -p --trust + stream-json headless mode\n" >&2
  cat "$dry_dir/cursor-yolo/log" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Cursor \
  --prompt "dry run cursor explicit json output" \
  --output-dir "$dry_dir" \
  --id cursor-json-output \
  --native-arg --output-format \
  --native-arg json \
  --dry-run >/dev/null

if ! grep -q -- '--output-format json' "$dry_dir/cursor-json-output/log" \
    || grep -q -- '--output-format stream-json' "$dry_dir/cursor-json-output/log"; then
  printf "FAIL: expected explicit Cursor output-format json to suppress stream-json defaults\n" >&2
  cat "$dry_dir/cursor-json-output/log" >&2
  exit 1
fi

expected_effort() {
  case "$1:$2" in
    OpenAI:min|OpenAI:low) printf "low\n" ;;
    OpenAI:medium) printf "medium\n" ;;
    OpenAI:high) printf "high\n" ;;
    OpenAI:xhigh|OpenAI:max) printf "xhigh\n" ;;
    Claude:min|Claude:low) printf "low\n" ;;
    Claude:medium) printf "medium\n" ;;
    Claude:high) printf "high\n" ;;
    Claude:xhigh) printf "xhigh\n" ;;
    Claude:max) printf "max\n" ;;
    Gemini:*) printf "<default>\n" ;;
    Cursor:*) printf "<default>\n" ;;
    *) printf "unknown\n" ;;
  esac
}

lower() {
  printf "%s" "$1" | tr '[:upper:]' '[:lower:]'
}

for effort_vendor in OpenAI Claude Gemini Cursor; do
  for effort in min low medium high xhigh max; do
    effort_id="effort-$(lower "$effort_vendor")-$effort"
    PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
      --vendor "$effort_vendor" \
      --prompt "dry run $effort_vendor effort $effort mapping" \
      --output-dir "$dry_dir" \
      --id "$effort_id" \
      --effort "$effort" \
      --dry-run >/dev/null

    expected=$(expected_effort "$effort_vendor" "$effort")
    if ! grep -q "^effort=$expected$" "$dry_dir/$effort_id/log"; then
      printf "FAIL: expected %s effort %s to resolve to %s\n" \
        "$effort_vendor" "$effort" "$expected" >&2
      cat "$dry_dir/$effort_id/log" >&2
      exit 1
    fi
  done
done

gemini_error_dir="$RUN_ROOT/gemini-semantic-error"
mkdir -p "$gemini_error_dir"
if PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor Gemini \
    --prompt "semantic gemini error" \
    --output-dir "$gemini_error_dir" \
    --id gemini-error \
    --min-success 1 >/dev/null 2>&1; then
  printf "FAIL: expected Gemini semantic error output to fail even with CLI exit 0\n" >&2
  cat "$gemini_error_dir/gemini-error/out" >&2
  exit 1
fi

if ! grep -q '^exit_code=1$' "$gemini_error_dir/gemini-error/status" \
    || ! grep -q '^reason=vendor_error$' "$gemini_error_dir/gemini-error/status"; then
  printf "FAIL: expected Gemini semantic error status to record vendor_error\n" >&2
  cat "$gemini_error_dir/gemini-error/status" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Claude \
  --prompt "dry run claude native arg passthrough" \
  --output-dir "$dry_dir" \
  --id claude-native \
  --native-arg --allowedTools \
  --native-arg Read \
  --dry-run >/dev/null

if ! grep -q -- '--allowedTools Read' "$dry_dir/claude-native/log"; then
  printf "FAIL: expected Claude native args to pass through unchanged\n" >&2
  cat "$dry_dir/claude-native/log" >&2
  exit 1
fi

context_file="$WORK/context-artifact.txt"
printf "CONTEXT_SENTINEL_%s\n" "$RANDOM" > "$context_file"
context_dir="$RUN_ROOT/context-file"
mkdir -p "$context_dir"
PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Claude \
  --prompt "review attached context" \
  --context-file "$context_file" \
  --output-dir "$context_dir" \
  --id claude-context \
  --min-success 1 >/dev/null

if ! grep -q 'BEGIN CONTEXT FILE:' "$context_dir/claude-context/out" \
    || ! grep -q 'CONTEXT_SENTINEL_' "$context_dir/claude-context/out"; then
  printf "FAIL: expected context-file contents to be inlined into prompt\n" >&2
  cat "$context_dir/claude-context/out" >&2
  exit 1
fi

schema_file="$WORK/response-schema.json"
cat > "$schema_file" <<'JSON'
{
  "type": "object",
  "properties": {
    "answer": {"type": "string"}
  },
  "required": ["answer"],
  "additionalProperties": false
}
JSON

schema_dir="$RUN_ROOT/claude-schema"
mkdir -p "$schema_dir"
PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Claude \
  --prompt "return schema output" \
  --schema-file "$schema_file" \
  --output-dir "$schema_dir" \
  --id claude-schema \
  --min-success 1 >/dev/null

if ! grep -q '"structured_output"' "$schema_dir/claude-schema/out" \
    || ! grep -q '"answer": "READY"' "$schema_dir/claude-schema/out"; then
  printf "FAIL: expected Claude stream-json schema output to preserve structured_output envelope\n" >&2
  cat "$schema_dir/claude-schema/out" >&2
  exit 1
fi

cursor_schema_dir="$RUN_ROOT/cursor-schema-rejected"
mkdir -p "$cursor_schema_dir"
cursor_schema_log="$WORK/cursor-schema-reject.log"
if PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor Cursor \
    --prompt "schema rejection probe" \
    --schema-file "$schema_file" \
    --output-dir "$cursor_schema_dir" \
    --id cursor-schema \
    --min-success 1 >"$cursor_schema_log" 2>&1; then
  printf "FAIL: expected --schema-file with Cursor to be rejected\n" >&2
  cat "$cursor_schema_log" >&2
  exit 1
fi
if ! grep -q -- '--schema-file is not supported on cursor' "$cursor_schema_log"; then
  printf "FAIL: expected Cursor schema rejection message\n" >&2
  cat "$cursor_schema_log" >&2
  exit 1
fi

dup_dir="$RUN_ROOT/duplicate-openai"
mkdir -p "$dup_dir"
PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor OpenAI \
  --vendor OpenAI \
  --prompt "duplicate openai fanout" \
  --output-dir "$dup_dir" \
  --min-success 2 >/dev/null

for id in openai openai-2; do
  if [ ! -s "$dup_dir/$id/out" ] || [ ! -s "$dup_dir/$id/status" ] || [ ! -e "$dup_dir/$id/log" ] || [ ! -s "$dup_dir/$id/usage.json" ]; then
    printf "FAIL: expected duplicate vendor output space %s\n" "$dup_dir/$id" >&2
    exit 1
  fi
  if ! grep -q 'duplicate openai fanout' "$dup_dir/$id/out"; then
    printf "FAIL: expected duplicate vendor output %s to contain prompt marker\n" "$dup_dir/$id/out" >&2
    exit 1
  fi
done

printf "OK: vendors smoke test passed (4 calls x 4 vendors + duplicate vendor + schema rejects)\n"
