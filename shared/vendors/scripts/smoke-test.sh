#!/usr/bin/env bash
# Smoke test for the shared vendors module without real model calls.
#
# Exercises the cross-vendor fan-out contract with fake CLIs, including the
# official Grok Build command surface and normalized artifacts.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Canonicalize (pwd -P): call.sh resolves --cwd the same way, and on macOS the
# mktemp path is the /tmp symlink while the resolved path is /private/tmp/...
WORK=$(cd "$(mktemp -d /tmp/vendors-smoke.XXXXXX)" && pwd -P)
cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT

BIN_DIR="$WORK/bin"
RUN_ROOT="$WORK/runs"
mkdir -p "$BIN_DIR" "$RUN_ROOT"

# Host OS detection self-check: vendors_host_os must match `uname -s`, and the
# PTY launcher must run this host's `script` form exactly once (no sniff-and-
# retry). A fake `script` outside BIN_DIR records argv and runs the runner, so
# the Claude calls below still exercise the real `script`.
pty_shim_dir="$WORK/pty-shim"
pty_capture="$WORK/pty-capture"
mkdir -p "$pty_shim_dir" "$pty_capture"
cat > "$pty_shim_dir/script" <<'FAKE_SCRIPT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_SCRIPT_CAPTURE/argv"
# BSD form: script -q /dev/null <runner>
if [ "$#" -eq 3 ] && [ "$1" = "-q" ] && [ "$2" = "/dev/null" ]; then
  exec "$3"
fi
# util-linux form: script -qefE never -c <runner> /dev/null
if [ "$#" -eq 5 ] && [ "$1" = "-qefE" ] && [ "$2" = "never" ] && [ "$3" = "-c" ]; then
  exec "$4"
fi
printf 'fake script: unexpected argv: %s\n' "$*" >&2
exit 1
FAKE_SCRIPT
chmod +x "$pty_shim_dir/script"

case "$(uname -s)" in
  Darwin) expected_host_os="darwin" ;;
  Linux) expected_host_os="linux" ;;
  *) expected_host_os="other" ;;
esac
pty_runner="$pty_capture/out.runner.sh"
case "$expected_host_os" in
  darwin) expected_pty_argv="-q /dev/null $pty_runner" ;;
  linux) expected_pty_argv="-qefE never -c $pty_runner /dev/null" ;;
  *) expected_pty_argv="" ;;
esac
printf 'PTY_PROMPT\n' > "$pty_capture/prompt"
(
  # shellcheck source=vendor-launch.sh
  . "$SCRIPT_DIR/vendor-launch.sh"
  host_os=$(vendors_host_os)
  if [ "$host_os" != "$expected_host_os" ]; then
    printf "FAIL: vendors_host_os returned %s, expected %s from uname -s\n" "$host_os" "$expected_host_os" >&2
    exit 1
  fi
  export FAKE_SCRIPT_CAPTURE="$pty_capture"
  export PATH="$pty_shim_dir:$PATH"
  vendors_run_stdin_with_pty "$pty_capture/prompt" "$pty_capture/out" cat \
    || { printf "FAIL: PTY launcher exited %s on %s\n" "$?" "$host_os" >&2; exit 1; }
)
if [ "$(cat "$pty_capture/out")" != "PTY_PROMPT" ]; then
  printf "FAIL: expected PTY launcher to deliver stdin prompt through script\n" >&2
  cat "$pty_capture/out" >&2
  exit 1
fi
if [ -n "$expected_pty_argv" ] && [ "$(cat "$pty_capture/argv")" != "$expected_pty_argv" ]; then
  printf "FAIL: expected exactly one %s-form script launch:\n  %s\ngot:\n" "$expected_host_os" "$expected_pty_argv" >&2
  sed -e 's/^/  /' "$pty_capture/argv" >&2
  exit 1
fi

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
  printf '{"type":"thread.started","thread_id":"11111111-1111-4111-8111-111111111111"}\n'
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
session_id="22222222-2222-4222-8222-222222222222"
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
    --session-id|--resume)
      session_id="$2"
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
elif printf "%s" "$prompt" | grep -q 'Idle-timeout probe'; then
  # Answer idle-probe.sh's evidence prompt like a cooperative arbiter.
  response="VERDICT: extend 120"
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
  python3 - "$response" "$include_partials" "$session_id" <<'PY'
import json
import sys

response = sys.argv[1]
include_partials = sys.argv[2] == "1"
session_id = sys.argv[3]
print(json.dumps({"type": "system", "subtype": "init", "session_id": session_id}))
if include_partials:
    print(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": response[:7]}]},
    }))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "result": response,
    "session_id": session_id,
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

cat > "$BIN_DIR/agy" <<'FAKE_AGY'
#!/usr/bin/env bash
prompt=""
format=""
conversation_id="33333333-3333-4333-8333-333333333333"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --print|-p)
      prompt="$2"
      shift 2
      ;;
    --output-format)
      format="$2"
      shift 2
      ;;
    --conversation)
      conversation_id="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  response="READY"
else
  response="agy received: $prompt"
fi
if [ "$format" = "json" ]; then
  python3 - "$response" "$conversation_id" <<'PY'
import json
import sys

print(json.dumps({
    "conversation_id": sys.argv[2],
    "status": "SUCCESS",
    "response": sys.argv[1],
    "usage": {
        "input_tokens": 107,
        "output_tokens": 19,
        "thinking_tokens": 3,
        "cache_read_tokens": 11,
        "total_tokens": 140,
    },
}))
PY
else
  printf "%s\n" "$response"
fi
FAKE_AGY

cat > "$BIN_DIR/cursor-agent" <<'FAKE_CURSOR'
#!/usr/bin/env bash
print=0
format=""
prompt=""
session_id="44444444-4444-4444-8444-444444444444"
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
    --resume)
      session_id="$2"
      shift 2
      ;;
    --model|--api-key|-H|--header|--mode|--sandbox|--workspace|-w|--worktree|--worktree-base)
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
  python3 - "$response" "$session_id" <<'PY'
import json
import sys

