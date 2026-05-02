"""PRD-review subagent (PRD R11 `subagent` mode).

Quality critique of an existing PRD. Produces `prd-review.md`. Non-blocking.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_dev.artifacts.common import write_markdown_with_hash
from auto_dev.state.hashing import hash_file
from auto_dev.subagents.prompts import load
from auto_dev.subagents.runner import SubagentInput, SubagentRunner
from auto_dev.vendors.config import StageSpec


def run_prd_review(
    *,
    feature: str,
    feature_root: Path,
    prd_path: Path,
    stage_spec: StageSpec,
    runner: SubagentRunner | None = None,
) -> dict[str, Any]:
    runner = runner or SubagentRunner(stage_spec)
    prd_hash = hash_file(prd_path)
    runner.guard_inputs([SubagentInput("prd", prd_path, prd_hash)])

    system = load("prd_review")
    payload = {
        "feature": feature,
        "prd": {"path": str(prd_path), "hash": prd_hash, "content": prd_path.read_text(encoding="utf-8")},
        "instruction": (
            "Critique this PRD for completeness, clarity, testability, conflicting "
            "requirements, hidden assumptions, risk areas. Return strict JSON with "
            "`review_md` (full body), `concerns` (list of short strings), "
            "`blocking_issues` (list — empty means nothing structurally wrong)."
        ),
    }
    response = runner.call(system=system, payload=payload, max_tokens=8000)
    data = response.structured
    runner.require_keys(data, ("review_md", "concerns", "blocking_issues"), where="prd_review")

    review_path = feature_root / "prd-review.md"
    write_markdown_with_hash(review_path, data["review_md"], source=str(prd_path), source_hash=prd_hash)

    return {
        "prd_review_path": str(review_path),
        "concerns": list(data["concerns"]),
        "blocking_issues": list(data["blocking_issues"]),
    }
