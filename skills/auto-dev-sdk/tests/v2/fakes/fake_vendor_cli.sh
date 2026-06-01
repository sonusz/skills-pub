#!/usr/bin/env bash
# FakeCLI: simulates a vendor CLI subprocess for deterministic tests.
#
# Reads a single env var AUTODEV_FAKE_BEHAVIOR to pick a scenario:
#   success_design | success_build | success_spec | success_review
#   timeout | exit_nonzero | missing_artifact | malformed_json
#   out_of_scope_write
# Plus env vars:
#   AUTODEV_FAKE_TARGET_ARTIFACT — where to write the artifact
#   AUTODEV_FAKE_SOURCE_HASH — hash to embed in the artifact (for non-scope)
#   AUTODEV_FAKE_SOURCE_PATH — source path to embed
#   AUTODEV_FAKE_FEATURE — feature name
#
# All args after the flags are swallowed (the harness passes a prompt).
set -euo pipefail

behavior="${AUTODEV_FAKE_BEHAVIOR:-success_design}"
target="${AUTODEV_FAKE_TARGET_ARTIFACT:-}"
src_hash="${AUTODEV_FAKE_SOURCE_HASH:-sha256:0000000000000000000000000000000000000000000000000000000000000000}"
src_path="${AUTODEV_FAKE_SOURCE_PATH:-unknown}"
feature="${AUTODEV_FAKE_FEATURE:-demo}"
tmp="${target}.tmp"

case "$behavior" in
  success_design)
    tgt_design="${AUTODEV_FAKE_TARGET_ARTIFACT:-$target}"
    tgt_scope="${AUTODEV_FAKE_TARGET_SCOPE:?need scope target}"
    tgt_trace="${AUTODEV_FAKE_TARGET_TRACE:?need trace target}"
    tgt_tp="${AUTODEV_FAKE_TARGET_TEST_PLAN:?need test-plan target}"
    # design-changelog.json target — default to sibling of design.md when
    # the test doesn't pass an explicit env var.
    tgt_changelog="${AUTODEV_FAKE_TARGET_CHANGELOG:-$(dirname "$tgt_design")/design-changelog.json}"
    cat > "${tgt_design}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

## 1. Context
toy context

## 2. Primitives & commitments
toy primitive
Validation commands: ["pytest -q"]

## 3. Seams & integration points
toy seam

## 4. Design decisions
toy decision
EOF
    cat > "${tgt_scope}.tmp" <<EOF
{
  "source": "$src_path",
  "source_hash": "$src_hash",
  "written": "$(date +%Y-%m-%d)",
  "feature": "$feature",
  "mode": "fresh",
  "diff_base": "main",
  "in_scope": [
    {"id":"t-1","description":"toy item","prd_ref":["§1"],"design_ref":["§2"],"status":"active"}
  ],
  "excluded": []
}
EOF
    cat > "${tgt_trace}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status | Source |
|---|---|---|---|---|---|---|---|
| 1 | t-1.r1 | t-1 | toy | -- | -- | pending | Source: prd:§1 |
EOF
    cat > "${tgt_tp}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

## Test Strategy
toy
## Test Cases
| Scope ID | Desc | Tier | Edges | Fixtures | Source |
|---|---|---|---|---|---|
| t-1 | happy | unit | — | — | Source: prd:§1 |
## Coverage
t-1 covered.
EOF
    cat > "${tgt_changelog}.tmp" <<EOF
{
  "kind": "design-changelog",
  "schema_version": 1,
  "entries": [
    {
      "round": 1,
      "trigger": "initial",
      "reason": "first design pass (FakeCLI)",
      "artifacts_changed": ["design.md", "scope.json", "trace.md", "test-plan.md"],
      "added": [
        {"artifact": "scope.json", "anchor": "t-1"},
        {"artifact": "trace.md", "anchor": "t-1.r1"}
      ],
      "removed": []
    }
  ]
}
EOF
    exit 0
    ;;
  success_build)
    cat > "$tmp" <<EOF
{
  "source": "$src_path",
  "source_hash": "$src_hash",
  "written": "$(date +%Y-%m-%d)",
  "test_cmd_run": "pytest tests/",
  "test_exit_code": 0,
  "test_results": {"passed": 1, "failed": 0, "skipped": 0},
  "files_changed": ["toy.py","test_toy.py"],
  "lint": {"passed": true, "cmd": "n/a"},
  "deviations": [],
  "blocking": false,
  "workspace_dirty_at_stage_end": false
}
EOF
    exit 0
    ;;
  success_spec)
    tgt_readme="${AUTODEV_FAKE_TARGET_README:?need readme}"
    cat > "$tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