response = sys.argv[1]
session_id = sys.argv[2]
print(json.dumps({"type": "system", "subtype": "init", "model": "fake-cursor", "session_id": session_id}))
print(json.dumps({
    "type": "user",
    "message": {"role": "user", "content": [{"type": "text", "text": "fake-prompt"}]},
    "session_id": session_id,
}))
print(json.dumps({
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": response}]},
    "session_id": session_id,
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": response,
    "session_id": session_id,
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

cat > "$BIN_DIR/grok" <<'FAKE_GROK'
#!/usr/bin/env bash
format=""
schema=""
prompt_file=""
session_id="fake-grok-session"
fail=0
delay=0
malformed_schema=0
no_text=0
original_args=("$@")
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-format)
      format="${2-}"
      shift 2
      ;;
    --json-schema)
      schema="${2-}"
      shift 2
      ;;
    --prompt-file)
      prompt_file="${2-}"
      shift 2
      ;;
    --session-id|--resume)
      session_id="${2-}"
      shift 2
      ;;
    --model|--reasoning-effort|--cwd|--fake-native)
      shift 2
      ;;
    --fake-fail)
      fail=1
      shift
      ;;
    --fake-delay)
      delay="${2-}"
      shift 2
      ;;
    --fake-malformed-schema)
      malformed_schema=1
      shift
      ;;
    --fake-no-text)
      no_text=1
      shift
      ;;
    --yolo)
      shift
      ;;
    *)
      shift
      ;;
  esac
done
prompt=""
if [ -n "$prompt_file" ]; then
  prompt=$(cat "$prompt_file")
fi
if [ -n "${FAKE_GROK_CAPTURE_DIR:-}" ]; then
  mkdir -p "$FAKE_GROK_CAPTURE_DIR"
  printf "%s\n" "${original_args[@]}" > "$FAKE_GROK_CAPTURE_DIR/argv"
  pwd > "$FAKE_GROK_CAPTURE_DIR/cwd"
  printf "%s\n" "${GROK_SMOKE_MARKER:-}" > "$FAKE_GROK_CAPTURE_DIR/env"
  printf "%s" "$prompt" > "$FAKE_GROK_CAPTURE_DIR/prompt"
fi
if [ "$delay" -gt 0 ]; then
  sleep "$delay"
fi
if [ "$fail" = "1" ]; then
  printf '{"type":"error","message":"controlled fake Grok failure"}\n'
  exit 17
fi
if [ "${FAKE_GROK_FORCE_FAIL:-0}" = "1" ]; then
  printf '{"type":"error","message":"forced fake Grok doctor failure"}\n'
  exit 18
fi
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  response="READY"
else
  response="grok received: $prompt"
fi
if [ "$format" != "streaming-json" ]; then
  printf "fake grok requires --output-format streaming-json\n" >&2
  exit 2
fi
python3 - "$response" "$schema" "$malformed_schema" "$no_text" "$session_id" <<'PY'
import json
import sys

