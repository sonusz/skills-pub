#!/usr/bin/env bash
# Fake invoker for G15 panel runner tests.
#
# Driven by env vars set by tests BEFORE running. Stdin is the composed
# prompt. Role ∈ {reviewer, synthesizer}, vendor ∈ {claude, gemini, codex}.
#
# Behaviors (AUTODEV_PANEL_FAKE_BEHAVIOR):
#   reviewers_all_pass       — 3 reviewers emit "Verdict: pass" markdown
#   reviewers_two_fail       — claude + gemini fail with invariant_violation,
#                              codex passes
#   reviewers_one_empty      — gemini returns empty stdout (simulates empty output)
#   reviewers_one_timeout    — codex sleeps past test timeout
#   synth_pass               — synthesizer emits per_reviewer with pass
#   synth_fail_inv           — synthesizer emits per_reviewer with fail + invariant
#   synth_empty              — synthesizer returns non-JSON (triggers fallback)
#   synth_timeout            — synthesizer sleeps past timeout
set -euo pipefail
role="${AUTODEV_PANEL_FAKE_ROLE:-reviewer}"
vendor="${AUTODEV_PANEL_FAKE_VENDOR:-claude}"
behavior="${AUTODEV_PANEL_FAKE_BEHAVIOR:-reviewers_all_pass}"

# Consume stdin so it doesn't cause a broken pipe on the caller side.
cat > /dev/null

if [[ "$role" == "reviewer" ]]; then
  case "$behavior" in
    reviewers_all_pass)
      echo "## Review by $vendor"
      echo ""
      echo "Looks fine."
      echo ""
      echo "Verdict: pass"
      exit 0
      ;;
    reviewers_two_fail)
      if [[ "$vendor" == "codex" ]]; then
        echo "## $vendor review"
        echo "Verdict: pass"
        exit 0
      else
        echo "## $vendor review"
        echo ""
        echo "invariant_violation: contradicting requirement R3 vs R7"
        echo ""
        echo "Verdict: fail"
        exit 0
      fi
      ;;
    reviewers_one_empty)
      if [[ "$vendor" == "gemini" ]]; then
        # emit nothing
        exit 0
      fi
      echo "Verdict: pass"
      exit 0
      ;;
    reviewers_one_timeout)
      if [[ "$vendor" == "codex" ]]; then
        sleep 3600
      fi
      echo "Verdict: pass"
      exit 0
      ;;
    *)
      echo "unknown reviewer behavior: $behavior" >&2
      exit 2
      ;;
  esac
fi

if [[ "$role" == "synthesizer" ]]; then
  case "$behavior" in
    synth_pass|reviewers_all_pass)
      cat <<'EOF'
{"per_reviewer":[
  {"vendor":"claude","verdict":"pass","findings":[]},
  {"vendor":"gemini","verdict":"pass","findings":[]},
  {"vendor":"codex","verdict":"pass","findings":[]}
]}
EOF
      exit 0
      ;;
    synth_fail_inv|reviewers_two_fail)
      cat <<'EOF'
{"per_reviewer":[
  {"vendor":"claude","verdict":"fail","findings":[
    {"severity":"invariant_violation","summary":"R3 vs R7 contradiction"}
  ]},
  {"vendor":"gemini","verdict":"fail","findings":[
    {"severity":"invariant_violation","summary":"requirement 3 cannot coexist with requirement 7"}
  ]},
  {"vendor":"codex","verdict":"pass","findings":[]}
]}
EOF
      exit 0
      ;;
    synth_needs_revision|reviewers_one_empty)
      cat <<'EOF'
{"per_reviewer":[
  {"vendor":"claude","verdict":"pass","findings":[]},
  {"vendor":"codex","verdict":"needs_revision","findings":[
    {"severity":"risk","summary":"unclear scope boundary"}
  ]}
]}
EOF
      exit 0
      ;;
    synth_empty)
      # not valid JSON → triggers mechanical fallback in runner
      echo "I apologize but I cannot comply."
      exit 0
      ;;
    synth_timeout)
      sleep 3600
      ;;
    *)
      # default: pass
      cat <<'EOF'
{"per_reviewer":[
  {"vendor":"claude","verdict":"pass","findings":[]}
]}
EOF
      exit 0
      ;;
  esac
fi

echo "unknown role: $role" >&2
exit 3
