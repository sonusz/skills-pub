"""implement-feature subagent runner.

TDD loop delegated to the subagent. This orchestrator-side wrapper:
  * Verifies input hashes.
  * Supplies scope/trace/test-plan bodies plus the allowed toolset.
  * Expects back: `files_changed`, `test_results`, `deviations`, `blocking`.
  * Writes `build.json` with upstream hash (scope.json).

The subagent is expected to use the bundled `ToolExecutor` (via the prompt's
tool contract) — this wrapper does NOT execute code on its behalf; it
receives the report.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from auto_dev.artifacts.build import BuildReport, write_build
from auto_dev.executor.permissions import PermissionRules
from auto_dev.executor.tool_schemas import default_tools
from auto_dev.executor.tools import ToolExecutor
from auto_dev.state.hashing import hash_file
from auto_dev.subagents.prompts import load
from auto_dev.subagents.runner import SubagentInput, SubagentRunner
from auto_dev.vendors.config import StageSpec


def run_implement(
    *,
    feature: str,
    feature_root: Path,
    scope_path: Path,
    trace_path: Path,
    test_plan_path: Path,
    stage_spec: StageSpec,
    allow_cmd: list[str],
    deny_cmd: list[str],
    runner: SubagentRunner | None = None,
    executor: ToolExecutor | None = None,
    repo_root: Path | None = None,
    max_iterations: int = 25,
) -> dict[str, Any]:
    runner = runner or SubagentRunner(stage_spec)
    scope_hash = hash_file(scope_path)
    trace_hash = hash_file(trace_path)
    test_plan_hash = hash_file(test_plan_path)
    runner.guard_inputs([
        SubagentInput("scope", scope_path, scope_hash),
        SubagentInput("trace", trace_path, trace_hash),
        SubagentInput("test_plan", test_plan_path, test_plan_hash),
    ])

    system = load("implement")
    payload = {
        "feature": feature,
        "scope": {
            "path": str(scope_path),
            "hash": scope_hash,
            "content": scope_path.read_text(encoding="utf-8"),
        },
        "trace": {
            "path": str(trace_path),
            "hash": trace_hash,
            "content": trace_path.read_text(encoding="utf-8"),
        },
        "test_plan": {
            "path": str(test_plan_path),
            "hash": test_plan_hash,
            "content": test_plan_path.read_text(encoding="utf-8"),
        },
        "permissions": {"allow_cmd": allow_cmd, "deny_cmd": deny_cmd},
        "instruction": (
            "Follow TDD per scope item. Return strict JSON with keys "
            "`test_results` ({passed, failed, skipped, cmd, output_path?}), "
            "`lint` ({passed, cmd?, output_path?}), `files_changed` (list), "
            "`deviations` (list of {scope_id, severity, blocking, detail}), "
            "`blocking` (bool)."
        ),
    }
    # Build executor if caller didn't pass one.
    if executor is None:
        rules = PermissionRules.from_flags(
            allow_cmd=allow_cmd, deny_cmd=deny_cmd,
            repo_root=repo_root or feature_root.parents[3],
            feature_root=repo_root or feature_root.parents[3],
        )
        executor = ToolExecutor(rules, cwd=repo_root or feature_root.parents[3])

    response = runner.call(
        system=system, payload=payload, max_tokens=32000,
        tools=default_tools(), executor=executor,
        max_iterations=max_iterations,
    )
    data = response.structured
    runner.require_keys(
        data,
        ("test_results", "lint", "files_changed", "deviations", "blocking"),
        where="implement",
    )

    report = BuildReport(
        source=str(scope_path),
        source_hash=scope_hash,
        written=date.today().isoformat(),
        test_results=data["test_results"],
        lint=data["lint"],
        files_changed=list(data["files_changed"]),
        deviations=list(data["deviations"]),
        blocking=bool(data["blocking"]),
    )
    build_path = feature_root / "build.json"
    write_build(build_path, report)

    return {
        "build_json_path": str(build_path),
        "files_changed": report.files_changed,
        "test_results": report.test_results,
        "deviations": report.deviations,
        "blocking": report.blocking,
    }