response, schema, malformed_schema, no_text, session_id = sys.argv[1:6]
print(json.dumps({"type": "thought", "data": "fake-control-frame"}))
if no_text != "1":
    mid = max(1, len(response) // 2)
    print(json.dumps({"type": "text", "data": response[:mid]}))
    print(json.dumps({"type": "text", "data": response[mid:]}))
end = {
    "type": "end",
    "stopReason": "EndTurn",
    "sessionId": session_id,
    "requestId": "fake-grok-request",
    "usage": {
        "input_tokens": 127,
        "cache_read_input_tokens": 17,
        "output_tokens": 29,
        "reasoning_tokens": 11,
        "total_tokens": 173,
    },
}
if schema:
    if malformed_schema == "1":
        end["structuredOutput"] = None
        end["structuredOutputError"] = "controlled schema mismatch"
    else:
        end["structuredOutput"] = {"answer": "READY"}
print(json.dumps(end))
PY
FAKE_GROK

chmod +x \
  "$BIN_DIR/codex" \
  "$BIN_DIR/claude" \
  "$BIN_DIR/agy" \
  "$BIN_DIR/cursor-agent" \
  "$BIN_DIR/grok"

# Provider-neutral session contract: every fake CLI establishes a native id,
# the second turn resumes it, and only the continuation prompt is delivered.
session_state_dir="$WORK/session-state"
for turn in 1 2; do
  session_run_dir="$RUN_ROOT/session-turn-$turn"
  PATH="$BIN_DIR:$PATH" \
  VENDORS_SESSION_STATE_DIR="$session_state_dir" \
  "$SCRIPT_DIR/call.sh" \
    --vendor OpenAI \
    --vendor Claude \
    --vendor Agy \
    --vendor Cursor \
    --vendor Grok \
    --session-key smoke-session-openai \
    --session-key smoke-session-claude \
    --session-key smoke-session-agy \
    --session-key smoke-session-cursor \
    --session-key smoke-session-grok \
    --prompt SESSION_INITIAL_PROMPT \
    --resume-prompt SESSION_DELTA_PROMPT \
    --output-dir "$session_run_dir" \
    --min-success 5 >/dev/null

  if [ "$turn" = "1" ]; then
    expected_mode="new"
    expected_prompt="SESSION_INITIAL_PROMPT"
  else
    expected_mode="resume"
    expected_prompt="SESSION_DELTA_PROMPT"
  fi
  for session_vendor in openai claude agy cursor grok; do
    if ! grep -q "^session_mode=$expected_mode$" \
        "$session_run_dir/$session_vendor/status" \
        || ! grep -Eq '^session_id=.+$' \
        "$session_run_dir/$session_vendor/status" \
        || ! grep -q "$expected_prompt" \
        "$session_run_dir/$session_vendor/out"; then
      printf "FAIL: expected %s session turn %s to be %s with %s\n" \
        "$session_vendor" "$turn" "$expected_mode" "$expected_prompt" >&2
      cat "$session_run_dir/$session_vendor/status" >&2
      cat "$session_run_dir/$session_vendor/out" >&2
      exit 1
    fi
  done
done

if grep -R -q 'smoke-session-' "$session_state_dir" \
    || ! grep -q '"source": "agy_json"' \
      "$RUN_ROOT/session-turn-2/agy/usage.json"; then
  printf "FAIL: expected hashed session state and normalized Agy JSON usage\n" >&2
  exit 1
fi

for caller in openai claude agy cursor grok; do
  call_dir="$RUN_ROOT/$caller"
  mkdir -p "$call_dir"

  PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor OpenAI \
    --vendor Claude \
    --vendor Agy \
    --vendor Cursor \
    --vendor Grok \
    --prompt "caller=$caller fan out to openai, claude, agy, cursor, grok" \
    --output-dir "$call_dir" \
    --min-success 5 >/dev/null

  for vendor in openai claude agy cursor grok; do
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
    if ! grep -q '^stream=' "$call_dir/$vendor/status"; then
      printf "FAIL: expected live stream path in status for %s in caller %s\n" "$vendor" "$caller" >&2
      cat "$call_dir/$vendor/status" >&2
      exit 1
    fi
    stream_path=$(awk -F= '$1 == "stream" { print $2; exit }' "$call_dir/$vendor/status")
    if [ ! -s "$stream_path" ]; then
      printf "FAIL: expected non-empty live stream %s for %s in caller %s\n" "$stream_path" "$vendor" "$caller" >&2
      cat "$call_dir/$vendor/status" >&2
      exit 1
    fi
    if [ "$vendor" = "agy" ]; then
      if ! grep -q '"available": false' "$call_dir/$vendor/usage.json" \
          || ! grep -q '"total_tokens": null' "$call_dir/$vendor/usage.json"; then
        printf "FAIL: expected unavailable token usage for %s in caller %s\n" "$vendor" "$caller" >&2
        cat "$call_dir/$vendor/usage.json" >&2
        exit 1
      fi
    elif ! grep -q '"available": true' "$call_dir/$vendor/usage.json" \
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
  --vendor Agy \
  --prompt "dry run agy yolo mapping" \
  --output-dir "$dry_dir" \
  --id agy-yolo \
  --yolo \
  --dry-run >/dev/null

if ! grep -q -- '--dangerously-skip-permissions' "$dry_dir/agy-yolo/log"; then
  printf "FAIL: expected Agy yolo dry-run to skip permission prompts\n" >&2
  cat "$dry_dir/agy-yolo/log" >&2
  exit 1
fi
if ! grep -q -- '--print' "$dry_dir/agy-yolo/log"; then
  printf "FAIL: expected Agy default dry-run to use print mode\n" >&2
  cat "$dry_dir/agy-yolo/log" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Agy \
  --prompt "dry run agy plan mode" \
  --output-dir "$dry_dir" \
  --id agy-plan-mode \
  --native-arg --mode \
  --native-arg plan \
  --dry-run >/dev/null

if ! grep -q -- '--mode plan' "$dry_dir/agy-plan-mode/log"; then
  printf "FAIL: expected Agy native mode args to pass through unchanged\n" >&2
  cat "$dry_dir/agy-plan-mode/log" >&2
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

for grok_label in Grok GROK xai XAI; do
  grok_alias_id="grok-alias-$(printf "%s" "$grok_label" | tr '[:upper:]' '[:lower:]')"
  PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor "$grok_label" \
    --prompt "dry run Grok alias $grok_label" \
    --output-dir "$dry_dir" \
    --id "$grok_alias_id" \
    --dry-run >/dev/null
  if ! grep -q '^vendor=grok$' "$dry_dir/$grok_alias_id/log" \
      || ! grep -q '^cli=grok$' "$dry_dir/$grok_alias_id/log"; then
    printf "FAIL: expected %s to normalize to vendor/cli grok\n" "$grok_label" >&2
    cat "$dry_dir/$grok_alias_id/log" >&2
    exit 1
  fi
done

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor Grok \
  --prompt "dry run grok defaults" \
  --output-dir "$dry_dir" \
  --id grok-defaults \
  --yolo \
  --native-arg --fake-native \
  --native-arg "value with spaces" \
  --dry-run >/dev/null

if ! grep -q '^model=grok-4.5$' "$dry_dir/grok-defaults/log" \
    || ! grep -q -- '--output-format streaming-json' "$dry_dir/grok-defaults/log" \
    || ! grep -q -- '--yolo' "$dry_dir/grok-defaults/log" \
    || ! grep -q -- '--fake-native value\\ with\\ spaces' "$dry_dir/grok-defaults/log" \
    || ! grep -q -- '--prompt-file' "$dry_dir/grok-defaults/log"; then
  printf "FAIL: expected Grok default model, streaming JSON, yolo, native args, and prompt-file transport\n" >&2
  cat "$dry_dir/grok-defaults/log" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
  --vendor xai \
  --model grok-explicit-test \
  --prompt "dry run explicit Grok model" \
  --output-dir "$dry_dir" \
  --id grok-explicit-model \
  --dry-run >/dev/null

if ! grep -q '^model=grok-explicit-test$' "$dry_dir/grok-explicit-model/log" \
    || ! grep -q -- '--model grok-explicit-test' "$dry_dir/grok-explicit-model/log"; then
  printf "FAIL: expected explicit Grok model to pass through\n" >&2
  cat "$dry_dir/grok-explicit-model/log" >&2
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
    Agy:*) printf "<default>\n" ;;
    Cursor:*) printf "<default>\n" ;;
    Grok:min|Grok:low) printf "low\n" ;;
    Grok:medium) printf "medium\n" ;;
    Grok:high|Grok:xhigh|Grok:max) printf "high\n" ;;
    *) printf "unknown\n" ;;
  esac
}

lower() {
  printf "%s" "$1" | tr '[:upper:]' '[:lower:]'
}

for effort_vendor in OpenAI Claude Agy Cursor Grok; do
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
    if [ "$effort_vendor" = "Grok" ] \
        && ! grep -q -- "--reasoning-effort $expected" "$dry_dir/$effort_id/log"; then
      printf "FAIL: expected Grok effort %s to use --reasoning-effort %s\n" \
        "$effort" "$expected" >&2
      cat "$dry_dir/$effort_id/log" >&2
      exit 1
    fi
  done