## 1. Purpose
toy spec
## 2. Users
n/a
## 3. Contract
n/a
## 4. Data model
n/a
## 5. Architecture
n/a
## 6. Out of scope
n/a
## 7. Testing
pytest
## 8. Operational notes
n/a
EOF
    cat > "${tgt_readme}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

# toy feature
See implemented-spec.md.
EOF
    exit 0
    ;;
  success_review)
    cat > "$tmp" <<EOF
{
  "source": "$src_path",
  "source_hash": "$src_hash",
  "written": "$(date +%Y-%m-%d)",
  "requirement_coverage": [
    {
      "req_id": "R1",
      "status": "covered",
      "evidence": "toy evidence"
    }
  ],
  "over_delivered": []
}
EOF
    exit 0
    ;;
  timeout)
    sleep 3600
    ;;
  exit_nonzero)
    echo "FakeCLI simulated failure" >&2
    exit 2
    ;;
  missing_artifact)
    # Exit 0 but don't write anything
    exit 0
    ;;
  missing_then_success_design)
    # Attempt 1: write design.md only (scope/trace/test-plan missing →
    # StageOutputInvalid:missing_artifact). Attempt 2+: write everything,
    # exercising the harness's bounded output-retry/amend path.
    tgt_design="${AUTODEV_FAKE_TARGET_ARTIFACT:-$target}"
    tgt_scope="${AUTODEV_FAKE_TARGET_SCOPE:?need scope target}"
    tgt_trace="${AUTODEV_FAKE_TARGET_TRACE:?need trace target}"
    tgt_tp="${AUTODEV_FAKE_TARGET_TEST_PLAN:?need test-plan target}"
    tgt_changelog="${AUTODEV_FAKE_TARGET_CHANGELOG:-$(dirname "$tgt_design")/design-changelog.json}"
    cnt="$(dirname "$tgt_design")/.fake.design.attempt"
    n=0; [ -f "$cnt" ] && n=$(cat "$cnt"); n=$((n + 1)); echo "$n" > "$cnt"
    cat > "${tgt_design}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

## 1. Context
toy context

## 2. Primitives & commitments
toy primitive
Validation commands: ["pytest -q"]

## 3. Seams & integration points
toy seam

## 4. Design decisions
toy decision
EOF
    if [ "$n" -ge 2 ]; then
      cat > "${tgt_scope}.tmp" <<EOF
{
  "source": "$src_path", "source_hash": "$src_hash",
  "written": "$(date +%Y-%m-%d)", "feature": "$feature",
  "mode": "fresh", "diff_base": "main",
  "in_scope": [{"id":"t-1","description":"toy item","prd_ref":["§1"],"design_ref":["§2"],"status":"active"}],
  "excluded": []
}
EOF
      cat > "${tgt_trace}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status | Source |
|---|---|---|---|---|---|---|---|
| 1 | t-1.r1 | t-1 | toy | -- | -- | pending | Source: prd:§1 |
EOF
      cat > "${tgt_tp}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

## Test Strategy
toy
## Test Cases
| Scope ID | Desc | Tier | Edges | Fixtures | Source |
|---|---|---|---|---|---|
| t-1 | happy | unit | — | — | Source: prd:§1 |
## Coverage
t-1 covered.
EOF
      cat > "${tgt_changelog}.tmp" <<EOF
{"kind":"design-changelog","schema_version":1,"entries":[{"round":1,"trigger":"initial","reason":"retry amend (FakeCLI)","artifacts_changed":["design.md","scope.json","trace.md","test-plan.md"],"added":[{"artifact":"scope.json","anchor":"t-1"}],"removed":[]}]}
EOF
    fi
    exit 0
    ;;
  malformed_provenance)
    # Primary markdown with NO `<!-- source_hash: ... -->` header. The
    # cascade's strict regex won't match → orchestrator must catch
    # this loud (provenance_malformed), not silently re-dispatch forever.
    # Extras get correct headers so the missing_artifact path is not
    # what fires.
    cat > "$tmp" <<EOF
