#!/usr/bin/env bash
# Shared vendor launch helpers for panel-review.
#
# Source this file from doctor.sh and launch.sh; do not execute it directly.
# Keeping the per-vendor commands here prevents the readiness probe from
# drifting away from the real panel launch path.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  printf "vendor-launch.sh is a library; source it from doctor.sh or launch.sh.\n" >&2
  exit 2
fi

panel_read_models() {
  local models_conf="$1"

  CLAUDE_MODEL=$(awk -F= '/^claude=/{print $2; exit}' "$models_conf")
  GEMINI_MODEL=$(awk -F= '/^gemini=/{print $2; exit}' "$models_conf")
  CODEX_MODEL=$(awk -F= '/^codex=/{print $2; exit}' "$models_conf")
}

panel_vendor_active() {
  local vendor="$1"
  local model="$2"

  command -v "$vendor" >/dev/null 2>&1 && [ -n "$model" ]
}

panel_start_vendor() {
  local vendor="$1"
  local model="$2"
  local prompt_file="$3"
  local run_dir="$4"

  case "$vendor" in
    claude)
      claude -p --model "$model" --output-format text --tools "" --no-session-persistence "$(cat "$prompt_file")" \
        < /dev/null > "$run_dir/claude.out" 2>&1 &
      ;;
    gemini)
      # stdin redirected to /dev/null: gemini CLI otherwise interactively
      # prompts ("Opening authentication page..." etc.) and blocks forever
      # when launched headless. The other CLIs do not need stdin in these
      # non-interactive modes, so they use the same closed-stdin path.
      gemini --model "$model" -p "$(cat "$prompt_file")" \
        < /dev/null > "$run_dir/gemini.out" 2>&1 &
      ;;
    codex)
      # Codex quirk: stdout is an execution transcript. Capture the final
      # assistant message explicitly so the synthesizer reads the review.
      codex exec --model "$model" --skip-git-repo-check --output-last-message "$run_dir/codex.out" \
        "$(cat "$prompt_file")" \
        < /dev/null > "$run_dir/codex-stdout.out" 2>&1 &
      ;;
    *)
      printf "unknown panel-review vendor: %s\n" "$vendor" >&2
      return 2
      ;;
  esac

  PANEL_VENDOR_PID=$!
}

panel_launch_vendor() {
  panel_start_vendor "$@"
  wait "$PANEL_VENDOR_PID"
}

panel_finalize_outputs() {
  local run_dir="$1"

  # Codex fallback: if the "write complete analysis to codex.out" directive
  # was ignored, fall back to the stdout summary so the synthesizer has an
  # explicit degraded output instead of silently getting nothing.
  if [ ! -s "$run_dir/codex.out" ] && [ -s "$run_dir/codex-stdout.out" ]; then
    {
      printf '[FALLBACK: codex did not honor the file-write directive; this is the stdout summary, not full analysis. Do not treat its brevity as substantive divergence.]\n\n'
      cat "$run_dir/codex-stdout.out"
    } > "$run_dir/codex.out"
  fi
}