done

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

grok_prompt_file="$WORK/grok-prompt.txt"
grok_system_file="$WORK/grok-system.txt"
grok_instruction_file="$WORK/grok-instruction.txt"
grok_context_file="$WORK/grok-context.txt"
grok_cwd="$WORK/grok-cwd"
grok_capture="$WORK/grok-capture"
grok_transport_dir="$RUN_ROOT/grok-transport"
mkdir -p "$grok_cwd" "$grok_transport_dir"
printf "GROK_FILE_PROMPT αβ\\nwith whitespace  \\n" > "$grok_prompt_file"
printf "GROK_SYSTEM_MARKER\\n" > "$grok_system_file"
printf "GROK_INSTRUCTION_MARKER\\n" > "$grok_instruction_file"
printf "GROK_CONTEXT_MARKER\\n" > "$grok_context_file"

printf "GROK_STDIN_MARKER\\nsecond line\\n" | \
  PATH="$BIN_DIR:$PATH" \
  FAKE_GROK_CAPTURE_DIR="$grok_capture" \
  "$SCRIPT_DIR/call.sh" \
    --vendor xai \
    --prompt-file "$grok_prompt_file" \
    --system-file "$grok_system_file" \
    --instruction-file "$grok_instruction_file" \
    --context-file "$grok_context_file" \
    --cwd "$grok_cwd" \
    --env GROK_SMOKE_MARKER=GROK_ENV_VALUE \
    --native-arg --fake-native \
    --native-arg "native value with spaces" \
    --output-dir "$grok_transport_dir" \
    --id grok-transport \
    --min-success 1 >/dev/null

for marker in GROK_FILE_PROMPT GROK_SYSTEM_MARKER GROK_INSTRUCTION_MARKER GROK_CONTEXT_MARKER; do
  if ! grep -q "$marker" "$grok_capture/prompt"; then
    printf "FAIL: expected Grok prompt transport to include %s\n" "$marker" >&2
    cat "$grok_capture/prompt" >&2
    exit 1
  fi
done
if [ "$(cat "$grok_capture/cwd")" != "$grok_cwd" ] \
    || [ "$(cat "$grok_capture/env")" != "GROK_ENV_VALUE" ] \
    || ! grep -Fxq -- '--fake-native' "$grok_capture/argv" \
    || ! grep -Fxq -- 'native value with spaces' "$grok_capture/argv"; then
  printf "FAIL: expected Grok cwd, env, and ordered native args to reach fake CLI\n" >&2
  exit 1
fi
python3 - "$grok_capture/argv" <<'PY'
import sys
from pathlib import Path

args = Path(sys.argv[1]).read_text().splitlines()
assert args.index("--output-format") < args.index("--fake-native")
assert args.index("--fake-native") < args.index("--prompt-file")
assert args.count("--fake-native") == 1
PY
if ! grep -q 'grok received:' "$grok_transport_dir/grok-transport/out" \
    || grep -q 'fake-control-frame' "$grok_transport_dir/grok-transport/out" \
    || ! grep -q '"source": "grok_jsonl"' "$grok_transport_dir/grok-transport/usage.json" \
    || ! grep -q '"total_tokens": 173' "$grok_transport_dir/grok-transport/usage.json" \
    || ! grep -q '"type": "thought"' "$grok_transport_dir/grok-transport/stream"; then
  printf "FAIL: expected normalized Grok final text, usage, and raw stream artifacts\n" >&2
  exit 1
fi
if ! grep -q '^cli=grok$' "$grok_transport_dir/grok-transport/status" \
    || ! grep -q '^model=grok-4.5$' "$grok_transport_dir/grok-transport/status"; then
  printf "FAIL: expected Grok status to retain selected CLI and model\n" >&2
  cat "$grok_transport_dir/grok-transport/status" >&2
  exit 1
fi

grok_stdin_capture="$WORK/grok-stdin-capture"
grok_stdin_dir="$RUN_ROOT/grok-stdin"
printf "GROK_STDIN_MARKER\\nsecond line\\n" | \
  PATH="$BIN_DIR:$PATH" \
  FAKE_GROK_CAPTURE_DIR="$grok_stdin_capture" \
  "$SCRIPT_DIR/call.sh" \
    --vendor grok \
    --output-dir "$grok_stdin_dir" \
    --min-success 1 >/dev/null
if ! grep -q 'GROK_STDIN_MARKER' "$grok_stdin_capture/prompt" \
    || ! grep -q 'second line' "$grok_stdin_capture/prompt"; then
  printf "FAIL: expected multiline stdin to reach fake Grok\n" >&2
  exit 1
fi

for kind in system instruction context; do
  kind_capture="$WORK/grok-$kind-capture"
  kind_dir="$RUN_ROOT/grok-$kind"
  case "$kind" in
    system)
      kind_args=(--system "GROK_SYSTEM_ONLY")
      marker="GROK_SYSTEM_ONLY"
      ;;
    instruction)
      kind_args=(--instruction "GROK_INSTRUCTION_ONLY")
      marker="GROK_INSTRUCTION_ONLY"
      ;;
    context)
      kind_args=(--context-file "$grok_context_file")
      marker="GROK_CONTEXT_MARKER"
      ;;
  esac
  PATH="$BIN_DIR:$PATH" \
  FAKE_GROK_CAPTURE_DIR="$kind_capture" \
  "$SCRIPT_DIR/call.sh" \
    --vendor grok \
    --prompt "GROK_USER_ONLY" \
    "${kind_args[@]}" \
    --output-dir "$kind_dir" \
    --min-success 1 >/dev/null
  if ! grep -q "$marker" "$kind_capture/prompt" \
      || ! grep -q 'GROK_USER_ONLY' "$kind_capture/prompt"; then
    printf "FAIL: expected standalone Grok %s transport\n" "$kind" >&2
    exit 1
  fi
done

