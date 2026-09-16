#!/usr/bin/env bash
# Smoke test for panel-review without real model calls.
#
# It puts fake codex/claude/agy/grok/cursor-agent binaries at the front of PATH,
# then
# exercises doctor.sh, launch.sh, and synthesize.sh through the shared vendors
# module.
#
# PANEL_SMOKE_SKILL_DIR selects the skill under test (default: panel-review).
# Point it at a skill whose scripts/ wrap panel-review's through a
# skills/panel-review link (multi-lens-review) to exercise the wrappers and
# that skill's sample-vendors.yaml; the fakes assume the same five panel ids.
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${PANEL_SMOKE_SKILL_DIR:-$TEST_DIR/..}" && pwd)"
SCRIPT_DIR="$ROOT_DIR/scripts"
# shellcheck source=panel-config.sh
. "$SCRIPT_DIR/panel-config.sh"

WORK=$(mktemp -d /tmp/panel-review-smoke.XXXXXX)
cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT

BIN_DIR="$WORK/bin"
RUN_DIR="$WORK/run"
PROMPT_FILE="$WORK/prompt.txt"
MARKER_PREFIX="$WORK/marker"
mkdir -p "$BIN_DIR" "$RUN_DIR"

cat > "$PROMPT_FILE" <<'PROMPT'
Review the local file at `SKILL.md` for ambiguity. Read it from the current
working directory with your file tools. Do not modify it.
PROMPT

cat > "$BIN_DIR/codex" <<'FAKE_CODEX'
#!/usr/bin/env bash
out=""
if [ -n "${PANEL_SMOKE_MARKERS:-}" ]; then
  printf "pwd=%s\nargs=%s\n" "$PWD" "$*" > "${PANEL_SMOKE_MARKERS}.codex"
fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-last-message)
      out="$2"
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
elif printf "%s" "$prompt" | grep -qi 'Panel outputs:'; then
  response="━━━ Panel Review ━━━
Task: smoke test
Vendors: openai ✅ | claude ✅ | agy ✅ | grok ✅ | cursor ✅

## Consensus
Smoke consensus.

## Divergence
None

## Recommendations
No action needed for smoke.
━━━ End ━━━"
else
  response="openai panel smoke output"
fi
[ -n "$out" ] && printf "%s\n" "$response" > "$out"
printf "fake codex transcript\n"
FAKE_CODEX

cat > "$BIN_DIR/claude" <<'FAKE_CLAUDE'
#!/usr/bin/env bash
if [ -n "${PANEL_SMOKE_MARKERS:-}" ]; then
  printf "pwd=%s\nargs=%s\n" "$PWD" "$*" > "${PANEL_SMOKE_MARKERS}.claude"
fi
prompt=$(cat)
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  printf "READY\n"
elif printf "%s" "$prompt" | grep -qi 'Panel outputs:'; then
  cat <<'OUT'
━━━ Panel Review ━━━
Task: smoke test
Vendors: openai ✅ | claude ✅ | agy ✅ | grok ✅ | cursor ✅

## Consensus
Smoke consensus.

## Divergence
None

## Recommendations
No action needed for smoke.
━━━ End ━━━
OUT
else
  printf "claude panel smoke output\n"
fi
FAKE_CLAUDE

cat > "$BIN_DIR/agy" <<'FAKE_AGY'
#!/usr/bin/env bash
if [ -n "${PANEL_SMOKE_MARKERS:-}" ]; then
  printf "pwd=%s\nargs=%s\n" "$PWD" "$*" > "${PANEL_SMOKE_MARKERS}.agy"
fi
prompt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --print|-p)
      prompt="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  printf "READY\n"
else
  printf "agy panel smoke output\n"
fi
FAKE_AGY


cat > "$BIN_DIR/grok" <<'FAKE_GROK'
#!/usr/bin/env bash
format=""
prompt_file=""
if [ -n "${PANEL_SMOKE_MARKERS:-}" ]; then
  printf "pwd=%s\nargs=%s\n" "$PWD" "$*" > "${PANEL_SMOKE_MARKERS}.grok"
fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-format)
      format="${2-}"
      shift 2
      ;;
    --prompt-file)
      prompt_file="${2-}"
      shift 2
      ;;
    --model|--reasoning-effort|--cwd)
      shift 2
      ;;
    --yolo)
      shift
      ;;
    *)
      shift
      ;;
  esac
done
if [ "$format" != "streaming-json" ] || [ -z "$prompt_file" ]; then
  printf '{"type":"error","message":"invalid fake Grok invocation"}\n'
  exit 2
fi
prompt=$(cat "$prompt_file")
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  response="READY"
else
  response="grok panel smoke output"
fi
python3 - "$response" <<'PY'
import json
import sys

response = sys.argv[1]
print(json.dumps({"type": "text", "data": response}))
print(json.dumps({
    "type": "end",
    "usage": {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
    },
}))
PY
FAKE_GROK

