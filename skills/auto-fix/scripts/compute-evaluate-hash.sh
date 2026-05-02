#!/usr/bin/env bash
# compute-evaluate-hash.sh
#
# Reads a decision-packet JSON from stdin and prints a 16-character
# SHA-256 hex prefix. The hash is canonical: keys sorted, no whitespace.
# Same packet → same hash, on macOS or Linux, in any shell context.
#
# Used by auto-fix's evaluate mode to bind user consent to a specific
# proposed change. The caller stores the hash, then passes it as
# `--confirmed-evaluate-hash <hash>` when invoking apply mode. apply
# re-derives the hash from the freshly-recomputed §§1-5 decision packet
# and refuses if it has drifted from what the user reviewed.
#
# What goes in the packet (built by the agent, fed to this script):
#   inputs:
#     mode: "evaluate"  (always, for hashing — apply re-runs evaluate)
#     comment_text or error_log
#     affected_files (sorted)
#     author_type
#     branch, head_sha (when present)
#   decision:
#     status, category, summary, evidence, would_apply_description
#
# Do NOT include timestamps, random fields, or anything that varies
# between identical evaluations.
#
# Usage:
#   echo "$packet_json" | scripts/compute-evaluate-hash.sh

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
  echo "compute-evaluate-hash.sh: jq is required" >&2
  exit 1
fi

if ! command -v shasum >/dev/null 2>&1 && ! command -v sha256sum >/dev/null 2>&1; then
  echo "compute-evaluate-hash.sh: shasum or sha256sum is required" >&2
  exit 1
fi

# `jq -cS .` canonicalizes: sorted keys, compact whitespace.
canonical=$(jq -cS .)

if command -v sha256sum >/dev/null 2>&1; then
  printf '%s' "$canonical" | sha256sum | cut -c1-16
else
  printf '%s' "$canonical" | shasum -a 256 | cut -c1-16
fi