grok_env_clean_capture="$WORK/grok-env-clean-capture"
PATH="$BIN_DIR:$PATH" \
FAKE_GROK_CAPTURE_DIR="$grok_env_clean_capture" \
"$SCRIPT_DIR/call.sh" \
  --vendor grok \
  --prompt "GROK_ENV_CLEAN" \
  --output-dir "$RUN_ROOT/grok-env-clean" \
  --min-success 1 >/dev/null
if [ -n "$(cat "$grok_env_clean_capture/env")" ] \
    || [ -n "${GROK_SMOKE_MARKER:-}" ]; then
  printf "FAIL: expected per-call Grok env not to leak to later call or parent\n" >&2
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

grok_schema_dir="$RUN_ROOT/grok-schema"
mkdir -p "$grok_schema_dir"
PATH="$BIN_DIR:$PATH" \
FAKE_GROK_CAPTURE_DIR="$grok_capture" \
"$SCRIPT_DIR/call.sh" \
  --vendor Grok \
  --prompt "return Grok schema output" \
  --schema-file "$schema_file" \
  --output-dir "$grok_schema_dir" \
  --id grok-schema \
  --min-success 1 >/dev/null

if ! grep -q '"structured_output"' "$grok_schema_dir/grok-schema/out" \
    || ! grep -q '"answer": "READY"' "$grok_schema_dir/grok-schema/out" \
    || ! grep -Fxq -- '--json-schema' "$grok_capture/argv"; then
  printf "FAIL: expected Grok native schema arg and output envelope\n" >&2
  cat "$grok_schema_dir/grok-schema/out" >&2
  exit 1
fi

grok_bad_schema_dir="$RUN_ROOT/grok-bad-schema"
mkdir -p "$grok_bad_schema_dir"
if PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor Grok \
    --prompt "return malformed Grok schema output" \
    --schema-file "$schema_file" \
    --native-arg --fake-malformed-schema \
    --output-dir "$grok_bad_schema_dir" \
    --id grok-bad-schema \
    --min-success 1 >/dev/null 2>&1; then
  printf "FAIL: expected malformed Grok schema event to fail\n" >&2
  exit 1
fi
if ! grep -q '^exit_code=1$' "$grok_bad_schema_dir/grok-bad-schema/status" \
    || ! grep -q 'structured output failed' "$grok_bad_schema_dir/grok-bad-schema/status"; then
  printf "FAIL: expected retained Grok schema failure status and reason\n" >&2
  cat "$grok_bad_schema_dir/grok-bad-schema/status" >&2
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

agy_schema_dir="$RUN_ROOT/agy-schema-rejected"
mkdir -p "$agy_schema_dir"
agy_schema_log="$WORK/agy-schema-reject.log"
if PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor Agy \
    --prompt "schema rejection probe" \
    --schema-file "$schema_file" \
    --output-dir "$agy_schema_dir" \
    --id agy-schema \
    --min-success 1 >"$agy_schema_log" 2>&1; then
  printf "FAIL: expected --schema-file with Agy to be rejected\n" >&2
  cat "$agy_schema_log" >&2
  exit 1
fi
if ! grep -q -- '--schema-file is not supported on agy' "$agy_schema_log"; then
  printf "FAIL: expected Agy schema rejection message\n" >&2
  cat "$agy_schema_log" >&2
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

grok_failure_dir="$RUN_ROOT/grok-failure"
mkdir -p "$grok_failure_dir"
if PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor Grok \
    --prompt "controlled Grok failure" \
    --native-arg --fake-fail \
    --output-dir "$grok_failure_dir" \
    --id grok-failure \
    --min-success 1 >/dev/null 2>&1; then
  printf "FAIL: expected controlled fake Grok failure\n" >&2
  exit 1
fi
if ! grep -q '^exit_code=17$' "$grok_failure_dir/grok-failure/status" \
    || ! grep -q 'controlled fake Grok failure' "$grok_failure_dir/grok-failure/stream"; then
  printf "FAIL: expected retained fake Grok failure status and stream\n" >&2
  exit 1
fi

grok_empty_dir="$RUN_ROOT/grok-empty-response"
mkdir -p "$grok_empty_dir"
if PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor Grok \
    --prompt "controlled empty Grok response" \
    --native-arg --fake-no-text \
    --output-dir "$grok_empty_dir" \
    --id grok-empty \
    --min-success 1 >/dev/null 2>&1; then
  printf "FAIL: expected Grok end event without text to fail\n" >&2
  exit 1
fi
if ! grep -q '^exit_code=1$' "$grok_empty_dir/grok-empty/status" \
    || ! grep -q 'without response text' "$grok_empty_dir/grok-empty/status"; then
  printf "FAIL: expected retained Grok empty-response failure reason\n" >&2
  exit 1
fi

# Fake idle probe for the watchdog cases below: records each prompt it receives
# under $FAKE_PROBE_STATE_DIR and answers from $FAKE_PROBE_ANSWERS (space-
# separated `extend:N` | `kill` verdicts, the last one repeating).
cat > "$WORK/fake-probe.sh" <<'FAKE_PROBE'
#!/usr/bin/env bash
state="${FAKE_PROBE_STATE_DIR:?}"
mkdir -p "$state"
n=$(ls "$state" | grep -c '^prompt\.' || true)
n=$((n + 1))
cat > "$state/prompt.$n"
set -- ${FAKE_PROBE_ANSWERS:-kill}
i=1
answer="$1"
while [ "$i" -lt "$n" ] && [ "$#" -gt 1 ]; do
  shift
  answer="$1"
  i=$((i + 1))
done
case "$answer" in
  extend:*) printf 'VERDICT: extend %s\nfake probe grant\n' "${answer#extend:}" ;;
  *) printf 'VERDICT: kill\nfake probe kill\n' ;;
esac
FAKE_PROBE
chmod +x "$WORK/fake-probe.sh"

