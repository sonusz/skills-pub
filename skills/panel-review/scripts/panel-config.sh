#!/usr/bin/env bash
# Helpers for reading the panel vendor config (vendors.yaml, or sample-vendors.yaml as fallback).
#
# This is intentionally a tiny parser for the fixed config shape in this skill,
# not a general YAML implementation.
set -euo pipefail

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  printf "panel-config.sh is a library; source it from panel-review scripts.\n" >&2
  exit 2
fi

# A caller skill that reuses these scripts through a skills/panel-review link
# (another skill) presets PANEL_SKILL_DIR so its own vendors.yaml /
# sample-vendors.yaml and shared/vendors are used.
PANEL_SKILL_DIR="${PANEL_SKILL_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PANEL_VENDORS_DIR="$PANEL_SKILL_DIR/shared/vendors"
PANEL_VENDOR_CALL="$PANEL_VENDORS_DIR/scripts/call.sh"

panel_yaml_value() {
  local yaml_file="$1"
  local section="$2"
  local id="$3"
  local key="$4"

  awk -v want_section="$section" -v want_id="$id" -v want_key="$key" '
    function clean(v) {
      sub(/^[[:space:]]+/, "", v)
      sub(/[[:space:]]+$/, "", v)
      if (v ~ /^".*"$/ || v ~ /^'\''.*'\''$/) {
        v = substr(v, 2, length(v) - 2)
      }
      return v
    }
    /^[[:space:]]*(#|$)/ { next }
    /^[^[:space:]][^:]*:/ {
      section=$1
      sub(/:$/, "", section)
      in_record=0
      next
    }
    section == "panel" && $0 ~ /^[[:space:]]*-[[:space:]]*id:[[:space:]]*/ {
      value=$0
      sub(/^[[:space:]]*-[[:space:]]*id:[[:space:]]*/, "", value)
      current_id=clean(value)
      in_record=(want_section == "panel" && current_id == want_id)
      if (in_record && want_key == "id") {
        print current_id
        exit
      }
      next
    }
    section == "synthesis" && $0 ~ /^[[:space:]]+id:[[:space:]]*/ {
      value=$0
      sub(/^[[:space:]]+id:[[:space:]]*/, "", value)
      current_id=clean(value)
      in_record=(want_section == "synthesis" && current_id == want_id)
      if (in_record && want_key == "id") {
        print current_id
        exit
      }
      next
    }
    in_record {
      line=$0
      sub(/^[[:space:]]+/, "", line)
      split(line, parts, ":")
      key=parts[1]
      if (key == want_key) {
        sub(/^[^:]*:[[:space:]]*/, "", line)
        print clean(line)
        exit
      }
    }
  ' "$yaml_file"
}

panel_yaml_panel_ids() {
  local yaml_file="$1"

  awk '
    function clean(v) {
      sub(/^[[:space:]]+/, "", v)
      sub(/[[:space:]]+$/, "", v)
      return v
    }
    /^[[:space:]]*(#|$)/ { next }
    /^[^[:space:]][^:]*:/ {
      section=$1
      sub(/:$/, "", section)
      next
    }
    section == "panel" && $0 ~ /^[[:space:]]*-[[:space:]]*id:[[:space:]]*/ {
      value=$0
      sub(/^[[:space:]]*-[[:space:]]*id:[[:space:]]*/, "", value)
      print clean(value)
    }
  ' "$yaml_file"
}

panel_yaml_synthesis_id() {
  local yaml_file="$1"

  awk '
    function clean(v) {
      sub(/^[[:space:]]+/, "", v)
      sub(/[[:space:]]+$/, "", v)
      return v
    }
    /^[[:space:]]*(#|$)/ { next }
    /^[^[:space:]][^:]*:/ {
      section=$1
      sub(/:$/, "", section)
      next
    }
    section == "synthesis" && $0 ~ /^[[:space:]]+id:[[:space:]]*/ {
      value=$0
      sub(/^[[:space:]]+id:[[:space:]]*/, "", value)
      print clean(value)
      exit
    }
  ' "$yaml_file"
}

# Machine-local config wins; the tracked sample (every vendor) is the fallback.
panel_default_config() {
  local local_yaml="$PANEL_SKILL_DIR/vendors.yaml"
  local sample_yaml="$PANEL_SKILL_DIR/sample-vendors.yaml"

  if [ -r "$local_yaml" ]; then
    printf "%s\n" "$local_yaml"
    return 0
  fi
  printf "note: %s not found; using %s (every vendor). Generate a local file with\n" \
    "$local_yaml" "$sample_yaml" >&2
  printf "      python3 %s/scripts/init-vendors.py --sample %s --out %s\n" \
    "$PANEL_VENDORS_DIR" "$sample_yaml" "$local_yaml" >&2
  printf "%s\n" "$sample_yaml"
}

panel_require_config() {
  local yaml_file="$1"

  if [ ! -r "$yaml_file" ]; then
    printf "FAIL: cannot read vendors config at %s\n" "$yaml_file" >&2
    printf "      Generate one from the sample: python3 %s/scripts/init-vendors.py --sample %s/sample-vendors.yaml --out %s/vendors.yaml\n" \
      "$PANEL_VENDORS_DIR" "$PANEL_SKILL_DIR" "$PANEL_SKILL_DIR" >&2
    return 2
  fi
  if [ ! -x "$PANEL_VENDOR_CALL" ]; then
    printf "FAIL: cannot execute vendor module call.sh at %s\n" "$PANEL_VENDOR_CALL" >&2
    return 2
  fi
}

panel_call_args() {
  local yaml_file="$1"
  local section="$2"
  local id="$3"
  local vendor=""
  local effort=""
  local model=""

  vendor=$(panel_yaml_value "$yaml_file" "$section" "$id" vendor)
  effort=$(panel_yaml_value "$yaml_file" "$section" "$id" effort)
  model=$(panel_yaml_value "$yaml_file" "$section" "$id" model)

  if [ -z "$vendor" ]; then
    printf "FAIL: %s call %s has no vendor in %s\n" "$section" "$id" "$yaml_file" >&2
    return 2
  fi

  printf '%s\0' --vendor "$vendor"
  [ -z "$effort" ] || printf '%s\0' --effort "$effort"
  [ -z "$model" ] || printf '%s\0' --model "$model"
}
