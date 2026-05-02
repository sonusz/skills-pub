#!/usr/bin/env bash
# Smoke test for panel-review without real model calls.
#
# It puts fake codex/claude/gemini binaries at the front of PATH, then exercises
# doctor.sh, launch.sh, and synthesize.sh through the shared vendors module.
set -eo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$TEST_DIR/.." && pwd)"
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
mkdir -p "$BIN_DIR" "$RUN_DIR"

cat > "$PROMPT_FILE" <<'PROMPT'
Review this tiny config for ambiguity:

timeout = 30
retry = true
PROMPT

cat > "$BIN_DIR/codex" <<'FAKE_CODEX'
#!/usr/bin/env bash
out=""
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
Vendors: openai ✅ | claude ✅ | gemini ✅

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
prompt=$(cat)
if printf "%s" "$prompt" | grep -qi 'single word READY'; then
  printf "READY\n"
elif printf "%s" "$prompt" | grep -qi 'Panel outputs:'; then
  cat <<'OUT'
━━━ Panel Review ━━━
Task: smoke test
Vendors: openai ✅ | claude ✅ | gemini ✅

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

cat > "$BIN_DIR/gemini" <<'FAKE_GEMINI'
#!/usr/bin/env bash
prompt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt|-p)
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
  printf "gemini panel smoke output\n"
fi
FAKE_GEMINI

chmod +x "$BIN_DIR/codex" "$BIN_DIR/claude" "$BIN_DIR/gemini"

OPENAI_ARGS=()
while IFS= read -r -d '' arg; do
  OPENAI_ARGS+=("$arg")
done < <(panel_call_args "$ROOT_DIR/vendors.yaml" panel openai)
OPENAI_ARG_TEXT=$(printf '<%s>' "${OPENAI_ARGS[@]}")
OPENAI_EXPECTED_EFFORT=$(panel_yaml_value "$ROOT_DIR/vendors.yaml" panel openai effort)
if [ -n "$OPENAI_EXPECTED_EFFORT" ] && [[ "$OPENAI_ARG_TEXT" != *"<--effort><$OPENAI_EXPECTED_EFFORT>"* ]]; then
  printf "FAIL: expected panel openai call args to include configured effort=%s\n" "$OPENAI_EXPECTED_EFFORT" >&2
  printf "%s\n" "$OPENAI_ARG_TEXT" >&2
  exit 1
fi

SYNTHESIS_ARGS=()
while IFS= read -r -d '' arg; do
  SYNTHESIS_ARGS+=("$arg")
done < <(panel_call_args "$ROOT_DIR/vendors.yaml" synthesis synthesis)
SYNTHESIS_ARG_TEXT=$(printf '<%s>' "${SYNTHESIS_ARGS[@]}")
SYNTHESIS_EXPECTED_EFFORT=$(panel_yaml_value "$ROOT_DIR/vendors.yaml" synthesis synthesis effort)
if [ -n "$SYNTHESIS_EXPECTED_EFFORT" ] && [[ "$SYNTHESIS_ARG_TEXT" != *"<--effort><$SYNTHESIS_EXPECTED_EFFORT>"* ]]; then
  printf "FAIL: expected synthesis call args to include configured effort=%s\n" "$SYNTHESIS_EXPECTED_EFFORT" >&2
  printf "%s\n" "$SYNTHESIS_ARG_TEXT" >&2
  exit 1
fi

PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/doctor.sh" "$ROOT_DIR/vendors.yaml" >/dev/null
PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/launch.sh" "$PROMPT_FILE" "$ROOT_DIR/vendors.yaml" "$RUN_DIR" >/dev/null
PATH="$BIN_DIR:$PATH" "$SCRIPT_DIR/synthesize.sh" "$PROMPT_FILE" "$ROOT_DIR/vendors.yaml" "$RUN_DIR" >/dev/null

for file in openai/out claude/out gemini/out synthesis/out openai/status claude/status gemini/status synthesis/status; do
  if [ ! -s "$RUN_DIR/$file" ]; then
    printf "FAIL: expected non-empty smoke output %s\n" "$RUN_DIR/$file" >&2
    exit 1
  fi
done

for id in openai claude gemini synthesis; do
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

printf "OK: panel-review smoke test passed\n"