# Mechanical watchdog without idle-probe flags: byte-for-byte the old contract.
# The fake probe is reachable in the environment but must never be consulted.
grok_timeout_dir="$RUN_ROOT/grok-timeout"
grok_timeout_probe_state="$WORK/grok-timeout-probe-state"
mkdir -p "$grok_timeout_dir"
if PATH="$BIN_DIR:$PATH" \
    VENDORS_IDLE_PROBE_FAKE="$WORK/fake-probe.sh" \
    FAKE_PROBE_STATE_DIR="$grok_timeout_probe_state" \
    "$SCRIPT_DIR/call.sh" \
    --vendor Grok \
    --prompt "controlled Grok timeout" \
    --native-arg --fake-delay \
    --native-arg 8 \
    --timeout 1 \
    --output-dir "$grok_timeout_dir" \
    --id grok-timeout \
    --min-success 1 >/dev/null 2>&1; then
  printf "FAIL: expected controlled fake Grok timeout\n" >&2
  exit 1
fi
if ! grep -q '^exit_code=124$' "$grok_timeout_dir/grok-timeout/status" \
    || ! grep -q '^reason=timeout$' "$grok_timeout_dir/grok-timeout/status"; then
  printf "FAIL: expected Grok timeout status and reason\n" >&2
  cat "$grok_timeout_dir/grok-timeout/status" >&2
  exit 1
fi
if grep -q 'idle_probe' "$grok_timeout_dir/grok-timeout/status" \
    || grep -q 'idle-probe' "$grok_timeout_dir/grok-timeout/log" \
    || [ -e "$grok_timeout_probe_state" ]; then
  printf "FAIL: expected no idle-probe activity without --idle-probe-vendor\n" >&2
  cat "$grok_timeout_dir/grok-timeout/status" >&2
  exit 1
fi

# Idle probe through call.sh: the vendor is silent past --timeout 2 with 1s
# extend windows; the fake probe grants 3s once, then says kill. The call must
# outlive the mechanical deadline by the grant, record both verdicts, and
# still end as a timeout kill.
idle_probe_dir="$RUN_ROOT/grok-idle-probe"
idle_probe_state="$WORK/idle-probe-state"
mkdir -p "$idle_probe_dir"
idle_probe_started=$(date +%s)
if PATH="$BIN_DIR:$PATH" \
    VENDORS_IDLE_PROBE_FAKE="$WORK/fake-probe.sh" \
    FAKE_PROBE_STATE_DIR="$idle_probe_state" \
    FAKE_PROBE_ANSWERS="extend:3 kill" \
    "$SCRIPT_DIR/call.sh" \
    --vendor Grok \
    --prompt "controlled Grok idle probe" \
    --native-arg --fake-delay \
    --native-arg 60 \
    --timeout 2 \
    --timeout-extend 1 \
    --idle-probe-vendor claude \
    --idle-probe-model fake-probe \
    --idle-probe-timeout 10 \
    --output-dir "$idle_probe_dir" \
    --id grok-idle-probe \
    --min-success 1 >/dev/null 2>&1; then
  printf "FAIL: expected idle-probed Grok call to end by kill\n" >&2
  exit 1
fi
idle_probe_elapsed=$(( $(date +%s) - idle_probe_started ))
idle_probe_status="$idle_probe_dir/grok-idle-probe/status"
idle_probe_log="$idle_probe_dir/grok-idle-probe/log"
if ! grep -q '^exit_code=124$' "$idle_probe_status" \
    || ! grep -q '^reason=timeout$' "$idle_probe_status" \
    || ! grep -q '^idle_probe_verdicts=2$' "$idle_probe_status" \
    || ! grep -q '^idle_probe_last_verdict=kill$' "$idle_probe_status"; then
  printf "FAIL: expected timeout status with two recorded idle-probe verdicts\n" >&2
  cat "$idle_probe_status" >&2
  exit 1
fi
if ! grep -q '^idle-probe: verdict 1: extend 3' "$idle_probe_log" \
    || ! grep -q '^idle-probe: extending 3s' "$idle_probe_log" \
    || ! grep -q '^idle-probe: verdict 2: kill' "$idle_probe_log"; then
  printf "FAIL: expected both idle-probe verdicts in the call log\n" >&2
  cat "$idle_probe_log" >&2
  exit 1
fi
if [ ! -s "$idle_probe_state/prompt.1" ] || [ ! -s "$idle_probe_state/prompt.2" ] \
    || [ -e "$idle_probe_state/prompt.3" ]; then
  printf "FAIL: expected exactly two idle-probe prompts\n" >&2
  ls "$idle_probe_state" >&2 || true
  exit 1
fi
if ! grep -q '^# Idle-timeout probe' "$idle_probe_state/prompt.1" \
    || ! grep -q '\*\*Stage\*\*: `grok-idle-probe`' "$idle_probe_state/prompt.1" \
    || ! grep -q '\*\*Configured idle cap\*\*: `2s`' "$idle_probe_state/prompt.1" \
    || ! grep -q '^### Process tree' "$idle_probe_state/prompt.1" \
    || ! grep -q 'sleep 60' "$idle_probe_state/prompt.1" \
    || ! grep -q '^### Stream output tail' "$idle_probe_state/prompt.1"; then
  printf "FAIL: expected idle-probe prompt to carry label, cap, process tree, and stream evidence\n" >&2
  cat "$idle_probe_state/prompt.1" >&2
  exit 1
fi
if [ "$idle_probe_elapsed" -lt 5 ]; then
  printf "FAIL: expected the 3s probe grant to delay the kill (elapsed %ss)\n" "$idle_probe_elapsed" >&2
  exit 1
fi

# Idle probe budget: a probe that always extends is bounded by
# --idle-probe-max-total, so the call still dies after one truncated grant.
idle_budget_dir="$RUN_ROOT/grok-idle-budget"
idle_budget_state="$WORK/idle-budget-state"
mkdir -p "$idle_budget_dir"
if PATH="$BIN_DIR:$PATH" \
    VENDORS_IDLE_PROBE_FAKE="$WORK/fake-probe.sh" \
    FAKE_PROBE_STATE_DIR="$idle_budget_state" \
    FAKE_PROBE_ANSWERS="extend:3" \
    "$SCRIPT_DIR/call.sh" \
    --vendor Grok \
    --prompt "controlled Grok idle probe budget" \
    --native-arg --fake-delay \
    --native-arg 60 \
    --timeout 1 \
    --idle-probe-vendor claude \
    --idle-probe-max-total 2 \
    --output-dir "$idle_budget_dir" \
    --id grok-idle-budget \
    --min-success 1 >/dev/null 2>&1; then
  printf "FAIL: expected budget-bounded idle-probed Grok call to end by kill\n" >&2
  exit 1
