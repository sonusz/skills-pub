"""feature-spec subagent wrapper.

Delegates to the external `feature-spec` skill (PRD: not internal). The
wrapper expects a callable that knows how to invoke the skill; the default
calls into `auto_dev.skills.feature_spec`. Returns the paths / envelope
flag from the skill's result.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

from auto_dev.artifacts.common import write_markdown_with_hash
from auto_dev.state.hashing import hash_file
from auto_dev.subagents.prompts import load
from auto_dev.subagents.runner import SubagentInput, SubagentRunner
from auto_dev.vendors.config import StageSpec


def run_spec(
    *,
    feature: str,
    feature_root: Path,
    scope_path: Path,
    stage_spec: StageSpec,
    skill_invoker: Callable[..., dict[str, Any]] | None = None,
    runner: SubagentRunner | None = None,
) -> dict[str, Any]:
    scope_hash = hash_file(scope_path)

    if skill_invoker is not None:
        result = skill_invoker(
            feature=feature,
            feature_root=feature_root,
            scope_path=scope_path,
            scope_hash=scope_hash,
        )
        return result

    # Fallback: treat spec generation as an LLM subagent call.
    runner = runner or SubagentRunner(stage_spec)
    runner.guard_inputs([SubagentInput("scope", scope_path, scope_hash)])

    system = load("spec")
    payload = {
        "feature": feature,
        "scope": {
            "path": str(scope_path),
            "hash": scope_hash,
            "content": scope_path.read_text(encoding="utf-8"),
        },
        "instruction": (
            "Generate a navigable spec. Return strict JSON with `spec_md` (full body "
            "including 8 sections), `readme_md`, `envelope` (bool — true when >60 KB), "
            "`sub_files` (list of {path, body} when envelope is true)."
        ),
    }
    response = runner.call(system=system, payload=payload, max_tokens=32000)
    data = response.structured
    runner.require_keys(data, ("spec_md", "readme_md", "envelope"), where="spec")

    spec_path = feature_root / "spec.md"
    readme_path = feature_root / "README.md"
    write_markdown_with_hash(spec_path, data["spec_md"], source=str(scope_path), source_hash=scope_hash)
    write_markdown_with_hash(readme_path, data["readme_md"], source=str(scope_path), source_hash=scope_hash)

    sub_files: list[str] = []
    if data.get("envelope"):
        for sub in data.get("sub_files", []):
            p = feature_root / sub["path"]
            write_markdown_with_hash(p, sub["body"], source=str(scope_path), source_hash=scope_hash)
            sub_files.append(str(p))

    return {
        "spec_path": str(spec_path),
        "readme_path": str(readme_path),
        "envelope": bool(data["envelope"]),
        "sub_files": sub_files,
    }