# Provenance: generated from $src_path $src_hash on $(date +%Y-%m-%d)

## 1. Context
toy
EOF
    if [ -n "${AUTODEV_FAKE_TARGET_SCOPE:-}" ]; then
      cat > "${AUTODEV_FAKE_TARGET_SCOPE}.tmp" <<EOF
{
  "source": "$src_path", "source_hash": "$src_hash",
  "written": "$(date +%Y-%m-%d)", "feature": "$feature",
  "mode": "fresh", "diff_base": "main",
  "in_scope": [{"id":"t-1","description":"x","prd_ref":["§1"],"design_ref":["§1"],"status":"active"}],
  "excluded": []
}
EOF
    fi
    if [ -n "${AUTODEV_FAKE_TARGET_TRACE:-}" ]; then
      cat > "${AUTODEV_FAKE_TARGET_TRACE}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->
EOF
    fi
    if [ -n "${AUTODEV_FAKE_TARGET_TEST_PLAN:-}" ]; then
      cat > "${AUTODEV_FAKE_TARGET_TEST_PLAN}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->
EOF
    fi
    tgt_changelog_mp="${AUTODEV_FAKE_TARGET_CHANGELOG:-$(dirname "$tmp" 2>/dev/null)/design-changelog.json}"
    if [ -n "$tgt_changelog_mp" ]; then
      cat > "${tgt_changelog_mp}.tmp" <<EOF
{"kind":"design-changelog","schema_version":1,"entries":[{"round":1,"trigger":"initial","reason":"malformed provenance test","artifacts_changed":["design.md"],"added":[],"removed":[]}]}
EOF
    fi
    if [ -n "${AUTODEV_FAKE_TARGET_README:-}" ]; then
      cat > "${AUTODEV_FAKE_TARGET_README}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

# toy
EOF
    fi
    exit 0
    ;;
  malformed_json)
    echo '{this is not valid json' > "$tmp"
    exit 0
    ;;
  out_of_scope_write)
    tgt_design="${AUTODEV_FAKE_TARGET_ARTIFACT:-$target}"
    tgt_scope="${AUTODEV_FAKE_TARGET_SCOPE:?need scope target}"
    tgt_trace="${AUTODEV_FAKE_TARGET_TRACE:?need trace target}"
    tgt_tp="${AUTODEV_FAKE_TARGET_TEST_PLAN:?need test-plan target}"
    cat > "${tgt_design}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->

## 1. Context
toy context
EOF
    cat > "${tgt_scope}.tmp" <<EOF
{
  "source": "$src_path", "source_hash": "$src_hash",
  "written": "$(date +%Y-%m-%d)", "feature": "$feature",
  "mode": "fresh", "diff_base": "main",
  "in_scope": [{"id":"t-1","description":"x","prd_ref":["§1"],"design_ref":["§1"],"status":"active"}],
  "excluded": []
}
EOF
    cat > "${tgt_trace}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->
EOF
    cat > "${tgt_tp}.tmp" <<EOF
<!-- source: $src_path -->
<!-- source_hash: $src_hash -->
<!-- written: $(date +%Y-%m-%d) -->
EOF
    tgt_changelog_oos="${AUTODEV_FAKE_TARGET_CHANGELOG:-$(dirname "$tgt_design")/design-changelog.json}"
    cat > "${tgt_changelog_oos}.tmp" <<EOF
{"kind":"design-changelog","schema_version":1,"entries":[{"round":1,"trigger":"initial","reason":"out_of_scope_write test","artifacts_changed":["design.md"],"added":[],"removed":[]}]}
EOF
    outside="${AUTODEV_FAKE_ESCAPE_PATH:-/tmp/escape.txt}"
    echo "escaped" > "$outside"
    exit 0
    ;;
  *)
    echo "FakeCLI: unknown behavior $behavior" >&2
    exit 1
    ;;
esac