cat > "$BIN_DIR/cursor-agent" <<'FAKE_CURSOR'
#!/usr/bin/env bash
if [ -n "${PANEL_SMOKE_MARKERS:-}" ]; then
  printf "pwd=%s\nargs=%s\n" "$PWD" "$*" > "${PANEL_SMOKE_MARKERS}.cursor"
fi
prompt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --)
      shift
      prompt="${1:-}"
      break
      ;;
    *)
      shift
      ;;
  esac
done
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  response="READY"
else
  response="cursor panel smoke output"
fi
python3 - "$response" <<'PY'
import json
import sys

response = sys.argv[1]
print(json.dumps({"type": "system", "subtype": "init", "model": "fake-cursor"}))
print(json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": response}]},
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": response,
    "usage": {
        "inputTokens": 10,
        "outputTokens": 2,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
    },
}))
PY
FAKE_CURSOR

chmod +x "$BIN_DIR/codex" "$BIN_DIR/claude" "$BIN_DIR/agy" "$BIN_DIR/grok" "$BIN_DIR/cursor-agent"

OPENAI_ARGS=()
while IFS= read -r -d '' arg; do
  OPENAI_ARGS+=("$arg")
done < <(panel_call_args "$ROOT_DIR/sample-vendors.yaml" panel openai)
OPENAI_ARG_TEXT=$(printf '<%s>' "${OPENAI_ARGS[@]}")
OPENAI_EXPECTED_EFFORT=$(panel_yaml_value "$ROOT_DIR/sample-vendors.yaml" panel openai effort)
if [ -n "$OPENAI_EXPECTED_EFFORT" ] && [[ "$OPENAI_ARG_TEXT" != *"<--effort><$OPENAI_EXPECTED_EFFORT>"* ]]; then
  printf "FAIL: expected panel openai call args to include configured effort=%s\n" "$OPENAI_EXPECTED_EFFORT" >&2
  printf "%s\n" "$OPENAI_ARG_TEXT" >&2
  exit 1
fi

SYNTHESIS_ARGS=()
while IFS= read -r -d '' arg; do
  SYNTHESIS_ARGS+=("$arg")
done < <(panel_call_args "$ROOT_DIR/sample-vendors.yaml" synthesis synthesis)
SYNTHESIS_ARG_TEXT=$(printf '<%s>' "${SYNTHESIS_ARGS[@]}")
SYNTHESIS_EXPECTED_EFFORT=$(panel_yaml_value "$ROOT_DIR/sample-vendors.yaml" synthesis synthesis effort)
if [ -n "$SYNTHESIS_EXPECTED_EFFORT" ] && [[ "$SYNTHESIS_ARG_TEXT" != *"<--effort><$SYNTHESIS_EXPECTED_EFFORT>"* ]]; then
  printf "FAIL: expected synthesis call args to include configured effort=%s\n" "$SYNTHESIS_EXPECTED_EFFORT" >&2
  printf "%s\n" "$SYNTHESIS_ARG_TEXT" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/doctor.sh" "$ROOT_DIR/sample-vendors.yaml" >/dev/null
PATH="$BIN_DIR:$PATH" PANEL_SMOKE_MARKERS="$MARKER_PREFIX" \
  "$SCRIPT_DIR/launch.sh" --cwd "$ROOT_DIR" "$PROMPT_FILE" "$ROOT_DIR/sample-vendors.yaml" "$RUN_DIR" >/dev/null
PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/synthesize.sh" "$PROMPT_FILE" "$ROOT_DIR/sample-vendors.yaml" "$RUN_DIR" >/dev/null

for vendor in codex claude agy cursor; do
  marker="$MARKER_PREFIX.$vendor"
  if [ ! -s "$marker" ]; then
    printf "FAIL: expected repo-mode marker for %s\n" "$vendor" >&2
    exit 1
  fi
  if ! grep -qx "pwd=$ROOT_DIR" "$marker"; then
    printf "FAIL: expected %s to run from repo cwd %s\n" "$vendor" "$ROOT_DIR" >&2
    cat "$marker" >&2
    exit 1
  fi
done
if ! grep -q -- '--dangerously-bypass-approvals-and-sandbox' "$MARKER_PREFIX.codex"; then
  printf "FAIL: expected repo-mode codex call to use approval bypass\n" >&2
  cat "$MARKER_PREFIX.codex" >&2
  exit 1
fi
if ! grep -q -- "--cd $ROOT_DIR" "$MARKER_PREFIX.codex"; then
  printf "FAIL: expected repo-mode codex call to receive --cd %s\n" "$ROOT_DIR" >&2
  cat "$MARKER_PREFIX.codex" >&2
  exit 1