fi
idle_budget_status="$idle_budget_dir/grok-idle-budget/status"
idle_budget_log="$idle_budget_dir/grok-idle-budget/log"
if ! grep -q '^exit_code=124$' "$idle_budget_status" \
    || ! grep -q '^idle_probe_verdicts=1$' "$idle_budget_status" \
    || ! grep -q '^idle_probe_last_verdict=extend 3$' "$idle_budget_status" \
    || ! grep -q '^idle-probe: extending 2s (2s of 2s budget used)' "$idle_budget_log" \
    || ! grep -q '^idle-probe: extension budget exhausted' "$idle_budget_log" \
    || [ -e "$idle_budget_state/prompt.2" ]; then
  printf "FAIL: expected --idle-probe-max-total to bound the extension\n" >&2
  cat "$idle_budget_status" "$idle_budget_log" >&2
  exit 1
fi

# Idle-probe flag validation happens before any call.
idle_flags_log="$WORK/idle-flags.log"
if PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor Grok \
    --prompt "idle probe flag validation" \
    --idle-probe-model fake-probe \
    --output-dir "$RUN_ROOT/idle-flags" \
    --dry-run >"$idle_flags_log" 2>&1 \
    || ! grep -q 'require --idle-probe-vendor' "$idle_flags_log"; then
  printf "FAIL: expected --idle-probe-model without --idle-probe-vendor to be rejected\n" >&2
  cat "$idle_flags_log" >&2
  exit 1
fi
if PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor Grok \
    --prompt "idle probe flag validation" \
    --idle-probe-vendor nosuch \
    --output-dir "$RUN_ROOT/idle-flags" \
    --dry-run >"$idle_flags_log" 2>&1 \
    || ! grep -q 'unknown vendor: nosuch' "$idle_flags_log"; then
  printf "FAIL: expected unknown --idle-probe-vendor to be rejected\n" >&2
  cat "$idle_flags_log" >&2
  exit 1
fi

# idle-probe.sh on its own: composition, forced verdicts, fail-closed parsing,
# and the real call.sh path through the fake Claude CLI.
probe_dir="$RUN_ROOT/idle-probe-standalone"
mkdir -p "$probe_dir"
printf 'Running tests...\nstill running\n' > "$probe_dir/stdout.log"
printf 'warn: slow\n' > "$probe_dir/stderr.log"
printf 'partial stream output' > "$probe_dir/stream"
sleep 60 &
probe_sleep_pid=$!
probe_prompt=$("$SCRIPT_DIR/idle-probe.sh" --compose-only \
  --pid "$$" --idle-sec 45 --idle-cap-sec 30 --label smoke-stage \
  --stream "$probe_dir/stream" --stdout "$probe_dir/stdout.log" --stderr "$probe_dir/stderr.log")
kill "$probe_sleep_pid" 2>/dev/null || true
wait "$probe_sleep_pid" 2>/dev/null || true
for marker in \
    '^# Idle-timeout probe' \
    '^## Probe inputs' \
    '\*\*Stage\*\*: `smoke-stage`' \
    "\*\*Subagent pid\*\*: \`$$\`" \
    '\*\*Idle duration\*\*: `45s`' \
    '\*\*Configured idle cap\*\*: `30s`' \
    '\*\*Stream output file size_bytes\*\*: `21`' \
    '^partial stream output$' \
    '^### Process tree' \
    'sleep 60' \
    '^### Stdout tail' \
    '^Running tests\.\.\.$' \
    '^### Stderr tail' \
    '^warn: slow$'; do
  if ! printf '%s\n' "$probe_prompt" | grep -q -- "$marker"; then
    printf "FAIL: expected composed idle-probe prompt to contain %s\n" "$marker" >&2
    printf '%s\n' "$probe_prompt" >&2
    exit 1
  fi
done
probe_args=(--pid "$$" --idle-sec 45 --idle-cap-sec 30 --label smoke-stage \
  --stdout "$probe_dir/stdout.log" --stderr "$probe_dir/stderr.log")
if [ "$(VENDORS_IDLE_PROBE_FAKE_VERDICT=extend:99999 "$SCRIPT_DIR/idle-probe.sh" "${probe_args[@]}" | head -n 1)" != "extend 1800" ] \
    || [ "$(VENDORS_IDLE_PROBE_FAKE_VERDICT=kill "$SCRIPT_DIR/idle-probe.sh" "${probe_args[@]}" | head -n 1)" != "kill" ]; then
  printf "FAIL: expected forced idle-probe verdicts to clamp and pass through\n" >&2
  exit 1
fi
cat > "$WORK/garbage-probe.sh" <<'FAKE_GARBAGE'
#!/usr/bin/env bash
cat > /dev/null
printf 'I cannot decide.\n'
FAKE_GARBAGE
chmod +x "$WORK/garbage-probe.sh"
garbage_verdict=$(VENDORS_IDLE_PROBE_FAKE="$WORK/garbage-probe.sh" "$SCRIPT_DIR/idle-probe.sh" "${probe_args[@]}")
if [ "$(printf '%s\n' "$garbage_verdict" | head -n 1)" != "kill" ] \
    || ! printf '%s\n' "$garbage_verdict" | grep -q '^rationale: .*parseable'; then
  printf "FAIL: expected an unparsable probe answer to fail closed to kill\n" >&2
  printf '%s\n' "$garbage_verdict" >&2
  exit 1
