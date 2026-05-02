#!/usr/bin/env bash
# Smoke test for the shared vendors module without real model calls.
#
# Exercises the cross-vendor fan-out contract: three separate callers each make
# one call to all three configured vendors, for three total calls and nine
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
json=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-format)
      if [ "${2-}" = "json" ]; then
        json=1
      fi
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
if [ "$json" = "1" ]; then
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
json=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt|-p)
      prompt="$2"
      shift 2
      ;;
    --output-format)
      if [ "${2-}" = "json" ]; then
        json=1
      fi
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
if [ "$json" = "1" ]; then
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

chmod +x "$BIN_DIR/codex" "$BIN_DIR/claude" "$BIN_DIR/gemini"

for caller in openai claude gemini; do
  call_dir="$RUN_ROOT/$caller"
  mkdir -p "$call_dir"

  PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/call.sh" \
    --vendor OpenAI \
    --vendor Claude \
    --vendor Gemini \
    --prompt "caller=$caller fan out to openai, claude, gemini" \
    --output-dir "$call_dir" \
    --min-success 3 >/dev/null

  for vendor in openai claude gemini; do
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
    *) printf "unknown\n" ;;
  esac
}

lower() {
  printf "%s" "$1" | tr '[:upper:]' '[:lower:]'
}

for effort_vendor in OpenAI Claude Gemini; do
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

printf "OK: vendors smoke test passed (3 calls x 3 vendors + duplicate vendor)\n"