fi
if ! grep -q -- '--permission-mode bypassPermissions' "$MARKER_PREFIX.claude"; then
  printf "FAIL: expected repo-mode claude call to use bypassPermissions\n" >&2
  cat "$MARKER_PREFIX.claude" >&2
  exit 1
fi
if ! grep -q -- '--dangerously-skip-permissions' "$MARKER_PREFIX.agy"; then
  printf "FAIL: expected repo-mode agy call to skip permission prompts\n" >&2
  cat "$MARKER_PREFIX.agy" >&2
  exit 1
fi
if ! grep -q -- '--trust' "$MARKER_PREFIX.cursor" \
    || ! grep -q -- '--yolo' "$MARKER_PREFIX.cursor"; then
  printf "FAIL: expected repo-mode cursor call to use --trust and --yolo\n" >&2
  cat "$MARKER_PREFIX.cursor" >&2
  exit 1
fi

if "$SCRIPT_DIR/launch.sh" --inline \
    "$PROMPT_FILE" "$ROOT_DIR/sample-vendors.yaml" "$WORK/rejected-inline" \
    >"$WORK/rejected-inline.log" 2>&1; then
  printf "FAIL: launch.sh must reject the removed --inline mode\n" >&2
  exit 1
fi
if ! grep -q -- 'unknown option: --inline' "$WORK/rejected-inline.log"; then
  printf "FAIL: --inline rejection did not explain the unsupported option\n" >&2
  cat "$WORK/rejected-inline.log" >&2
  exit 1
fi

if PANEL_REVIEW_CWD= "$SCRIPT_DIR/launch.sh" \
    "$PROMPT_FILE" "$ROOT_DIR/sample-vendors.yaml" "$WORK/rejected-no-cwd" \
    >"$WORK/rejected-no-cwd.log" 2>&1; then
  printf "FAIL: launch.sh must require an explicit audit cwd\n" >&2
  exit 1
fi
if ! grep -q -- '--cwd is required' "$WORK/rejected-no-cwd.log"; then
  printf "FAIL: missing-cwd rejection did not explain the path-based contract\n" >&2
  cat "$WORK/rejected-no-cwd.log" >&2
  exit 1
fi

# PANEL_REVIEW_CWD in the environment is the only accepted substitute for
# --cwd; --repo is a compatibility no-op.
ENV_RUN_DIR="$WORK/run-env"
ENV_MARKER_PREFIX="$WORK/env-marker"
PATH="$BIN_DIR:$PATH" PANEL_SMOKE_MARKERS="$ENV_MARKER_PREFIX" PANEL_REVIEW_CWD="$ROOT_DIR" \
  "$SCRIPT_DIR/launch.sh" --repo "$PROMPT_FILE" "$ROOT_DIR/sample-vendors.yaml" "$ENV_RUN_DIR" >/dev/null
if ! grep -qx "pwd=$ROOT_DIR" "$ENV_MARKER_PREFIX.codex" \
    || ! grep -qx "cwd=$ROOT_DIR" "$ENV_RUN_DIR/openai/status"; then
  printf "FAIL: expected PANEL_REVIEW_CWD to supply the audit cwd when --cwd is omitted\n" >&2
  cat "$ENV_MARKER_PREFIX.codex" "$ENV_RUN_DIR/openai/status" >&2
  exit 1
fi

for file in \
  openai/out claude/out agy/out cursor/out synthesis/out \
  openai/status claude/status agy/status cursor/status synthesis/status; do
  if [ ! -s "$RUN_DIR/$file" ]; then
    printf "FAIL: expected non-empty smoke output %s\n" "$RUN_DIR/$file" >&2
    exit 1
  fi
done

for id in openai claude agy cursor synthesis; do
  status_file="$RUN_DIR/$id/status"
  expected_kind="panel"
  [ "$id" = "synthesis" ] && expected_kind="synthesis"

  if ! grep -qx "kind=$expected_kind" "$status_file"; then
    printf "FAIL: expected %s status kind=%s\n" "$id" "$expected_kind" >&2
    exit 1
  fi
  if ! grep -qx "exit_code=0" "$status_file"; then
    printf "FAIL: expected %s status exit_code=0\n" "$id" >&2
    exit 1
  fi
  for key in id vendor output log call_log; do
    if ! grep -q "^$key=" "$status_file"; then
      printf "FAIL: expected %s status to include %s\n" "$id" "$key" >&2
      exit 1
    fi
  done
  call_log=$(awk -F= '$1 == "call_log" { print $2; exit }' "$status_file")
  if [ ! -s "$call_log" ]; then
    printf "FAIL: expected non-empty call log %s\n" "$call_log" >&2
    exit 1
  fi
done

if ! grep -q '━━━ Panel Review ━━━' "$RUN_DIR/synthesis/out"; then
  printf "FAIL: synthesis smoke output did not contain panel review header\n" >&2
  exit 1
fi

printf "OK: panel-review smoke test passed (%s)\n" "$ROOT_DIR"
