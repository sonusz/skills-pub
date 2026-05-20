#!/usr/bin/env python3
"""Stage-autodetecting FakeCLI for full-pipeline smoke tests.

Inspects the prompt (last positional arg) to figure out which stage
it was invoked for, then writes the corresponding fake artifact(s).
Differs from fake_vendor_cli.sh which requires per-stage
AUTODEV_FAKE_BEHAVIOR switching — impractical across a multi-stage
autodev run invocation.

Invocation: same args the shared vendors launcher sends to the selected
CLI. The rendered prompt arrives on stdin for Claude/Codex; older tests
may still pass it as the final argv.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

_DATE = datetime.date.today().isoformat()
_HASH_ZERO = "sha256:" + "0" * 64


def _hash_file(p: Path) -> str:
    """Match the harness's hashing.hash_file convention."""
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return f"sha256:{h}"


def _extract_path(prompt: str, keys: list[str]) -> Path | None:
    """Find the first prompt line naming any of `keys` as a path."""
    for key in keys:
        m = re.search(
            rf"\*?\*?{re.escape(key)}\*?\*?\s*[:=]\s*`?([^\s`\n]+)`?",
            prompt,
        )
        if m:
            return Path(m.group(1))
    return None


def _write_tmp(target: Path, body: str) -> None:
    tmp = target.with_name(target.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(body, encoding="utf-8")


def main() -> int:
    stdin_prompt = sys.stdin.read()
    if stdin_prompt:
        prompt = stdin_prompt
    elif len(sys.argv) >= 2 and sys.argv[-1] != "-":
        prompt = sys.argv[-1]
    else:
        print("fake_vendor_cli_auto: missing prompt", file=sys.stderr)
        return 2

    # Stage detection: look for target variables the stage-*.md prompts
    # declare. The unified design stage writes four artifacts.
    tgt_design = _extract_path(prompt, ["TARGET_DESIGN"])
    tgt_scope = _extract_path(prompt, ["TARGET_SCOPE", "TARGET_ARTIFACT"]) if "stage-design" in prompt or "stage-scope" in prompt or "scope.json" in prompt else None
    tgt_trace = _extract_path(prompt, ["TARGET_TRACE"])
    tgt_test_plan = _extract_path(prompt, ["TARGET_TEST_PLAN"])
    tgt_changelog = _extract_path(prompt, ["TARGET_CHANGELOG"])
    tgt_build = _extract_path(prompt, ["TARGET_BUILD_JSON"])
    tgt_spec = _extract_path(prompt, ["TARGET_SPEC"])
    tgt_readme = _extract_path(prompt, ["TARGET_README"])
    tgt_review = _extract_path(prompt, ["TARGET_REVIEW"])
    tgt_ralph_review = _extract_path(prompt, ["TARGET_RALPH_REVIEW"])

    # TARGET_ARTIFACT is the primary; the stage-specific keys above
    # are stage-resolvable without ambiguity except scope/build which
    # both share TARGET_ARTIFACT. Disambiguate by filename.
    primary = _extract_path(prompt, ["TARGET_ARTIFACT"])
    if primary is not None:
        name = primary.name
        if name == "scope.json":
            tgt_scope = primary
        elif name == "design.md":
            tgt_design = primary
        elif name == "build.json":
            tgt_build = primary
        elif name == "review.json":
            tgt_review = primary
        elif name == "ralph-review.json":
            tgt_ralph_review = primary
        elif name in ("spec.md", "implemented-spec.md"):
            tgt_spec = primary
        elif name == "trace.md":
            tgt_trace = primary

    # unified design stage
    if tgt_design and tgt_scope and tgt_trace and tgt_test_plan:
        feature = os.environ.get("AUTODEV_FAKE_FEATURE", "smoke")
        active = tgt_design.parent
        prd_path = active / "prd.md"
        src_hash = _hash_file(prd_path) if prd_path.exists() else _HASH_ZERO
        scope_path = active / "scope.json"
        _write_tmp(tgt_design, f"""<!-- source: {prd_path} -->
<!-- source_hash: {src_hash} -->
<!-- written: {_DATE} -->

# Design

## 2. Primitives & commitments
Validation commands: ["pytest -q"]

## Flow
Implement the smoke feature in one pass.
""")
        _write_tmp(tgt_scope, f"""{{
  "source": "docs/features/{feature}/active/prd.md",
  "source_hash": "{src_hash}",
  "written": "{_DATE}",
  "feature": "{feature}",
  "mode": "fresh",
  "diff_base": "main",
  "in_scope": [
    {{"id": "t-1", "description": "smoke test item", "prd_ref": ["R1"], "design_ref": ["Flow"], "status": "active"}}
  ],
  "excluded": []
}}
""")
        _write_tmp(tgt_trace, f"""<!-- source: {prd_path} -->
<!-- source_hash: {src_hash} -->
<!-- written: {_DATE} -->

| # | Req ID | Scope ID | Requirement | Test(s) | Code Path | Status |
|---|---|---|---|---|---|---|
| 1 | t-1.r1 | t-1 | smoke test | -- | -- | pending |
""")
        _write_tmp(tgt_test_plan, f"""<!-- source: {prd_path} -->
<!-- source_hash: {src_hash} -->
<!-- written: {_DATE} -->

## Test Strategy
Smoke-test only — single happy path.

## Test Cases

| Scope ID | Description | Tier | Edges | Fixtures |
|---|---|---|---|---|
| t-1 | smoke happy path | unit | — | — |

## Coverage Summary
t-1 covered.
""")
        if tgt_changelog:
            # Read prior changelog if present to compute next round number
            existing_rounds: list = []
            if tgt_changelog.exists():
                try:
                    prior = json.loads(tgt_changelog.read_text(encoding="utf-8"))
                    existing_rounds = prior.get("entries", []) or []
                except Exception:
                    existing_rounds = []
            next_round = (
                max((e.get("round", 0) for e in existing_rounds), default=0) + 1
            )
            new_entry = {
                "round": next_round,
                "trigger": "initial" if next_round == 1 else "design-review",
                "reason": "fake auto vendor",
                "artifacts_changed": ["design.md", "scope.json", "trace.md", "test-plan.md"],
                "added": [],
                "removed": [],
            }
            entries = list(existing_rounds) + [new_entry]
            _write_tmp(tgt_changelog, json.dumps({
                "kind": "design-changelog",
                "schema_version": 1,
                "entries": entries,
            }))
        return 0

    # build stage
    if tgt_build:
        active = tgt_build.parent
        scope_path = active / "scope.json"
        src_hash = _hash_file(scope_path) if scope_path.exists() else _HASH_ZERO
        _write_tmp(tgt_build, f"""{{
  "source": "scope.json",
  "source_hash": "{src_hash}",
  "written": "{_DATE}",
  "test_cmd_run": "pytest tests/",
  "test_exit_code": 0,
  "test_results": {{"passed": 1, "failed": 0, "skipped": 0}},
  "files_changed": ["smoke.py", "test_smoke.py"],
  "lint": {{"passed": true, "cmd": "n/a"}},
  "deviations": [],
  "blocking": false,
  "workspace_dirty_at_stage_end": false
}}
""")
        return 0

    # spec stage (implemented-spec.md + README.md)
    if tgt_spec:
        active = tgt_spec.parent
        index_path = active / "implementation-index.json"
        src_hash = _hash_file(index_path) if index_path.exists() else _HASH_ZERO
        _write_tmp(tgt_spec, f"""<!-- source: implementation-index.json -->
<!-- source_hash: {src_hash} -->
<!-- written: {_DATE} -->

## 1. Purpose
Smoke spec.

## 2. Users
Internal smoke test.

## 3. Contract
Smoke behavior implemented.

## 4. Data model
None.

## 5. Architecture
None.

## 6. Out of scope
Everything else.

## 7. Testing
pytest smoke.

## 8. Operational notes
None.
""")
        if tgt_readme:
            _write_tmp(tgt_readme, f"""<!-- source: implemented-spec.md -->
<!-- source_hash: {src_hash} -->
<!-- written: {_DATE} -->

# Smoke feature
See implemented-spec.md.
""")
        return 0

    # ralph-review stage (v3-core: JSON output, not markdown)
    if tgt_ralph_review:
        _write_tmp(tgt_ralph_review, """{
  "classifications": [
    {"req_id": "t-1.r1", "scope_id": "t-1", "classification": "Fully", "evidence": "fake_vendor output"}
  ],
  "summary": {"Fully": 1, "Partial": 0, "Missing": 0, "Deviated": 0, "Deferred": 0}
}
""")
        return 0

    if tgt_review:
        # v3-core: review.json per-PRD-requirement coverage.
        active = tgt_review.parent
        prd_path = active / "prd.md"
        prd_hash = _hash_file(prd_path) if prd_path.exists() else _HASH_ZERO
        spec_path = active / "implemented-spec.md"
        spec_hash = _hash_file(spec_path) if spec_path.exists() else _HASH_ZERO
        import json as _json
        _write_tmp(tgt_review, _json.dumps({
            "source": str(prd_path),
            "source_hash": prd_hash,
            "spec_hash": spec_hash,
            "written": _DATE,
            "requirement_coverage": [
                {"req_id": "R1", "status": "covered",
                 "evidence": "spec §3.1 — fake vendor output"},
            ],
            "over_delivered": [],
            "summary": {"covered": 1, "missing": 0, "contradicted": 0,
                        "partially_covered": 0, "over_delivered": 0},
        }, indent=2) + "\n")
        return 0

    print(f"fake_vendor_cli_auto: could not detect stage from prompt; no target path found",
          file=sys.stderr)
    print(f"prompt-first-500-chars: {prompt[:500]}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
