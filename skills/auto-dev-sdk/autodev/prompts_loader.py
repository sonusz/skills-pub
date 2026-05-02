"""Load + render stage prompts with orchestrator-supplied variables."""
from __future__ import annotations

from pathlib import Path

from autodev.state.hashing import hash_file

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


_STAGE_PROMPT_FILE = {
    "design": "stage-scope.md",
    "build":  "stage-implement.md",
    "spec":   "stage-spec.md",
}


def _prompt_file_for_stage(stage: str) -> Path:
    if stage == "ralph-review":
        return PROMPTS_DIR / "stage-ralph-review.md"
    return PROMPTS_DIR / _STAGE_PROMPT_FILE[stage]


def render_stage_prompt(
    *,
    stage: str,
    feature: str,
    feature_active: Path,
    repo_root: Path,
    primary_target: Path,
    extra_targets: list[Path],
    context_artifacts: list[str] | None = None,
) -> str:
    """Build the prompt string passed to the shared vendors adapter.

    Appends a "## Orchestrator context" section to the stage prompt file
    body, listing the concrete paths + hashes the subagent needs.
    """
    prompt_file = _prompt_file_for_stage(stage)
    body = prompt_file.read_text(encoding="utf-8")

    ctx_lines: list[str] = [
        "",
        "---",
        "",
        "## Orchestrator context (filled at invocation)",
        "",
        f"- FEATURE: `{feature}`",
        f"- FEATURE_ACTIVE: `{feature_active}`",
        f"- REPO_ROOT: `{repo_root}`",
        f"- PROMPT_FILE: `{prompt_file}`",
        f"- PROMPT_HASH: `{hash_file(prompt_file)}`",
    ]

    # Per-stage upstream artifacts + hashes. Spec is deliberately
    # code-first: it receives only the harness-authored implementation
    # index, not PRD/scope/design/build semantics.
    upstream_by_stage = {
        "design": [
            (feature_active / "prd.md",     "PRD_PATH",   "PRD_HASH"),
        ],
        "build":  [
            (feature_active / "prd.md",        "PRD_PATH",       "PRD_HASH"),
            (feature_active / "design-packet.json", "DESIGN_PACKET_PATH", "DESIGN_PACKET_HASH"),
            (feature_active / "accepted-design.json", "ACCEPTED_DESIGN_PATH", "ACCEPTED_DESIGN_HASH"),
            (feature_active / "design.md",     "DESIGN_PATH",    "DESIGN_HASH"),
            (feature_active / "scope.json",    "SCOPE_PATH",     "SCOPE_HASH"),
            (feature_active / "trace.md",      "TRACE_PATH",     "TRACE_HASH"),
            (feature_active / "test-plan.md",  "TEST_PLAN_PATH", "TEST_PLAN_HASH"),
        ],
        "spec":   [
            (
                feature_active / "implementation-index.json",
                "IMPLEMENTATION_INDEX_PATH",
                "IMPLEMENTATION_INDEX_HASH",
            ),
        ],
        # ralph-review is deliberately context-isolated: code + trace only.
        # No PRD/scope/build.json — it's a pure "does code match trace?"
        # classification gear, not an intent-vs-impl judge.
        "ralph-review": [
            (feature_active / "trace.md",      "TRACE_PATH",      "TRACE_HASH"),
        ],
    }
    for path, name_key, hash_key in upstream_by_stage.get(stage, []):
        if path.exists():
            ctx_lines.append(f"- {name_key}: `{path}`")
            ctx_lines.append(f"- {hash_key}: `{hash_file(path)}`")
        else:
            ctx_lines.append(f"- {name_key}: `{path}` (MISSING — stage will abort)")

    # Target artifacts
    target_map = {
        "design": [
            ("TARGET_DESIGN", primary_target),
            ("TARGET_SCOPE", extra_targets[0] if len(extra_targets) > 0 else feature_active / "scope.json"),
            ("TARGET_TRACE", extra_targets[1] if len(extra_targets) > 1 else feature_active / "trace.md"),
            ("TARGET_TEST_PLAN", extra_targets[2] if len(extra_targets) > 2 else feature_active / "test-plan.md"),
        ],
        "build":  [("TARGET_BUILD_JSON", primary_target)],
        "spec":   [("TARGET_SPEC",    primary_target),
                   ("TARGET_README",  extra_targets[0] if extra_targets else feature_active / "README.md")],
        "review": [("TARGET_REVIEW",  primary_target)],
        "ralph-review": [("TARGET_RALPH_REVIEW", primary_target)],
    }
    for key, p in target_map.get(stage, []):
        ctx_lines.append(f"- {key}: `{p}`")

    # v3-core: CONTEXT_ARTIFACTS — stage-relevant feedback artifacts
    # (prior verdict for this stage, own previous output, route feedback,
    # etc.). Empty on initial runs.
    effective_context = context_artifacts or []
    if effective_context:
        ctx_lines.append(f"- CONTEXT_ARTIFACTS: {effective_context}")
        ctx_lines.append("")
        ctx_lines.append(
            "Read every path in CONTEXT_ARTIFACTS. Use them to inform this "
            "stage's output: a panel verdict file may contain "
            "`invariant_violation` or `risk` findings pointing at this "
            "stage's artifact — address those (MUST fix). `opinion` "
            "findings may be acknowledged but do not require change. "
            "A previous version of your own artifact (same filename) may "
            "be present — revise it in place rather than regenerate from "
            "scratch; preserve stable IDs and incorporate the fixes the "
            "panel asked for."
        )
    else:
        ctx_lines.append("- CONTEXT_ARTIFACTS: [] (initial run)")

    # Iteration history manifest — temporal view of artifact / gate
    # events drawn from log.jsonl. Lets the consumer see WHEN each
    # CONTEXT_ARTIFACTS file was produced and whether anything has
    # happened since (most importantly: whether prd.md was amended
    # after the panel verdict was written).
    from autodev.iteration_history import (
        build_iteration_history,
        render_iteration_history,
    )

    history = build_iteration_history(feature_active)
    if history:
        ctx_lines.append("")
        ctx_lines.append("## Iteration history (oldest → newest)")
        ctx_lines.append("")
        ctx_lines.append(render_iteration_history(history))
        ctx_lines.append("")
        ctx_lines.append(
            "Read this manifest to place every file in CONTEXT_ARTIFACTS "
            "and the upstream paths above on a single timeline. Key "
            "inferences it enables: (a) if a panel verdict's row is "
            "older than `prd.md AMENDED`, that verdict was produced "
            "under a previous PRD — its findings may already be "
            "addressed by the latest amendment; treat them as historical "
            "signals to confirm rather than fresh blockers. (b) if your "
            "own previous output (e.g. `design.md` for the design stage) "
            "appears in the manifest, the most recent corresponding row "
            "shows the version on disk you should revise from. (c) "
            "`Current hash` is a sha256 prefix of the file as it stands "
            "right now; mismatches between the row's logical claim and "
            "the current hash mean the file has been rewritten since "
            "the event."
        )

    ctx_lines.append("")
    ctx_lines.append(
        "Write each TARGET_* to `<path>.tmp`; the orchestrator renames to "
        "final on exit 0. Exit non-zero on abort."
    )
    ctx_lines.append("")

    return body + "\n".join(ctx_lines)
