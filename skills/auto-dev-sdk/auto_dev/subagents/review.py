"""review-feature subagent runner.

Compares PRD ↔ spec; classifies every in-scope item. Processes ALL items
(active + removed/superseded) per agents.md — the one stage that needs the
full scope history.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_dev.artifacts.common import write_markdown_with_hash
from auto_dev.state.hashing import hash_file
from auto_dev.subagents.prompts import load
from auto_dev.subagents.runner import SubagentInput, SubagentRunner
from auto_dev.vendors.config import StageSpec


def run_review(
    *,
    feature: str,
    feature_root: Path,
    scope_path: Path,
    prd_path: Path,
    spec_path: Path,
    trace_path: Path,
    stage_spec: StageSpec,
    runner: SubagentRunner | None = None,
) -> dict[str, Any]:
    runner = runner or SubagentRunner(stage_spec)
    scope_hash = hash_file(scope_path)
    prd_hash = hash_file(prd_path)
    spec_hash = hash_file(spec_path)
    trace_hash = hash_file(trace_path)
    runner.guard_inputs([
        SubagentInput("scope", scope_path, scope_hash),
        SubagentInput("prd", prd_path, prd_hash),
        SubagentInput("spec", spec_path, spec_hash),
        SubagentInput("trace", trace_path, trace_hash),
    ])

    system = load("review")
    payload = {
        "feature": feature,
        "scope": {"path": str(scope_path), "hash": scope_hash, "content": scope_path.read_text(encoding="utf-8")},
        "prd": {"path": str(prd_path), "hash": prd_hash, "content": prd_path.read_text(encoding="utf-8")},
        "spec": {"path": str(spec_path), "hash": spec_hash, "content": spec_path.read_text(encoding="utf-8")},
        "trace": {"path": str(trace_path), "hash": trace_hash, "content": trace_path.read_text(encoding="utf-8")},
        "instruction": (
            "Return strict JSON with `review_md` (full body) and `classifications` "
            "(list of {id, classification, notes}). Valid classifications: "
            "FullyImplemented, PartiallyImplemented, Missing, Deviated, Deferred, "
            "removed_by_design, superseded. Active items get one of the first five; "
            "removed items get `removed_by_design`; superseded items get `superseded`."
        ),
    }
    response = runner.call(system=system, payload=payload, max_tokens=16000)
    data = response.structured
    runner.require_keys(data, ("review_md", "classifications"), where="review")

    review_path = feature_root / "review.md"
    write_markdown_with_hash(review_path, data["review_md"], source=str(scope_path), source_hash=scope_hash)

    return {
        "review_path": str(review_path),
        "classifications": list(data["classifications"]),
        "deviations": list(data.get("deviations", [])),
    }
