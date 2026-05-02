"""plan-feature subagent runner.

Input: scope.json + hash.
Output: trace.md + test-plan.md with source_hash header.
Return JSON shape: { trace_path, test_plan_path, row_count, coverage_gaps }.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_dev.artifacts.common import write_markdown_with_hash
from auto_dev.state.hashing import hash_file
from auto_dev.subagents.prompts import load
from auto_dev.subagents.runner import SubagentInput, SubagentRunner
from auto_dev.vendors.config import StageSpec


def run_plan(
    *,
    feature: str,
    feature_root: Path,
    scope_path: Path,
    stage_spec: StageSpec,
    runner: SubagentRunner | None = None,
) -> dict[str, Any]:
    runner = runner or SubagentRunner(stage_spec)
    scope_hash = hash_file(scope_path)
    runner.guard_inputs([SubagentInput("scope", scope_path, scope_hash)])

    system = load("plan")
    payload = {
        "feature": feature,
        "scope_path": str(scope_path),
        "scope_hash": scope_hash,
        "scope_content": scope_path.read_text(encoding="utf-8"),
        "instruction": (
            "Return strict JSON with keys `trace_md`, `test_plan_md`, `row_count`, "
            "`coverage_gaps`. `trace_md` and `test_plan_md` are the full file bodies "
            "(no hash header — the caller injects it)."
        ),
    }
    response = runner.call(system=system, payload=payload, max_tokens=16000)
    data = response.structured
    runner.require_keys(data, ("trace_md", "test_plan_md", "row_count", "coverage_gaps"), where="plan")

    trace_path = feature_root / "trace.md"
    test_plan_path = feature_root / "test-plan.md"
    write_markdown_with_hash(
        trace_path,
        data["trace_md"],
        source=str(scope_path),
        source_hash=scope_hash,
    )
    write_markdown_with_hash(
        test_plan_path,
        data["test_plan_md"],
        source=str(scope_path),
        source_hash=scope_hash,
    )

    return {
        "trace_path": str(trace_path),
        "test_plan_path": str(test_plan_path),
        "row_count": int(data["row_count"]),
        "coverage_gaps": list(data.get("coverage_gaps", [])),
    }