fi
real_probe_verdict=$(PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/idle-probe.sh" "${probe_args[@]}" \
  --probe-vendor claude --probe-model fake-probe --probe-timeout 20 \
  --output-dir "$probe_dir/real")
if [ "$(printf '%s\n' "$real_probe_verdict" | head -n 1)" != "extend 120" ] \
    || ! grep -q '^exit_code=0$' "$probe_dir/real/probe/status" \
    || ! grep -q '^model=fake-probe$' "$probe_dir/real/probe/status"; then
  printf "FAIL: expected idle-probe.sh to reach the fake Claude CLI through call.sh\n" >&2
  printf '%s\n' "$real_probe_verdict" >&2
  cat "$probe_dir/real/probe/status" >&2 || true
  exit 1
fi
missing_cli_verdict=$(PATH="/usr/bin:/bin" /bin/bash "$SCRIPT_DIR/idle-probe.sh" "${probe_args[@]}" \
  --probe-vendor claude --output-dir "$probe_dir/missing")
if [ "$(printf '%s\n' "$missing_cli_verdict" | head -n 1)" != "kill" ]; then
  printf "FAIL: expected a missing probe CLI to fail closed to kill\n" >&2
  printf '%s\n' "$missing_cli_verdict" >&2
  exit 1
fi

doctor_dir="$RUN_ROOT/grok-doctor"
PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/doctor.sh" \
  --vendor XAI \
  --timeout 5 \
  --output-dir "$doctor_dir" >/dev/null
if [ "$(cat "$doctor_dir/grok.status")" != "ok" ] \
    || [ ! -s "$doctor_dir/grok-call/grok/out" ]; then
  printf "FAIL: expected fake Grok doctor path to report ready\n" >&2
  exit 1
fi

doctor_failure_dir="$RUN_ROOT/grok-doctor-failure"
if PATH="$BIN_DIR:$PATH" FAKE_GROK_FORCE_FAIL=1 "$SCRIPT_DIR/doctor.sh" \
    --vendor grok \
    --timeout 5 \
    --output-dir "$doctor_failure_dir" >/dev/null 2>&1; then
  printf "FAIL: expected fake Grok doctor failure path\n" >&2
  exit 1
fi
if [ "$(cat "$doctor_failure_dir/grok.status")" = "ok" ]; then
  printf "FAIL: expected failed fake Grok doctor status\n" >&2
  exit 1
fi

doctor_missing_dir="$RUN_ROOT/grok-doctor-missing"
if PATH="/usr/bin:/bin" /bin/bash "$SCRIPT_DIR/doctor.sh" \
    --vendor grok \
    --timeout 5 \
    --output-dir "$doctor_missing_dir" >/dev/null 2>&1; then
  printf "FAIL: expected missing Grok doctor path\n" >&2
  exit 1
fi
if ! grep -q 'CLI not on PATH' "$doctor_missing_dir/grok.status"; then
  printf "FAIL: expected missing Grok doctor diagnostic\n" >&2
  exit 1
fi

no_python_dir="$RUN_ROOT/grok-no-python"
no_python_capture="$WORK/grok-no-python-capture"
mkdir -p "$no_python_dir"
printf "GROK_NO_PYTHON_PROMPT\n" > "$no_python_dir/prompt.txt"
(
  # Exercise the real production functions while replacing only interpreter
  # discovery. No production test knob is needed, and fake Grok must remain
  # uninvoked because correctness depends on the normalizer.
  # shellcheck source=vendor-launch.sh
  . "$SCRIPT_DIR/vendor-launch.sh"
  vendors_python() {
    return 0
  }
  VENDORS_TRANSCRIPT_FILE="$no_python_dir/grok-transcript.jsonl"
  VENDORS_VENDOR_ID="grok"
  VENDORS_VENDOR_CLI="grok"
  VENDORS_RESOLVED_MODEL="grok-4.5"
  VENDORS_RESOLVED_EFFORT="low"
  VENDORS_CWD=""
  VENDORS_YOLO=0
  VENDORS_DRY_RUN=0
  VENDORS_SCHEMA_FILE=""
  VENDORS_NATIVE_ARGS=()
  VENDORS_ENV=()
  export FAKE_GROK_CAPTURE_DIR="$no_python_capture"

  no_python_status=0
  vendors_run_grok \
    "$no_python_dir/prompt.txt" \
    "$no_python_dir/out" \
    > "$no_python_dir/stdout" \
    2> "$no_python_dir/diagnostic" \
    || no_python_status=$?
  if [ "$no_python_status" -ne 69 ]; then
    printf "FAIL: expected Grok missing-Python guard exit 69, got %s\n" \
      "$no_python_status" >&2
    exit 1
  fi
  VENDORS_DRY_RUN=1
  vendors_run_grok \
    "$no_python_dir/prompt.txt" \
    "$no_python_dir/dry-run-out" \
    > "$no_python_dir/dry-run"
  VENDORS_DRY_RUN=0
  vendors_collect_usage \
    grok \
    "$no_python_dir/out" \
    "$no_python_dir/grok-transcript.jsonl" \
    "$no_python_dir/usage.json"
  vendors_collect_usage \
    agy \
    "$no_python_dir/agy-out" \
    "$no_python_dir/agy-transcript" \
    "$no_python_dir/agy-usage.json"
)

if [ -e "$no_python_capture/argv" ] \
    || ! grep -q 'requires python3 or python' "$no_python_dir/diagnostic" \
    || ! grep -q '^vendor=grok$' "$no_python_dir/dry-run" \
    || [ -e "$no_python_dir/agy-usage.json" ]; then
  printf "FAIL: expected Grok Python guard before invocation without changing Agy behavior\n" >&2
  exit 1
fi
python3 - "$no_python_dir/usage.json" <<'PY'
import json
import sys

usage = json.load(open(sys.argv[1], encoding="utf-8"))
assert usage == {
    "available": False,
    "provider": "grok",
    "total_tokens": None,
    "reason": "python3 or python is required for Grok streaming JSON normalization",
}
PY

printf "OK: vendors smoke test passed (host-os/pty selection + native sessions + 5 calls x 5 vendors + Grok aliases/runtime/schema/failure/timeout/doctor + idle probe)\n"
